"""Gate for anything Ramu Kaka says to the user that they did not just ask for,
and the chase when they go quiet on something it did say.

Pure functions, no I/O, so it can be tested without importing daemon.py.
State is derived from the conversation log (turns with role/ts, and
"unsolicited": true on messages the daemon sent on its own), so there is no
extra runtime file to keep in sync.

Why: a fixed gap and daily cap stopped the spam (a wall of unanswered
messages on waking), but once the cap was used up the agent went silent for
the rest of the day. A person doesn't count to three and stop.
If you're talking, they keep talking. If you go quiet on them, they don't pile
on new topics -- they chase the one thing they said, harder the more it
matters: "you there?", "where did you go?", "please, this is urgent", and
finally they call.

Rules for a new topic (evaluate):
  1. Quiet hours: nothing unsolicited, ever. No override.
  2. kind="briefing" (the morning day-build) stops here.
  3. One at a time: if an unsolicited message is still unanswered, a new
     topic waits -- unless it is more urgent than the one the user is sitting on.

The chase (next_chase), on the unanswered message, by its urgency:
  chat       -- two light nudges, then let it go
  important  -- three firmer nudges, then a call
  urgent     -- nudges every few minutes, then a call
"""
from datetime import datetime, timedelta

import ramu_profile

URGENCY = ("chat", "important", "urgent")

# minutes after the unanswered message -> step. "call" is the last resort.
CHASE_SCHEDULE = {
    "chat": [90, 240],
    "important": [30, 75, 150, "call"],
    "urgent": [5, 10, 15, "call"],
}
_CALL_AT = {"important": 180, "urgent": 20}

CHASE_LINES = {
    "chat": [
        "You there?",
        "Hey, where did you go? Ping me when you surface.",
    ],
    "important": [
        f"{ramu_profile.NAME}, you there? Need you on this one.",
        "Hey, where did you disappear to? This one actually matters.",
        "Okay, now I'm worried. Are you alive? Please reply, even one word.",
    ],
    "urgent": [
        f"{ramu_profile.NAME}, this is urgent. Are you there?",
        "Please respond. It can't wait.",
        "Please, please reply. I'm going to call you next.",
    ],
}

# A chase that started too long ago is stale -- the user is clearly off somewhere,
# and picking it up hours later would just be the wall of texts again.
_MAX_CHASE_AGE = timedelta(hours=8)


def _parse(ts, tz):
    try:
        return datetime.fromisoformat(str(ts)).astimezone(tz)
    except (TypeError, ValueError):
        return None


def _rank(urgency):
    return URGENCY.index(urgency) if urgency in URGENCY else 0


def in_quiet_hours(now: datetime, start_hour: int, end_hour: int) -> bool:
    hour = now.hour
    if start_hour <= end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def _open_thread(turns, tz):
    """The unsolicited message the user hasn't answered, and the chase turns sent
    on it since. (None, []) if they have answered everything."""
    parsed = [(ts, t) for t in turns if (ts := _parse(t.get("ts"), tz)) is not None]
    parsed.sort(key=lambda p: p[0])
    anchor, chases = None, []
    for ts, t in parsed:
        if t.get("role") == "user":
            anchor, chases = None, []
        elif t.get("unsolicited"):
            if t.get("chase"):
                if anchor is not None:
                    chases.append((ts, t))
            else:
                anchor, chases = (ts, t), []
    return anchor, chases


def evaluate(turns, now, quiet_start=0, quiet_end=8, kind="nudge", urgency="chat"):
    """May a new unsolicited topic go out now? Return (allowed, reason).
    `now` must be timezone-aware."""
    if in_quiet_hours(now, quiet_start, quiet_end):
        return False, "quiet hours"
    if kind == "briefing":
        return True, ""
    anchor, _ = _open_thread(turns, now.tzinfo)
    if anchor is not None and now - anchor[0] < _MAX_CHASE_AGE:
        if _rank(urgency) > _rank(anchor[1].get("urgency")):
            return True, ""
        return False, "previous unsolicited message is still unanswered"
    return True, ""


def next_chase(turns, now, quiet_start=0, quiet_end=8):
    """What to do about the message the user is ignoring, if anything is due now.
    Returns None, or {"step": n, "urgency": u, "action": "text"|"call",
    "text": str}."""
    if in_quiet_hours(now, quiet_start, quiet_end):
        return None
    anchor, chases = _open_thread(turns, now.tzinfo)
    if anchor is None:
        return None
    sent_at, msg = anchor
    # An untapped check-in ("Had lunch?") is not worth "You there?". It still
    # holds the gate in evaluate(), so questions don't pile up behind it.
    if msg.get("checkin_q"):
        return None
    age = now - sent_at
    if age >= _MAX_CHASE_AGE:
        return None
    # Don't pick up a chase that straddles the night: they were asleep, not ignoring it.
    if sent_at.date() != now.date() or in_quiet_hours(sent_at, quiet_start, quiet_end):
        return None
    urgency = msg.get("urgency") if msg.get("urgency") in URGENCY else "chat"
    schedule = CHASE_SCHEDULE[urgency]
    step = len(chases)
    if step >= len(schedule):
        return None
    # After a daemon restart several steps can be overdue at once; space them out.
    if chases and now - chases[-1][0] < timedelta(minutes=4):
        return None
    due = schedule[step]
    if due == "call":
        if age < timedelta(minutes=_CALL_AT[urgency]):
            return None
        return {"step": step + 1, "urgency": urgency, "action": "call",
                "text": "Couldn't reach you on Telegram, so I'm calling."}
    if age < timedelta(minutes=due):
        return None
    return {"step": step + 1, "urgency": urgency, "action": "text",
            "text": CHASE_LINES[urgency][step]}
