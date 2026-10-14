"""Direct Telegram Bot API for Ramu Kaka, no Composio in the path.

Why: every call through the Composio CLI costs seconds of process overhead, and
its toolkit has no typing indicator. Talking to api.telegram.org directly makes
receiving and sending near-instant and lets us show "typing...".

Also the missing security check: only messages from ALLOWED_CHAT_ID are handled.
Before this, anyone who found the bot could talk to it.

The message shapes and the offset invariant mirror listen.sh exactly (see its
header): every update we consume produces one dict before the offset advances,
so nothing is silently swallowed. The offset lives in the same `.last_update`
file listen.sh uses, so falling back to listen.sh is safe.

The bot token is read from the environment or the project's .env and is never
logged. Errors are sanitized because the token sits in the request URL.
"""
import json
import os
import ssl
import time
import urllib.error
import urllib.request

import ramu_profile

API = "https://api.telegram.org"
# Env wins, then the profile. Unset = 0, which matches no sender: nothing is handled.
ALLOWED_CHAT_ID = int(os.environ.get("RAMU_CHAT_ID") or ramu_profile.PROFILE.get("telegram_chat_id") or 0)
UPDATE_TYPES = ["message", "edited_message", "callback_query"]
MEDIA_KINDS = ["photo", "document", "video", "video_note", "animation", "voice", "audio", "sticker", "location", "contact"]
MAX_TEXT = 4000  # Telegram's limit is 4096 characters


class TelegramError(Exception):
    """Sanitized: never contains the token."""


def load_token(project_dir):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if tok:
        return tok
    try:
        with open(os.path.join(project_dir, ".env")) as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:]
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


_ctx = None


def _ssl_context():
    """Verified TLS. The python.org framework Python on macOS ships without a CA bundle
    (default cafile None), so use certifi if present, else the macOS system
    bundle. Verification is never turned off."""
    global _ctx
    if _ctx is None:
        cafile = None
        try:
            import certifi
            cafile = certifi.where()
        except ImportError:
            if os.path.exists("/etc/ssl/cert.pem"):
                cafile = "/etc/ssl/cert.pem"
        _ctx = ssl.create_default_context(cafile=cafile)
    return _ctx


def _http_post(url, payload, timeout):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:  # Telegram puts the reason in the body
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:  # network, timeout, bad JSON: keep the URL (token) out
        raise TelegramError(f"{type(e).__name__} talking to Telegram") from None


def call(token, method, payload, timeout=15):
    data = _http_post(f"{API}/bot{token}/{method}", payload, timeout)
    if not data.get("ok"):
        raise TelegramError(f"{method}: {data.get('description', 'failed')}")
    return data.get("result")


def send_message(token, chat_id, text, buttons=None, silent=False):
    """Plain text, split at the length limit. Buttons (rows of [label, data])
    ride on the last chunk only. Returns True when everything was delivered."""
    if not text:
        return False
    chunks = [text[i:i + MAX_TEXT] for i in range(0, len(text), MAX_TEXT)]
    for n, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk}
        if silent:
            payload["disable_notification"] = True
        if buttons and n == len(chunks) - 1:
            payload["reply_markup"] = {"inline_keyboard": [
                [{"text": b[0], "callback_data": b[1]} for b in row] for row in buttons]}
        call(token, "sendMessage", payload)
    return True


def send_chat_action(token, chat_id, action="typing"):
    call(token, "sendChatAction", {"chat_id": chat_id, "action": action}, timeout=5)


def answer_callback(token, callback_id):
    call(token, "answerCallbackQuery", {"callback_query_id": callback_id}, timeout=5)


def _media_info(kind, m):
    if kind == "photo":
        photos = m.get("photo") or []
        return {"file_id": photos[-1].get("file_id")} if photos else None
    if kind in ("document", "video", "audio", "animation"):
        d = m.get(kind) or {}
        return {"file_id": d.get("file_id"), "file_name": d.get("file_name"), "mime_type": d.get("mime_type")}
    if kind in ("voice", "video_note"):
        v = m.get(kind) or {}
        return {"file_id": v.get("file_id"), "mime_type": v.get("mime_type")}
    if kind == "sticker":
        s = m.get("sticker") or {}
        return {"file_id": s.get("file_id"), "emoji": s.get("emoji")}
    if kind == "location":
        loc = m.get("location") or {}
        return {"latitude": loc.get("latitude"), "longitude": loc.get("longitude")}
    if kind == "contact":
        c = m.get("contact") or {}
        return {"phone_number": c.get("phone_number"), "first_name": c.get("first_name")}
    return None


def _placeholder(kind, info):
    if kind == "document":
        return f"[document: {(info or {}).get('file_name') or 'unnamed file'}]"
    if kind == "location":
        return "[location]"
    if kind == "contact":
        return f"[contact: {(info or {}).get('first_name') or 'contact'}]"
    return f"[{kind}, no caption]"


def sender_id(u):
    cq = u.get("callback_query")
    if cq:
        return (cq.get("from") or {}).get("id")
    m = u.get("message") or u.get("edited_message")
    if m:
        return (m.get("from") or {}).get("id")
    return None


def normalize_update(u):
    """One update -> one dict, in the shape listen.sh emits. Never returns None:
    an unrecognized update still yields an 'unhandled' dict."""
    uid = u["update_id"]
    cq = u.get("callback_query")
    if cq:
        data = (cq.get("data") or "").strip()
        msg = cq.get("message") or {}
        if data:
            return {"update_id": uid, "date": msg.get("date"), "text": data, "type": "callback_query"}
        return {"update_id": uid, "date": msg.get("date"), "text": "[callback_query with no data]",
                "type": "unhandled", "raw_keys": sorted(cq.keys())}

    m = u.get("message") or u.get("edited_message")
    if m is None:
        keys = sorted(u.keys())
        return {"update_id": uid, "text": f"[unhandled update type: {keys}]", "type": "unhandled", "raw_keys": keys}

    txt = (m.get("text") or "").strip()
    caption = (m.get("caption") or "").strip()
    media_kind = next((k for k in MEDIA_KINDS if m.get(k)), None)
    if txt:
        out = {"update_id": uid, "date": m.get("date"), "text": txt, "type": "message"}
    elif media_kind:
        info = _media_info(media_kind, m)
        out = {"update_id": uid, "date": m.get("date"), "type": "message", "media_kind": media_kind,
               "text": caption or _placeholder(media_kind, info)}
        if info:
            out["media"] = info
    elif caption:
        out = {"update_id": uid, "date": m.get("date"), "text": caption, "type": "message"}
    else:
        keys = sorted(m.keys())
        out = {"update_id": uid, "date": m.get("date"), "text": f"[unhandled update type: {keys}]",
               "type": "unhandled", "raw_keys": keys}

    if m.get("forward_origin") or m.get("forward_date") or m.get("forward_from") or m.get("forward_from_chat"):
        out["forwarded"] = True
    r = m.get("reply_to_message")
    if r:
        rt = (r.get("text") or r.get("caption") or "").strip()
        if rt:
            out["reply_to_text"] = rt
    return out


def read_offset(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def write_offset(path, hi):
    with open(path, "w") as f:
        f.write(str(hi))


def poll(token, offset_path, long_poll_seconds=0, allowed_chat_id=ALLOWED_CHAT_ID):
    """Returns (messages, dropped). Updates from anyone but the allowed chat are
    dropped (and counted) but still advance the offset so they are not replayed."""
    offset = read_offset(offset_path)
    payload = {"timeout": long_poll_seconds, "allowed_updates": UPDATE_TYPES}
    if offset is not None:
        payload["offset"] = offset + 1
    res = call(token, "getUpdates", payload, timeout=long_poll_seconds + 10) or []
    messages, dropped, hi = [], 0, None
    for u in res:
        hi = u["update_id"]
        if sender_id(u) != allowed_chat_id:
            dropped += 1
            continue
        messages.append(normalize_update(u))
    if hi is not None:
        write_offset(offset_path, hi)
    for u in res:  # stop the button spinner; best-effort, after the offset is safe
        cq = u.get("callback_query")
        if cq and cq.get("id") and sender_id(u) == allowed_chat_id:
            try:
                answer_callback(token, cq["id"])
            except TelegramError:
                pass
    return messages, dropped


MAX_DOWNLOAD = 20 * 1024 * 1024  # the Bot API will not hand over bigger files


def download(token, file_id, dest_dir, timeout=60):
    """Fetch a file the sender uploaded and return its local path. Raises
    TelegramError (sanitized) when it is too big or the fetch fails."""
    info = call(token, "getFile", {"file_id": file_id}) or {}
    remote = info.get("file_path")
    if not remote:
        raise TelegramError("getFile: no file_path")
    if (info.get("file_size") or 0) > MAX_DOWNLOAD:
        raise TelegramError("file is over the 20MB bot download limit")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"{int(time.time() * 1000)}_{os.path.basename(remote)}")
    try:
        with urllib.request.urlopen(f"{API}/file/bot{token}/{remote}", timeout=timeout,
                                    context=_ssl_context()) as r, open(dest, "wb") as f:
            f.write(r.read(MAX_DOWNLOAD + 1))
    except Exception as e:
        raise TelegramError(f"{type(e).__name__} downloading a file") from None
    return dest
