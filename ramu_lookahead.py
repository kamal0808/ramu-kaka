"""Lookahead: Ramu Kaka preps the next 48 hours instead of waiting to be asked.

Why: when the user mentions in chat that an event has moved or
changed, saying "noted" isn't enough; the calendar keeps the wrong
times and the day around it falls apart. The fix is to read the calendar,
check it against what the user said, research the place, move the blocks, and
tell them once.

So twice a day, and whenever the user's personal calendar for the next 48h
changes, a worker gets the coming events plus today's chat and does that job:
verify, fix what's reversible on the personal calendar, and say one line only
if something changed or needs the user. Silent when all is well.

Pure functions; daemon.lookahead_loop does the I/O.
"""
import hashlib
import json
from datetime import datetime, timedelta

import ramu_profile

RUN_HOURS = (10, 18)          # local time: morning prep and evening prep for tomorrow
MIN_GAP = timedelta(hours=3)  # a calendar change never triggers more often than this


def fingerprint(events):
    """Stable hash of what's coming: changes when an event is added, moved, renamed or removed."""
    key = sorted((e.get("id") or "", e.get("start") or "", e.get("end") or "", e.get("summary") or "",
                  e.get("location") or "") for e in events or [])
    return hashlib.sha1(json.dumps(key).encode()).hexdigest()[:16]


def _parse(ts, tz):
    try:
        return datetime.fromisoformat(str(ts)).astimezone(tz)
    except (TypeError, ValueError):
        return None


def due(now, state, fp):
    """(run?, reason). Scheduled slots run once a day each; a changed calendar
    runs at most every MIN_GAP, and never at night."""
    state = state or {}
    if not 9 <= now.hour < 23:
        return False, ""
    last = _parse(state.get("last_run"), now.tzinfo)
    today = now.date().isoformat()
    for h in RUN_HOURS:
        slot = f"{today}T{h:02d}"
        if now.hour >= h and slot not in (state.get("slots") or []) and (last is None or now - last >= timedelta(minutes=30)):
            return True, slot
    if fp and fp != state.get("fp") and state.get("fp") and (last is None or now - last >= MIN_GAP):
        return True, "changed"
    return False, ""


def mark(state, now, fp, reason):
    state = dict(state or {})
    slots = [s for s in state.get("slots") or [] if s.startswith(now.date().isoformat())]
    if reason:  # any run covers every slot already passed today (no 10:00 then 18:00 double on a late start)
        slots += [f"{now.date().isoformat()}T{h:02d}" for h in RUN_HOURS if now.hour >= h]
    return {"last_run": now.isoformat(timespec="seconds"), "fp": fp, "slots": slots}


def brief(events, company_events, now, personal_cal, personal_account):
    n = ramu_profile.NAME
    tz = now.tzname() or str(ramu_profile.TZ)
    return (
        f"LOOKAHEAD. You are Ramu Kaka, {n}'s chief of staff, prepping the next 48 hours so {n} never has to. "
        f"Now: {now.strftime('%a %d %b %H:%M')} {tz}.\n\n"
        f"{n}'s PERSONAL calendar, next 48h ({personal_cal}, composio account {personal_account}; "
        "you may edit it):\n" + json.dumps(events, ensure_ascii=False) + "\n\n"
        f"{n}'s WORK calendar, same window, READ-ONLY (only to spot clashes; never edit or reply):\n"
        + json.dumps(company_events or [], ensure_ascii=False) + "\n\n"
        f"Today's conversation and orders are at the top of this brief. What {n} SAID beats the calendar.\n\n"
        "For each event that matters (skip trivial reminders):\n"
        f"1. Does it still match what {n} said? Times, places, who, what. If {n} said a meeting moved or "
        "runs long, the calendar is wrong: fix it.\n"
        "2. Is it real and verified? Address, opening hours on that day, booking/ticket, the right branch. "
        f"Check the web/{n}'s inbox. Unverified facts go in the event description marked UNVERIFIED.\n"
        "3. Does the day around it work? Travel time with realistic traffic, buffers, meals, a stop that "
        "fits the route, nothing overlapping a work meeting, the car/ride sorted, what to carry or wear.\n"
        f"4. FIX what's reversible yourself, on {n}'s PERSONAL calendar only: move/resize blocks, add a travel "
        "or meal block, delete one that's now wrong, put addresses and links in descriptions. Research. "
        f"Don't book, pay, or message anyone as {n}: if that's needed, that's the one thing for {n}.\n"
        f"5. Don't re-raise anything {n} said they're handling or told you to drop today.\n\n"
        f"SUMMARY RULES (it goes to {n} verbatim on Telegram, only if non-empty): if you changed nothing and "
        f"nothing needs {n}, the summary MUST be an empty string. Otherwise at most 2 short lines: what you "
        "changed ('Moved your 4pm block to 5, the meeting runs long.'), then at most ONE thing "
        f"that needs {n}, with your recommended default. No lists, no process, no 'I checked'."
    )
