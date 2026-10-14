#!/usr/bin/env python3
"""Ramu Kaka as a remote MCP server -- the door ChatGPT (or any MCP client) uses.

Runs ALONGSIDE daemon.py on the same machine, as a separate process. It holds no
credentials and talks to no API: it only writes into rpc_inbox.json and reads
back out of rpc_responses/, which is daemon.py's round-trip door (see
rpc_loop). Everything that makes Ramu Kaka itself -- memory, the capability
library, ClickUp/calendar writes, conversation history -- stays in the daemon.
This file is a thin mouthpiece on purpose: the blast radius of something
reachable from the public internet should be as small as it can be.

Auth is a secret path segment: the server answers only at /<RAMU_MCP_SECRET>/mcp.
ChatGPT's connector UI takes a URL and little else, so a secret in the URL is
the one scheme that reliably works there. A Bearer token is also enforced if
RAMU_MCP_BEARER is set. Treat the URL itself as a credential.

Written against the MCP Python SDK 2.x (FastMCP was renamed MCPServer in 2.0).

Env (from .env, gitignored):
    RAMU_MCP_SECRET       required, >=32 chars. The secret path segment.
    RAMU_MCP_PUBLIC_HOST  required. The cloudflared hostname, e.g. ramu.example.com.
                          The SDK's DNS-rebinding guard rejects any Host it
                          isn't told about, and behind a tunnel the Host header
                          is the public name, not localhost.
    RAMU_MCP_BEARER       optional, and MUST be left unset for ChatGPT. A bare
                          401 is not "unauthorized" to an MCP client -- it is the
                          start of an OAuth handshake, which this server does not
                          implement, so ChatGPT refuses to connect with
                          "does not implement OAuth". Fine for clients that can
                          send a static header (Claude Code, scripts).
    RAMU_MCP_PORT         default 8787. Bound to 127.0.0.1; cloudflared fronts it.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import daemon as ramu  # constants + enqueue_rpc; importing starts no loops
import ramu_profile

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# How long a single tool call is willing to block. ChatGPT gives a tool call
# roughly a minute before it gives up, while a claude -p call behind the daemon
# can legitimately take two or three. So: wait a short while, and if the answer
# isn't ready, hand back a ticket instead of dying. collect_ramu_kaka_reply
# picks it up on the next turn -- the model asks again rather than the answer
# being lost.
FIRST_WAIT_SECONDS = 50
POLL_SECONDS = 1.0

mcp = MCPServer(
    "ramu-kaka",
    instructions=(
        f"Ramu Kaka is {ramu_profile.NAME}'s personal life assistant. It knows their health, "
        "training, food, habits, ClickUp Life OS tasks and personal calendar, "
        "and it has durable memory of them. Ask it anything about their day or "
        "their commitments, or tell it something worth remembering or acting on."
    ),
)


def _await_reply(request_id: str, budget: float) -> dict | None:
    path = os.path.join(ramu.RPC_RESPONSES_DIR, f"{request_id}.json")
    deadline = time.time() + budget
    while time.time() < deadline:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    payload = json.load(f)
            except (OSError, json.JSONDecodeError):
                time.sleep(POLL_SECONDS)  # still being written; look again
                continue
            try:
                os.remove(path)  # collected exactly once
            except OSError:
                pass
            return payload
        time.sleep(POLL_SECONDS)
    return None


@mcp.tool()
def ask_ramu_kaka(message: str) -> str:
    """Ask the user's personal assistant Ramu Kaka something and get its answer back.

    Ramu Kaka runs the user's life operating system: health, training, food,
    habits, their ClickUp Life OS task list and their personal calendar. It has
    durable memory of them and context on their current goals and commitments.
    Use this to ask about their day, tasks or training, or to tell it something
    that should be remembered or acted on -- it can create and close tasks and
    log habits itself.

    If the answer is not ready in time this returns a ticket; call
    collect_ramu_kaka_reply with it to get the answer.
    """
    message = (message or "").strip()
    if not message:
        return "Nothing to ask -- the message was empty."
    request_id = uuid.uuid4().hex[:16]
    if not ramu.enqueue_rpc(request_id, message, origin="ChatGPT"):
        return "Ramu Kaka's request queue is full right now. Try again in a minute."

    payload = _await_reply(request_id, FIRST_WAIT_SECONDS)
    if payload is None:
        return (
            "Ramu Kaka is still thinking (this can take a couple of minutes). "
            f"Call collect_ramu_kaka_reply with ticket {request_id} for the answer."
        )
    if not payload.get("ok"):
        return payload.get("error") or "Ramu Kaka could not answer that."
    return payload["reply"]


@mcp.tool()
def collect_ramu_kaka_reply(ticket: str) -> str:
    """Collect an answer from an earlier ask_ramu_kaka call that wasn't ready yet.

    Pass the ticket id ask_ramu_kaka returned. Safe to call repeatedly; if the
    answer still isn't ready it says so.
    """
    ticket = (ticket or "").strip()
    if not ticket:
        return "No ticket given."
    payload = _await_reply(ticket, FIRST_WAIT_SECONDS)
    if payload is None:
        return f"Still not ready. Call collect_ramu_kaka_reply with ticket {ticket} again."
    if not payload.get("ok"):
        return payload.get("error") or "Ramu Kaka could not answer that."
    return payload["reply"]


@mcp.tool()
def tell_ramu_kaka(message: str) -> str:
    """Send the user's assistant a note without waiting for a reply.

    Ramu Kaka answers on the user's Telegram instead. Use this for fire-and-forget
    things -- capturing a task, logging that they did something -- when the answer
    does not need to come back into this conversation.
    """
    message = (message or "").strip()
    if not message:
        return "Nothing to send -- the message was empty."
    ramu._enqueue_synthetic(message)
    return f"Sent. Ramu Kaka will reply on {ramu_profile.NAME}'s Telegram."


class BearerGate(BaseHTTPMiddleware):
    def __init__(self, app, token: str):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request, call_next):
        got = request.headers.get("authorization", "")
        if not got.startswith("Bearer ") or not secrets.compare_digest(
            got[len("Bearer "):], self.token
        ):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def build_app():
    secret = os.environ.get("RAMU_MCP_SECRET", "")
    public_host = os.environ.get("RAMU_MCP_PUBLIC_HOST", "")
    bearer = os.environ.get("RAMU_MCP_BEARER", "")
    port = int(os.environ.get("RAMU_MCP_PORT", "8787"))

    if len(secret) < 32:
        raise SystemExit(
            "RAMU_MCP_SECRET missing or shorter than 32 chars. It is the only "
            "thing between the public internet and a system that can write to "
            "the user's ClickUp, calendar and memory. Generate one with:\n"
            "  python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
        )
    if not public_host:
        raise SystemExit(
            "RAMU_MCP_PUBLIC_HOST not set (e.g. ramu.example.com). Without it "
            "the SDK's DNS-rebinding guard rejects every tunnelled request."
        )

    app = mcp.streamable_http_app(
        streamable_http_path=f"/{secret}/mcp",
        transport_security=TransportSecuritySettings(
            allowed_hosts=[public_host, f"127.0.0.1:{port}", f"localhost:{port}"],
            allowed_origins=[f"https://{public_host}", "https://chatgpt.com",
                             "https://chat.openai.com"],
        ),
    )
    if bearer:
        app.add_middleware(BearerGate, token=bearer)
    return app


if __name__ == "__main__":
    import uvicorn

    ramu.load_env_file(ramu.ENV_PATH)
    os.makedirs(ramu.RPC_RESPONSES_DIR, exist_ok=True)
    # 127.0.0.1 only. Reaching this from outside is cloudflared's job, so the
    # machine never has an inbound port open even if the tunnel is down.
    uvicorn.run(
        build_app(),
        host="127.0.0.1",
        port=int(os.environ.get("RAMU_MCP_PORT", "8787")),
        log_level="info",
    )
