"""Promises Ramu Kaka makes in chat ("I'll ping you at 9", "checking back
tonight") that must actually fire.

Before this, a promise was just words in a reply: "I'll ping you at 9 to
leave" had nothing behind it and would never have gone out.
Now the reply envelope carries "promises": [{"at": ISO, "say": text}], the
daemon stores them here, and promise_loop sends each one when it falls due.

Pure functions over a small JSON file; the loop and the Telegram send live in
daemon.py.
"""
import json
import os
import uuid
from datetime import datetime, timedelta

# A promise that fell due while the daemon was down still goes out on restart,
# but not once it's this stale -- "time to leave" at 3am is worse than nothing.
MAX_LATE = timedelta(hours=2)
MAX_AHEAD = timedelta(days=14)


def load(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save(path, promises):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(promises, f, indent=1)
    os.replace(tmp, path)


def _parse(ts, tz):
    try:
        dt = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)  # the model writes local wall-clock times
    return dt.astimezone(tz)


def normalize(raw, now):
    """Envelope entries -> clean promise records. Drops anything unusable:
    no text, unparseable time, in the past, or absurdly far out."""
    out = []
    for p in raw or []:
        if not isinstance(p, dict):
            continue
        say = str(p.get("say") or "").strip()
        at = _parse(p.get("at"), now.tzinfo)
        if not say or at is None or at <= now or at - now > MAX_AHEAD:
            continue
        out.append({
            "id": uuid.uuid4().hex[:10],
            "at": at.isoformat(timespec="minutes"),
            "say": say,
            "urgency": p.get("urgency") if p.get("urgency") in ("chat", "important", "urgent") else "chat",
            "call": bool(p.get("call")),
            "created": now.isoformat(timespec="seconds"),
            "status": "pending",
        })
    return out


def add(path, records):
    if not records:
        return []
    promises = load(path)
    known = {(p.get("at"), p.get("say")) for p in promises if p.get("status") == "pending"}
    fresh = [r for r in records if (r["at"], r["say"]) not in known]
    if fresh:
        save(path, promises + fresh)
    return fresh


def due(promises, now):
    """(to_send, to_expire): pending promises whose time has come."""
    send, expire = [], []
    for p in promises:
        if p.get("status") != "pending":
            continue
        at = _parse(p.get("at"), now.tzinfo)
        if at is None or at > now:
            continue
        (expire if now - at > MAX_LATE else send).append(p)
    return send, expire


def mark(path, ids, status, now):
    promises = load(path)
    for p in promises:
        if p.get("id") in ids:
            p["status"] = status
            p["closed"] = now.isoformat(timespec="seconds")
    # Keep the file small: drop closed entries older than a week.
    cutoff = now - timedelta(days=7)
    promises = [p for p in promises if p.get("status") == "pending"
                or (_parse(p.get("closed"), now.tzinfo) or now) > cutoff]
    save(path, promises)


def pending_block(promises, now):
    """What's already promised, so the model doesn't promise it twice or forget it."""
    rows = [p for p in promises if p.get("status") == "pending"]
    if not rows:
        return ""
    rows.sort(key=lambda p: p["at"])
    lines = "\n".join(f"- {p['at'][5:16].replace('T', ' ')}: {p['say']}" for p in rows[:10])
    return f"Promises already scheduled (these WILL fire; don't re-promise them):\n{lines}\n---\n"
