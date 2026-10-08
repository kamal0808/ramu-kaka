"""Self-improvement loop: every time the user is frustrated with Ramu Kaka, that's
a bug report. Catch it, remember it, diagnose it, fix it for real.

Foreground quality signals -> improvement opportunities:
  - detect(): the user's words -> friction categories (no model call)
  - record(): a friction with what Ramu Kaka said just before
  - block(): open frictions in every prompt, so it stops repeating them now
  - the overnight shift diagnoses each one (fix: memory / skill / code)
  - coding sessions read frictions.json first and mark fixes with the commit
  - weekly(): "what annoyed you this week, what got fixed"
"""
import re
from datetime import datetime, timedelta

import ramu_profile

CATEGORIES = {
    "re-asked": r"already (told|said|answered|mentioned)|i told (u|you)|why .{0,20}ask.{0,20}again|"
                r"same question|asked (that|this) (already|before)",
    "forgot context": r"you forgot|u forgot|(don'?t|dont|do not) (u|you) remember|we (just )?(talked|went over)|"
                      r"you'?re not listening|not listening|supposed to (know|remember)",
    "spam": r"stop messaging|stop texting|too many (messages|questions|notifications)|wall of (text|questions)|"
            r"so many messages|leave me alone",
    "not proactive": r"not helpful|unhelpful|this is useless|you'?re useless|of no use|no help|"
                     r"why can'?t (u|you)|won'?t help|not doing anything|nothing (is )?(getting )?done",
    "wrong info": r"that'?s (wrong|not right|incorrect)|is wrong|that'?s not true|doesn'?t exist|"
                  r"could(n'?t| not) find|made (that|it) up",
    "didn't understand": r"you (don'?t|do not|didn'?t) understand|not what i (meant|asked)|"
                         r"that'?s not what i|you misunderstood|missed the point",
    "anger": r"\b(dumb|stupid|wtf|fuck\w*|damn|ridiculous)\b",
}
_COMPILED = {k: re.compile(v, re.I) for k, v in CATEGORIES.items()}

# The user is low, not asking for a plan: answering "rough day" with a plan,
# a to-do or a canned check-in reads as not listening.
_LOW = re.compile(
    r"^\W*rough\W*$|\brough (one|day|night|week|time)\b|\b(bad|terrible|awful|horrible|long|hard) (day|night|week)\b|"
    r"\b(i'?m|i am|feeling|feel|felt)\s+(really |so |very |pretty )?(not (good|ok|okay|well|fine)|low|down|sad|"
    r"anxious|exhausted|drained|overwhelmed|tired|worn out|burnt out|burned out|stressed)\b|"
    r"\b(can'?t cope|feeling low|exhausted|overwhelmed|too much going on|need a break)\b", re.I)


def sounds_low(text):
    """The user's own words say they're low (not frustrated with Ramu Kaka, low)."""
    t = re.sub(r'^\[replying to: "[^"]*"\]\s*', "", str(text or ""))
    return len(t) <= 300 and bool(_LOW.search(t))


def detect(text):
    """The user's message -> sorted list of friction categories ([] = none)."""
    t = re.sub(r'^\[replying to: "[^"]*"\]\s*', "", str(text or ""))
    if len(t) > 600:  # long pastes/briefs aren't frustration
        return []
    return sorted(k for k, rx in _COMPILED.items() if rx.search(t))


def record(frictions, text, cats, ts, prior_reply):
    """Append a friction unless this exact message is already recorded."""
    frictions = list(frictions or [])
    if any(f.get("ts") == ts and f.get("said") == text for f in frictions):
        return frictions, None
    f = {"id": f"f{len(frictions) + 1:03d}", "ts": ts, "said": str(text)[:300],
         "after": str(prior_reply or "")[:300], "categories": cats, "status": "open",
         "diagnosis": None, "fix": None, "fixed_in": None}
    return frictions + [f], f


def backfill(turns):
    """Frictions from a whole conversation log, oldest first."""
    frictions, prior = [], ""
    for t in turns:
        if t.get("role") == "ramu_kaka":
            prior = t.get("text", "")
        elif t.get("role") == "user":
            cats = detect(t.get("text"))
            if cats:
                frictions, _ = record(frictions, t.get("text", ""), cats, t.get("ts"), prior)
    return frictions


def _parse(ts, tz):
    try:
        return datetime.fromisoformat(str(ts)).astimezone(tz)
    except (TypeError, ValueError):
        return None


def repeats(frictions, now, days=14):
    """category -> count of open frictions in the last `days`; 3+ is a pattern."""
    counts = {}
    for f in frictions or []:
        t = _parse(f.get("ts"), now.tzinfo)
        if f.get("status") == "open" and t and now - t <= timedelta(days=days):
            for c in f.get("categories") or []:
                counts[c] = counts.get(c, 0) + 1
    return counts


def block(frictions, now, limit=5):
    """The latest open frictions, for every prompt: don't do these again."""
    open_ = [f for f in frictions or [] if f.get("status") == "open"]
    if not open_:
        return ""
    lines = "\n".join(
        f"- {f['ts'][:10]} [{', '.join(f['categories'])}] {ramu_profile.NAME} said \"{f['said'][:120]}\""
        + (f" after you said \"{f['after'][:100]}\"" if f.get("after") else "")
        + (f" -> lesson: {f['fix']}" if f.get("fix") else "")
        for f in open_[-limit:])
    pats = ", ".join(f"{c} x{n}" for c, n in sorted(repeats(frictions, now).items(), key=lambda x: -x[1]) if n >= 3)
    return (f"Things that frustrated {ramu_profile.NAME} recently (open; don't repeat them"
            + (f"; recurring: {pats}" if pats else "") + "):\n" + lines + "\n---\n")


def weekly(frictions, now):
    """One line for the weekly reset."""
    week = [f for f in frictions or [] if (t := _parse(f.get("ts"), now.tzinfo)) and now - t <= timedelta(days=7)]
    if not week:
        return "No frustrations logged this week."
    fixed = [f for f in week if f.get("status") == "fixed"]
    cats = {}
    for f in week:
        for c in f.get("categories") or []:
            cats[c] = cats.get(c, 0) + 1
    top = ", ".join(f"{c} x{n}" for c, n in sorted(cats.items(), key=lambda x: -x[1])[:3])
    return f"Frustrations this week: {len(week)} ({top}); fixed {len(fixed)}, open {len(week) - len(fixed)}."
