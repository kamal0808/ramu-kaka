"""Blockers: what Ramu Kaka is waiting on, and waking it when the wait is over.

WHY. When a task needs a service the agent isn't connected to yet, the right
move is to link it through Composio (`composio link <toolkit>`, a URL the user
taps -- credentials go to Composio, never through chat). But once the user
finishes linking, nothing would resume the original goal; they would have to
remember to ask again.

This module is the second half: a small registry of open blockers, and a check
that notices when one is over. The FIRST half (discover -> verify -> link) is
the UNBLOCK PROTOCOL in daemon.py's reply prompt.

A blocker is {goal, toolkit, word_id?, resume}. It resolves when Composio
reports an ACTIVE connection for that toolkit (the specific word_id if given).
On resolve the daemon injects an automatic note into the normal reply pipeline,
so Ramu wakes up with full memory/capabilities, carries on, then tells the user.

No LLM and no network beyond one `composio connections list` per open blocker,
so an idle registry costs nothing. Stdlib only; no dependency on daemon.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timedelta

import ramu_memory as rm
import ramu_profile

AUTO_PREFIX = "[auto]"  # marks a note the daemon wrote to Ramu, not the user
MAX_OPEN = 20


def _now() -> datetime:
    return datetime.now(ramu_profile.TZ)


def _load(path: str) -> list[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save(path: str, items: list[dict]) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def add(items: list, path: str, source: str = "telegram") -> list[str]:
    """Register blockers. item: {"goal","toolkit","word_id"?,"resume"}. One result string each."""
    results = []
    blockers = _load(path)
    for it in items or []:
        if not isinstance(it, dict):
            results.append("skipped (not an object)")
            continue
        goal = " ".join(str(it.get("goal") or "").split())[:300]
        toolkit = rm.slugify(it.get("toolkit") or "").replace("-", "")
        resume = " ".join(str(it.get("resume") or "").split())[:800]
        word_id = str(it.get("word_id") or "").strip()[:80]
        if not goal or not toolkit or not resume:
            results.append(f"skipped (needs goal, toolkit and resume): {toolkit or '?'}")
            continue
        if rm.looks_secret(goal + " " + resume):
            results.append(f"skipped (looks like a secret): {toolkit}")
            continue
        existing = next((b for b in blockers if b["status"] == "open" and b["toolkit"] == toolkit
                         and rm.slugify(b["goal"]) == rm.slugify(goal)), None)
        if existing:
            existing.update(resume=resume, word_id=word_id or existing.get("word_id", ""))
            results.append(f"refreshed: {toolkit}")
            continue
        if sum(1 for b in blockers if b["status"] == "open") >= MAX_OPEN:
            results.append(f"skipped (too many open blockers): {toolkit}")
            continue
        blockers.append({
            "id": f"{toolkit}-{_now().strftime('%m%d%H%M%S')}", "goal": goal, "toolkit": toolkit,
            "word_id": word_id, "resume": resume, "status": "open", "source": source,
            "created": rm._iso(_now()), "resolved": "",
        })
        results.append(f"waiting on {toolkit}: {goal[:60]}")
    _save(path, blockers)
    return results


def open_blockers(path: str) -> list[dict]:
    return [b for b in _load(path) if b.get("status") == "open"]


def list_connections(toolkit: str, composio_bin: str = "composio", timeout: int = 45) -> list[dict] | None:
    """Composio's view of a toolkit's connections, or None if the check itself failed
    (a failed check must never look like 'not connected yet' AND never resolve anything)."""
    env = dict(os.environ)
    env["PATH"] = os.path.expanduser("~/.local/bin") + os.pathsep + env.get("PATH", "")
    try:
        r = subprocess.run([composio_bin, "connections", "list", "--toolkit", toolkit],
                           capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    out = r.stdout or ""
    i = out.find("{")
    if i < 0:
        return None
    try:
        data = json.loads(out[i:])
    except json.JSONDecodeError:
        return None
    conns = data.get(toolkit) if isinstance(data, dict) else None
    return conns if isinstance(conns, list) else []


def _is_live(blocker: dict, conns: list[dict]) -> bool:
    active = [c for c in conns if str(c.get("status", "")).upper() == "ACTIVE"]
    if blocker.get("word_id"):
        return any(c.get("word_id") == blocker["word_id"] for c in active)
    return bool(active)


def check(path: str, expire_days: int = 5, lister=list_connections) -> list[dict]:
    """Resolve blockers whose connection is now live; expire stale ones silently.
    Returns the newly-resolved blockers (caller wakes Ramu for each)."""
    blockers = _load(path)
    resolved = []
    changed = False
    now = _now()
    for b in blockers:
        if b.get("status") != "open":
            continue
        created = rm._parse_iso(b.get("created"))
        if created and now - created > timedelta(days=expire_days):
            b["status"], b["resolved"] = "expired", rm._iso(now)
            changed = True
            continue
        conns = lister(b["toolkit"])
        if conns is None:
            continue  # couldn't check; try again next cycle
        if _is_live(b, conns):
            b["status"], b["resolved"] = "resolved", rm._iso(now)
            resolved.append(dict(b))
            changed = True
    if changed:
        _save(path, blockers)
    return resolved


def resume_message(b: dict) -> str:
    """The automatic note that wakes Ramu. Goes through the normal reply pipeline."""
    return (
        f"{AUTO_PREFIX} Not from {ramu_profile.NAME} -- an automatic note to you. The {b['toolkit']} connection "
        f"you were waiting on is now live. What {ramu_profile.NAME} wanted: {b['goal']}. Your own note on how to "
        f"continue: {b['resume']} Carry on now, and tell {ramu_profile.NAME} briefly what you did or what you need "
        f"next. External actions (spending money, calling or messaging anyone but {ramu_profile.NAME}) still need "
        f"{ramu_profile.NAME}'s yes first."
    )
