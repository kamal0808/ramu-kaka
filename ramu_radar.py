"""Relationship radar: nobody who matters goes quiet by accident.

Rule: no important relationship should go quiet for more than ~21 days by
accident.

people.json holds who matters, their circle, and the last time the user was in
touch. Contact is learned without asking: the user mentions them in chat, the
user's phone is at their place, or a calendar event carries their name. Every
Friday the most overdue 1-3 surface as a note for Ramu Kaka (relationship-radar
skill), which suggests an easy nudge and drafts a message the user can send.

Pure functions; daemon.radar_loop does the I/O.
"""
import re
from datetime import datetime, timedelta

import ramu_profile

# Days before someone counts as drifting, by circle. Loosely Dunbar's layers:
# the closer the circle, the more often contact keeps it alive.
CADENCE = {"close": 7, "friend": 21, "network": 45}

# Starting list of people, from the profile's `people` key, e.g.
#   [{"name": "Asha", "aliases": ["asha's"], "circle": "friend", "place": "Asha's"}]
# More are added as they come up.
SEED = list(ramu_profile.PROFILE.get("people") or [])


def _parse(ts, tz):
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).astimezone(tz)


def _pattern(person):
    names = [person["name"], person["name"].split()[0]] + list(person.get("aliases") or [])
    alts = "|".join(sorted({re.escape(n.lower()) for n in names if n}, key=len, reverse=True))
    return re.compile(rf"(?<![a-z])({alts})(?![a-z])", re.I)


def _touch(person, ts, how):
    cur = person.get("last_contact")
    if cur is None or str(ts) > str(cur):
        person["last_contact"], person["last_how"] = ts, how


def learn(people, turns=(), events=(), calendar=()):
    """Update last_contact from the user's messages, place visits and calendar events.
    Only the USER's words count (Ramu Kaka mentioning someone isn't contact)."""
    people = [dict(p) for p in people]
    pats = [(p, _pattern(p)) for p in people]
    for t in turns:
        if t.get("role") != "user" or not t.get("ts"):
            continue
        text = str(t.get("text") or "")
        for p, pat in pats:
            if pat.search(text):
                _touch(p, t["ts"], "mentioned in chat")
    for ev in events:
        if ev.get("kind") == "place" and ev.get("event") == "enter":
            for p in people:
                if p.get("place") and p["place"].lower() == str(ev.get("place", "")).lower():
                    _touch(p, ev["ts"], f"at {p['place']}")
        if ev.get("kind") == "whatsapp" and not ev.get("group"):
            # a 1:1 chat from them: real contact, the strongest signal we get
            for p, pat in pats:
                if pat.search(str(ev.get("from") or "")):
                    _touch(p, ev["ts"], "WhatsApp")
    for c in calendar:
        title = str(c.get("title") or "")
        for p, pat in pats:
            if pat.search(title) and c.get("start"):
                _touch(p, c["start"], f"calendar: {title[:40]}")
    return people


def overdue(people, now, limit=3):
    """(person, days_quiet, cadence) for the most drifting, worst first.
    Someone never seen yet counts from when they were added."""
    rows = []
    for p in people:
        if p.get("paused"):
            continue
        cadence = int(p.get("cadence_days") or CADENCE.get(p.get("circle"), 21))
        last = _parse(p.get("last_contact") or p.get("added"), now.tzinfo)
        if last is None:
            continue
        days = (now - last).days
        if days >= cadence:
            rows.append((p, days, cadence))
    rows.sort(key=lambda r: r[1] / r[2], reverse=True)
    return rows[:limit]


def radar_note(rows):
    if not rows:
        return None
    bits = "; ".join(f"{p['name']} ({p.get('circle')}, quiet {d}d vs {c}d"
                     f"{', last: ' + p['last_how'] if p.get('last_how') else ''})" for p, d, c in rows)
    return (f"Friday relationship radar. Drifting: {bits}. Use the relationship-radar skill: "
            f"one easy nudge per person with a draft {ramu_profile.NAME} can send, max 3, then stop.")


def seed(now):
    stamp = now.isoformat(timespec="seconds")
    return [{**p, "added": stamp, "source": "seed"} for p in SEED]
