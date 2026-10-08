"""Tap-to-answer daily check-ins: Ramu Kaka asks, the user taps, nothing to type.

Pure functions, no I/O. State is derived from the conversation log:
  - a question sent carries "checkin_q": <key>
  - a tap the user made is recorded as a user turn carrying "checkin": <key>
so what has been answered today is always recoverable and there is no extra
runtime file to keep in sync.

Flow: the daemon opens ONE check-in (silently, through the ping gate). Each tap
triggers the next due question, up to a chain limit, so a few questions get
answered from one ping and nothing piles up while the user is away.
"""
import re
from datetime import datetime, timedelta

PREFIX = "ci:"

# key -> when it becomes due (local hour, profile timezone), the question, options, and where the
# answer is logged (an hq_task_ids key in daemon.py).
CHECKINS = {
    "sleep": {
        "after": 10.0, "hq": "sleep", "label": "Sleep",
        "q": "How did you sleep?",
        "options": [("Good", "good"), ("Okay", "okay"), ("Bad", "bad"), ("Barely slept", "barely")],
    },
    "lunch": {
        "after": 13.5, "hq": "nutrition", "label": "Lunch",
        "q": "Had lunch?",
        "options": [("Proper meal", "meal"), ("Just snacks", "snacks"), ("Not yet", "none")],
    },
    "energy": {
        "after": 16.0, "hq": "habits", "label": "Energy",
        "q": "Energy right now?",
        "options": [("High", "high"), ("Okay", "okay"), ("Low", "low")],
    },
    "move": {
        "after": 18.0, "hq": "training", "label": "Movement",
        "q": "Move your body today?",
        "options": [("Trained", "trained"), ("Walked", "walked"), ("Not today", "none")],
    },
    "dinner": {
        "after": 20.5, "hq": "nutrition", "label": "Dinner",
        "q": "Dinner sorted?",
        "options": [("Yes", "meal"), ("Snacks only", "snacks"), ("Not yet", "none")],
    },
}
ORDER = ["sleep", "lunch", "energy", "move", "dinner"]
SKIP = ("Skip", "skip")


def _parse(ts, tz):
    try:
        return datetime.fromisoformat(str(ts)).astimezone(tz)
    except (TypeError, ValueError):
        return None


def answered_today(turns, now, meals_at=()):
    """Keys already asked or answered today: a question is never repeated the same day.

    A tap is not the only answer: if the user already said it in their own words
    ("3 sets of squats", "had a sandwich", "slept well"), that counts too.
    """
    out = set()
    for t in turns:
        key = t.get("checkin") or t.get("checkin_q")
        if not key:
            continue
        ts = _parse(t.get("ts"), now.tzinfo)
        if ts is not None and ts.date() == now.date():
            out.add(key)
    return out | said_today(turns, now) | _meal_already_raised(turns, now) | _ordered(meals_at, now)


def _ordered(meals_at, now):
    """A food-delivery order in the meal's window answers "Had lunch?" for the user."""
    out = set()
    for at in meals_at or ():
        if at.date() != now.date():
            continue
        hour = at.hour + at.minute / 60
        for key in ("lunch", "dinner"):
            lo, hi = _WINDOW[key]
            if lo <= hour < hi:
                out.add(key)
    return out


def _meal_already_raised(turns, now):
    """Meals Ramu Kaka already brought up itself today ("have a snack now"):
    following that with "Had lunch?" minutes later reads as not listening."""
    out = set()
    for t in turns:
        # Replies only: a day plan's "dinner at 8" isn't a conversation about food.
        if t.get("role") != "ramu_kaka" or t.get("checkin_q") or t.get("unsolicited"):
            continue
        ts = _parse(t.get("ts"), now.tzinfo)
        if ts is None or ts.date() != now.date():
            continue
        hour = ts.hour + ts.minute / 60
        for key in ("lunch", "dinner"):
            lo, hi = _WINDOW[key]
            if lo <= hour < hi and re.search(_FOOD_TALK, str(t.get("text") or ""), re.I):
                out.add(key)
    return out


def in_conversation(turns, now, minutes=20):
    """The user is talking to Ramu Kaka right now: a canned check-in would cut
    across the conversation."""
    for t in reversed(turns):
        if t.get("checkin") or t.get("checkin_q"):
            continue
        if t.get("role") == "user" or (t.get("role") == "ramu_kaka" and not t.get("unsolicited")):
            ts = _parse(t.get("ts"), now.tzinfo)
            if ts is not None:
                return timedelta(0) <= now - ts <= timedelta(minutes=minutes)
    return False


# Evidence in the user's own words, per check-in. Past/present facts only: "will go to
# gym" is a plan, not an answer, so plain "gym" alone does not count.
_FOOD = r"\b(ate|eaten|lunch|dinner|meal|rice|noodles|pasta|pizza|salad|sandwich|soup|breakfast|snack)\b"
_FOOD_TALK = _FOOD[:-3] + r"|snack|milk|eat|eating|food|ordered)\b"
_SAID = {
    "sleep": [r"\bslept\b", r"\bsleep\b.*\b(good|well|bad|okay|ok|hrs?|hours)\b",
              r"\b\d+\s*(hrs?|hours)\b.*\bsleep", r"\bwoke up at\b"],
    "move": [r"\b\d+\s*sets?\b", r"\breps?\b", r"\b\d+(\.\d+)?\s*kgs?\b", r"\bcalories\b",
             r"\b(trained|worked out|workout done|gym done|in the gym|i'?m in the gym|ran|walked)\b",
             r"\b(treadmill|dumbb?ell|squats?|curls?|yoga|went for a (run|walk)|cycling|plank)\b",
             r"\b(rest day)\b"],
    "lunch": [_FOOD],
    "dinner": [_FOOD],
}
# Meals are told apart by when the user said it.
_WINDOW = {"lunch": (11.0, 17.0), "dinner": (17.0, 24.0)}


def said_today(turns, now):
    """Check-ins the user already answered in free text (or by replying to the question) today."""
    questions = {CHECKINS[k]["q"]: k for k in CHECKINS}
    out = set()
    for t in turns:
        if t.get("role") != "user" or t.get("checkin"):
            continue
        ts = _parse(t.get("ts"), now.tzinfo)
        if ts is None or ts.date() != now.date():
            continue
        text = str(t.get("text") or "")
        m = re.match(r'\[replying to: "([^"]*)"\]', text)
        if m and m.group(1) in questions:
            out.add(questions[m.group(1)])
        body = re.sub(r'^\[replying to: "[^"]*"\]\s*', "", text).lower()
        hour = ts.hour + ts.minute / 60
        for key, pats in _SAID.items():
            lo, hi = _WINDOW.get(key, (0.0, 24.0))
            if lo <= hour < hi and any(re.search(p, body) for p in pats):
                out.add(key)
    return out


def due(now, answered):
    """Check-ins whose time has come and that are not answered yet, in order."""
    hour = now.hour + now.minute / 60
    return [k for k in ORDER if CHECKINS[k]["after"] <= hour and k not in answered]


def recent_questions(turns, now, minutes=30):
    """How many check-in questions went out lately: the chain-length guard."""
    n = 0
    for t in turns:
        if not t.get("checkin_q"):
            continue
        ts = _parse(t.get("ts"), now.tzinfo)
        if ts is not None and now - ts <= timedelta(minutes=minutes):
            n += 1
    return n


def build(key):
    """(question text, inline-keyboard rows). Options on one row, skip below."""
    c = CHECKINS[key]
    row = [[label, f"{PREFIX}{key}:{val}"] for label, val in c["options"]]
    return c["q"], [row, [[SKIP[0], f"{PREFIX}{key}:{SKIP[1]}"]]]


def parse(text):
    """'ci:sleep:bad' -> ('sleep', 'bad'); anything else -> None."""
    if not isinstance(text, str) or not text.startswith(PREFIX):
        return None
    parts = text[len(PREFIX):].split(":")
    if len(parts) != 2 or parts[0] not in CHECKINS:
        return None
    key, val = parts
    if val != SKIP[1] and val not in dict((v, l) for l, v in CHECKINS[key]["options"]):
        return None
    return key, val


def describe(key, val):
    """(human line for the log, plain factual note for the HQ comment)."""
    c = CHECKINS[key]
    if val == SKIP[1]:
        return f"{c['label']}: skipped", None
    label = dict((v, l) for l, v in c["options"])[val].lower()
    return f"{c['label']}: {label}", f"{c['label']}: {label} (tap check-in)."
