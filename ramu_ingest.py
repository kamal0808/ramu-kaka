"""Device signals: the user's phone, watch and computers reporting in, so Ramu Kaka
knows where they are and what they're doing instead of guessing.

Pure functions, no I/O. daemon.py runs the listener (bound to a private
network address only, e.g. Tailscale, token-protected), appends raw events to a local
log, keeps presence.json current, and turns the few events that matter into
an automatic note for Ramu Kaka.

Senders (any app that can POST JSON works):
  - a phone location logger app: POST /event, JSON {"kind": "location", "lat", "lon",
    "acc", "spd"}. Places are geofenced HERE from places.json, learned when the
    user says "I'm at X"; driving is inferred from speed.
  - an SMS forwarder app: POST /sms, JSON {"from", "text"}.
  - a health-data webhook app: POST /health, its payload (steps, sleep,
    heart rate, exercise) from the phone's health data.
  - an OwnTracks-compatible location app: POST /owntracks, HTTP Basic auth.
All: Authorization: Bearer <token> (or Basic with the token as password).

Why: without it, Ramu Kaka plans a normal day at home while the user is
actually travelling, and has to ask where they are and what they're doing.
"""
import base64
import hmac
import re
from datetime import datetime, timedelta

import ramu_profile

GENERIC_KINDS = ("unlock", "screen_off", "sms", "car", "health", "mac", "location", "place")

PLACE_RADIUS_M = 150
DRIVING_MPS = 7.0      # ~25 km/h: faster than walking or being stuck in traffic
MAX_ACC_M = 100        # ignore fixes worse than this for places/driving

# A first unlock after this long in the dark reads as waking up.
WAKE_GAP = timedelta(hours=3)
STALE_FIX = timedelta(hours=3)  # a place older than the last fix by this much is a guess

_OTP = re.compile(r"\b(otp|one[- ]time|verification code|passcode|do not share)\b", re.I)
_MONEY = re.compile(r"(rs\.?|inr|₹|usd|us\$|\$|eur|€|gbp|£)\s*([\d,]+(?:\.\d{1,2})?)", re.I)
# "Avl Bal 12,345" is the card's balance, not the charge: without this, a small
# charge gets reported as a spend of the whole balance.
_BALANCE = re.compile(r"\b(avl\.?|available|total|outstanding|closing)\s*(bal(ance)?|lmt|limit|credit limit|amt due)\b"
                      r"[^\d]{0,12}[\d,]+(?:\.\d{1,2})?", re.I)
_CURRENCY = {"rs": "INR", "rs.": "INR", "inr": "INR", "₹": "INR", "usd": "USD", "us$": "USD", "$": "USD",
             "eur": "EUR", "€": "EUR", "gbp": "GBP", "£": "GBP"}
_SYMBOL = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}
_DEBIT = re.compile(r"\b(debited|spent|paid|sent|withdrawn|purchase)\b", re.I)
_CREDIT = re.compile(r"\b(credited|received|deposited|refund)\b", re.I)
_LONG_NUM = re.compile(r"\d{5,}")


def authorized(headers, token):
    """Bearer <token>, or HTTP Basic with the token as the password (OwnTracks)."""
    if not token:
        return False
    auth = (headers.get("Authorization") or "").strip()
    if auth.startswith("Bearer "):
        return hmac.compare_digest(auth[7:].strip(), token)
    if auth.startswith("Basic "):
        try:
            _, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(pw, token)
    return False


def _ts(epoch, now):
    try:
        return datetime.fromtimestamp(int(epoch), now.tzinfo)
    except (TypeError, ValueError, OSError, OverflowError):
        return now


def parse_owntracks(payload, now):
    """OwnTracks HTTP payload -> events. Unknown _types are ignored."""
    if not isinstance(payload, dict):
        return []
    kind = payload.get("_type")
    when = _ts(payload.get("tst"), now)
    if kind == "location":
        return [{"kind": "location", "ts": when.isoformat(timespec="seconds"),
                 "lat": payload.get("lat"), "lon": payload.get("lon"), "acc": payload.get("acc"),
                 "regions": payload.get("inregions") or []}]
    if kind == "transition" and payload.get("event") in ("enter", "leave") and payload.get("desc"):
        return [{"kind": "place", "ts": when.isoformat(timespec="seconds"),
                 "event": payload["event"], "place": str(payload["desc"])[:60]}]
    return []


def sms_summary(sender, body):
    """Bank/UPI alert -> {direction, amount, sender, text}; None for OTPs and
    everything that isn't a money alert. The body is kept only with long
    digit runs (account/card numbers) masked."""
    body = str(body or "")
    if not body or _OTP.search(body):
        return None
    m = _MONEY.search(_BALANCE.sub(" ", body))
    if not m:
        return None
    direction = "debit" if _DEBIT.search(body) else "credit" if _CREDIT.search(body) else None
    if direction is None:
        return None
    try:
        amount = float(m.group(2).replace(",", ""))
    except ValueError:
        return None
    out = {"direction": direction, "amount": amount, "currency": _CURRENCY[m.group(1).lower()],
           "sender": str(sender or "")[:40],
            "text": _LONG_NUM.sub("•••", body)[:200]}
    # "spent ... at SOMESHOP on 02-01-2030 at 10:00:00 am": the alert email can land
    # hours later, so keep the merchant and the real time.
    mm = _MERCHANT.search(body)
    if mm:
        out["merchant"] = mm.group(1).strip()[:40]
    tm = _TXN_AT.search(body)
    if tm:
        try:
            # Bank alerts state local time: interpret it in the profile's timezone.
            out["txn_at"] = datetime.strptime(f"{tm.group(1)} {tm.group(2)} {tm.group(3) or ''}".strip(),
                                              "%d-%m-%Y %I:%M:%S %p" if tm.group(3) else "%d-%m-%Y %H:%M:%S"
                                              ).replace(tzinfo=ramu_profile.TZ).isoformat()
        except ValueError:
            pass
    return out


_MERCHANT = re.compile(r"\b(?:at|towards|to)\s+(?:VPA\s+\S+\s+\()?([A-Za-z][A-Za-z0-9 &.'*-]{1,40}?)\)?\s+on\s+\d", re.I)
_TXN_AT = re.compile(r"\bon\s+(\d{2}-\d{2}-\d{4})\s+at\s+(\d{1,2}:\d{2}:\d{2})\s*(am|pm)?", re.I)

# Food delivery = a meal the user is eating; quick-commerce = groceries/snacks,
# not a meal. The merchant names are the profile's `meal_merchants` /
# `grocery_merchants` (regex fragments), with generic defaults.
_DEFAULT_MEAL = ["uber ?eats", "doordash", "deliveroo", "grubhub", "domino", "pizza ?hut", "mcdonald", "kfc", "burger king"]
_DEFAULT_GROCERY = ["instacart", "gopuff", "getir", "gorillas", "amazon fresh"]


def _alts(key, default):
    return re.compile("|".join(ramu_profile.PROFILE.get(key) or default), re.I)


MEAL_MERCHANTS = _alts("meal_merchants", _DEFAULT_MEAL)
GROCERY_MERCHANTS = _alts("grocery_merchants", _DEFAULT_GROCERY)


def order_kind(text):
    """'meal', 'grocery' or None from a merchant name or order title."""
    t = str(text or "")
    return "meal" if MEAL_MERCHANTS.search(t) else "grocery" if GROCERY_MERCHANTS.search(t) else None


def orders_today(food_signals, sms_events, now):
    """Food/grocery orders today, from inbox signals and bank alerts, one per order:
    a delivery app's mail and its card charge within 90 min are the same order."""
    out = []
    for s in food_signals or []:
        at = _parse(s.get("when") or s.get("seen_at"), now)
        if at and at.date() == now.date():
            out.append({"at": at, "what": s.get("title", "food order"), "kind": order_kind(s.get("title")) or "meal"})
    for e in sms_events or []:
        if e.get("direction") != "debit":
            continue
        kind = order_kind(f"{e.get('merchant') or ''} {e.get('text') or ''}")
        at = _parse(e.get("txn_at"), now) or _parse(e.get("ts"), now)
        if not kind or not at or at.date() != now.date():
            continue
        if any(o["kind"] == kind and abs((o["at"] - at).total_seconds()) < 5400 for o in out):
            continue
        out.append({"at": at, "what": f"{e.get('merchant') or kind} order, {_SYMBOL.get(e.get('currency') or '', '')}{e.get('amount', 0):,.0f}", "kind": kind})
    return sorted(out, key=lambda o: o["at"])


def parse_sms_forwarder(payload, now):
    """SMS to URL Forwarder's default body -> the same event /event would make."""
    if not isinstance(payload, dict):
        return []
    return parse_generic({"kind": "sms", "from": payload.get("from"),
                          "body": payload.get("text") or payload.get("body")}, now)


def _iso(ts, now):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(now.tzinfo)
    except (TypeError, ValueError):
        return None


def parse_health(payload, now):
    """Health Connect Webhook payload -> one health event with today's steps,
    the latest finished sleep session, and any workouts. Key names are matched
    loosely (sleep / sleep_sessions, exercise / exercise_sessions)."""
    if not isinstance(payload, dict):
        return []
    ev = {"kind": "health", "ts": now.isoformat(timespec="seconds")}
    steps = 0
    for r in payload.get("steps") or []:
        end = _iso(r.get("end_time") or r.get("start_time"), now)
        start = _iso(r.get("start_time"), now)
        if isinstance(r.get("count"), (int, float)) and start and start.date() == now.date():
            steps += int(r["count"])
        elif isinstance(r.get("count"), (int, float)) and end and end.date() == now.date() and not start:
            steps += int(r["count"])
    if steps:
        ev["steps"] = steps
    sleeps = [r for k, v in payload.items() if "sleep" in k and isinstance(v, list) for r in v if isinstance(r, dict)]
    ends = [(e, r) for r in sleeps if (e := _iso(r.get("session_end_time") or r.get("end_time"), now))]
    if ends:
        end, r = max(ends, key=lambda x: x[0])
        secs = r.get("duration_seconds")
        ev["sleep_end"] = end.isoformat(timespec="minutes")
        if isinstance(secs, (int, float)):
            ev["sleep_hours"] = round(secs / 3600, 1)
    workouts = [r for k, v in payload.items() if "exercise" in k and isinstance(v, list) for r in v if isinstance(r, dict)]
    today = [r for r in workouts if (s := _iso(r.get("start_time"), now)) and s.date() == now.date()]
    if today:
        ev["workouts_today"] = len(today)
        mins = sum(int(r.get("duration_seconds") or 0) for r in today) // 60
        if mins:
            ev["workout_minutes"] = mins
    return [ev] if len(ev) > 2 else []


def _meters(a_lat, a_lon, b_lat, b_lon):
    import math
    r = 6371000
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def nearest_place(places, lat, lon):
    """(name, metres) of the closest known place within its radius, else (None, None)."""
    best = (None, None)
    for name, pl in (places or {}).items():
        try:
            d = _meters(float(lat), float(lon), float(pl["lat"]), float(pl["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
        if d <= float(pl.get("r", PLACE_RADIUS_M)) and (best[1] is None or d < best[1]):
            best = (name, d)
    return best


_PLACE_SAID = re.compile(
    r"\b(?:i'?m|i am|im|am|reached|back)\s+(?:still\s+|now\s+|just\s+)?(?:at|in|to)?\s*"
    r"((?:the\s+)?[a-z][a-z']{1,20}(?:\s+(?:place|house|home|gym|office|cafe))?)\b", re.I)
_NOT_PLACES = {"the", "a", "an", "it", "this", "that", "work", "here", "there", "going", "coming",
               "leaving", "on", "out", "so", "not", "tired", "done", "fine", "ok", "okay", "good",
               "back", "still", "now", "just", "in", "at", "to", "home now"}


def place_from_text(text):
    """"i'm still at sam's" -> "Sam's"; "reached gym" -> "Gym"; "back home" -> "Home".
    None when it doesn't read as a place."""
    t = str(text or "")
    if re.search(r"\b(back home|home now|reached home|at home|i'?m home)\b", t, re.I):
        return "Home"
    m = _PLACE_SAID.search(t)
    if not m:
        return None
    name = re.sub(r"^the\s+", "", m.group(1).strip(), flags=re.I)
    if name.lower() in _NOT_PLACES or len(name) < 3:
        return None
    return name[0].upper() + name[1:]


def fresh_fix(presence, now, minutes=15):
    """The last location fix, if recent and decent enough to pin a place on."""
    loc = (presence or {}).get("last_location") or {}
    t = _parse(loc.get("ts"), now)
    acc = _num(loc.get("acc"))
    if t is None or now - t > timedelta(minutes=minutes) or (acc is not None and acc > MAX_ACC_M):
        return None
    return loc


def learn_place(places, name, presence):
    """The user said "I'm at <name>": pin their last good fix as that place."""
    loc = (presence or {}).get("last_location") or {}
    try:
        lat, lon = float(loc["lat"]), float(loc["lon"])
    except (KeyError, TypeError, ValueError):
        return places, False
    name = str(name).strip()[:40]
    if not name:
        return places, False
    return {**(places or {}), name: {"lat": lat, "lon": lon, "r": PLACE_RADIUS_M}}, True


def email_text(body):
    """Bank alert email HTML -> plain text (styles, tags and entities stripped)."""
    import html as _html
    t = re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1>", " ", str(body or ""))
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", _html.unescape(t)).strip()


def parse_bank_email(sender, subject, body, ts, now):
    """A bank's transaction-alert email -> the same money event an SMS would give."""
    s = sms_summary(sender, f"{subject}. {email_text(body)}")
    if s is None:
        return []
    when = _iso(ts, now) or now
    return [{"kind": "sms", "ts": when.isoformat(timespec="seconds"), "via": "email", **s}]


WHATSAPP_PACKAGES = ("com.whatsapp", "com.whatsapp.w4b")
_WA_NOISE = re.compile(r"^(whatsapp|\d+ new messages?|checking for new messages|backup|calling|ringing|"
                       r"missed (voice|video) call|incoming (voice|video) call|ongoing (voice|video) call)", re.I)


def parse_notification(payload, now):
    """A forwarded phone notification -> a WhatsApp contact event, or [].
    Only WhatsApp is kept, only who and when plus a short preview; everything
    else (OTPs, other apps) is dropped here and never stored."""
    if not isinstance(payload, dict):
        return []
    pkg = str(payload.get("packageName") or payload.get("package") or payload.get("app") or "")
    if pkg not in WHATSAPP_PACKAGES:
        return []
    title = str(payload.get("title") or "").strip()
    text = str(payload.get("text") or "").strip()
    if not title or _WA_NOISE.match(title):
        return []
    # Groups show "Group name" as title and "Sender: message" as text (or "Group: Sender").
    group, sender = None, title
    if ": " in title:
        group, sender = title.split(": ", 1)
    elif re.match(r"^[^:]{1,40}: ", text):
        group, (sender, text) = title, text.split(": ", 1)
    sender = re.sub(r"\s*\(\d+ messages?\)$", "", sender).strip()[:60]
    when = _ts(int(payload["postedAt"]) // 1000, now) if str(payload.get("postedAt", "")).isdigit() else now
    return [{"kind": "whatsapp", "ts": when.isoformat(timespec="seconds"), "from": sender,
             "group": (group or "")[:60] or None, "preview": _LONG_NUM.sub("•••", text)[:80]}]


def parse_call_record(payload, now):
    """The voice-call service's post-call record (client ramu-kaka) -> one call event."""
    if not isinstance(payload, dict) or payload.get("client") not in (None, "ramu-kaka"):
        return []
    summary = str(payload.get("summary") or "").strip()
    transcript = str(payload.get("transcript") or "").strip()
    if not (summary or transcript):
        return []
    return [{"kind": "call", "ts": now.isoformat(timespec="seconds"),
             "duration_s": payload.get("duration_sec"), "summary": summary[:600],
             "transcript": transcript[-1500:]}]


def parse_generic(payload, now):
    """Tasker-style {"kind": ...} -> one event, or [] if unusable."""
    if not isinstance(payload, dict) or payload.get("kind") not in GENERIC_KINDS:
        return []
    ev = {"kind": payload["kind"], "ts": now.isoformat(timespec="seconds")}
    k = payload["kind"]
    if k == "sms":
        s = sms_summary(payload.get("from"), payload.get("body"))
        if s is None:
            return []  # OTPs and chatter never get stored
        ev.update(s)
    elif k == "car":
        ev["device"] = str(payload.get("device") or "")[:40]
        ev["state"] = "connected" if payload.get("state") in ("connected", "on", True) else "disconnected"
    elif k == "place":
        if payload.get("event") not in ("enter", "leave") or not payload.get("place"):
            return []
        ev["event"], ev["place"] = payload["event"], str(payload["place"])[:60]
    elif k == "mac":
        try:
            ev["idle_s"] = max(0, int(payload.get("idle_s", 0)))
            ev["interval_s"] = min(900, max(60, int(payload.get("interval_s", 300))))
        except (TypeError, ValueError):
            return []
        ev["host"] = str(payload.get("host") or "mac")[:40]
        ev["app"] = str(payload.get("app") or "")[:40]
    elif k in ("health", "location"):
        ev.update({key: payload[key] for key in list(payload)[:12]
                   if key != "kind" and isinstance(payload[key], (int, float, str, bool))})
    return [ev]


def apply(presence, ev, now, places=None):
    """Fold an event into presence. Returns (new_presence, note or None).
    A note is a one-line description of something worth Ramu Kaka's attention."""
    p = dict(presence or {})
    if ev["kind"] == "location":
        return _apply_location(p, ev, now, places)
    k, note = ev["kind"], None
    hhmm = now.strftime("%H:%M")
    if k == "place":
        if ev["event"] == "enter":
            if p.get("place") != ev["place"]:
                note = f"{ramu_profile.NAME}'s phone arrived at '{ev['place']}' at {hhmm}."
            p.update(place=ev["place"], place_since=ev["ts"], place_said=False)
        elif p.get("place") == ev["place"]:
            note = f"{ramu_profile.NAME}'s phone left '{ev['place']}' at {hhmm}."
            p.update(place=None, place_since=ev["ts"], left=ev["place"], place_said=False)
    elif k == "unlock":
        off = _parse(p.get("last_screen_off"), now)
        # 05:00-16:00 only: an unlock in the middle of the night isn't the morning.
        if (off is not None and now - off >= WAKE_GAP and 5 <= now.hour < 16
                and p.get("awake_since_today") != now.date().isoformat()):
            note = f"{ramu_profile.NAME} just unlocked the phone at {hhmm} after {int((now - off).total_seconds() // 3600)}h dark: likely awake."
            p["awake_since_today"] = now.date().isoformat()
            p["woke_at"] = ev["ts"]
            p["slept_from"] = p.get("last_screen_off")
        p["last_unlock"] = ev["ts"]
    elif k == "screen_off":
        p["last_screen_off"] = ev["ts"]
    elif k == "sms":
        word = "spent" if ev["direction"] == "debit" else "received"
        sym = _SYMBOL.get(ev.get("currency") or "INR", ev.get("currency", "") + " ")
        amt = f"{ev['amount']:,.2f}" if ev.get("currency", "INR") != "INR" else f"{ev['amount']:,.0f}"
        note = f"Bank alert: {word} {sym}{amt} ({ev['sender']})."
    elif k == "car":
        p["driving"] = ev["state"] == "connected"
        if p["driving"]:
            note = f"{ramu_profile.NAME}'s phone connected to the car ({ev['device'] or 'car'}) at {hhmm}: driving."
    elif k == "whatsapp":
        p["last_whatsapp"] = ev["ts"]
    elif k == "call":
        note = (f"A phone call between Ramu Kaka and {ramu_profile.NAME} just ended ({ev.get('duration_s') or '?'}s). "
                f"Summary: {ev['summary'] or '(none)'} Transcript tail: {ev['transcript'][-700:]} "
                f"Act on what {ramu_profile.NAME} said (log, promise, task), and don't re-ask what was already "
                "answered on the call.")
    elif k == "health":
        old_end = (p.get("health") or {}).get("sleep_end")
        p["health"] = {**p.get("health", {}), **{x: ev[x] for x in ev if x not in ("kind", "ts")}, "ts": ev["ts"]}
        end = _parse(ev.get("sleep_end"), now)
        if end and ev.get("sleep_end") != old_end and now - end < timedelta(hours=6):
            hrs = f" after {ev['sleep_hours']}h sleep" if ev.get("sleep_hours") else ""
            note = f"Watch says {ramu_profile.NAME} woke at {end.strftime('%H:%M')}{hrs}."
            p["woke_at"] = end.isoformat(timespec="minutes")
    elif k == "mac":
        p, note = _apply_mac(p, ev, now)
    p["updated"] = ev["ts"]
    return p, note


LATE_HOURS = (1, 5)          # active at the Mac in this window = working too late
LONG_DAY_MARKS = (480, 600)  # minutes: 8h and 10h at the Mac


def _apply_mac(p, ev, now):
    """Accumulate active minutes per 'work day' (which rolls over at 5am, so a
    2am session counts toward the day it belongs to)."""
    day = (now - timedelta(hours=5)).date().isoformat()
    m = p.get("mac_day") or {}
    if m.get("day") != day:
        m = {"day": day, "active_min": 0, "apps": {}, "noted": []}
    active = ev["idle_s"] < ev["interval_s"] and ev["app"] not in ("loginwindow", "ScreenSaverEngine", "")
    note = None
    if active:
        last = _parse(m.get("last_credit"), now)
        # One or more computers reporting the same 5 minutes shouldn't count twice.
        if last is None or now - last >= timedelta(seconds=ev["interval_s"] * 0.8):
            mins = ev["interval_s"] // 60
            m["active_min"] = m.get("active_min", 0) + mins
            m["apps"][ev["app"]] = m["apps"].get(ev["app"], 0) + mins
            m["last_credit"] = now.isoformat(timespec="seconds")
        m.setdefault("first", now.isoformat(timespec="minutes"))
        m["last"] = now.isoformat(timespec="minutes")
        hrs = m["active_min"] / 60
        top = max(m["apps"], key=m["apps"].get) if m["apps"] else ev["app"]
        if LATE_HOURS[0] <= now.hour < LATE_HOURS[1]:
            # Recorded, not pinged: it's always inside quiet hours, and a "you're up
            # late" arriving at 8am is noise. The morning chat and weekly reset see it.
            m["late_until"] = now.isoformat(timespec="minutes")
        for mark in LONG_DAY_MARKS:
            if m["active_min"] >= mark and f"{mark}" not in m["noted"]:
                m["noted"].append(f"{mark}")
                note = note or (f"{ramu_profile.NAME} has been at the Mac {mark // 60}h today (mostly {top}), "
                                f"since {m['first'][11:16]}.")
    p["mac_day"] = m
    p["updated"] = ev["ts"]
    return p, note


def _apply_location(p, ev, now, places):
    hhmm = now.strftime("%H:%M")
    try:
        lat, lon = float(ev.get("lat")), float(ev.get("lon"))
    except (TypeError, ValueError):
        return p, None
    acc = _num(ev.get("acc"))
    p["last_location"] = {"ts": ev["ts"], "lat": lat, "lon": lon, "acc": acc}
    p["updated"] = ev["ts"]
    if acc is not None and acc > MAX_ACC_M:
        return p, None
    notes = []
    spd = _num(ev.get("spd"))
    if spd is not None:
        driving = spd >= DRIVING_MPS
        if driving and not p.get("driving"):
            notes.append(f"{ramu_profile.NAME} is moving at driving speed ({spd * 3.6:.0f} km/h) since {hhmm}.")
        p["driving"] = driving
    here, _ = nearest_place(places, lat, lon)
    was = p.get("place")
    if here != was:
        if was and not here:
            # hysteresis: only "left" once clearly outside, not on GPS jitter at the edge
            pl = (places or {}).get(was)
            if pl and _meters(lat, lon, float(pl["lat"]), float(pl["lon"])) < 1.5 * float(pl.get("r", PLACE_RADIUS_M)):
                return p, (" ".join(notes) or None)
            notes.append(f"{ramu_profile.NAME}'s phone left '{was}' at {hhmm}.")
            p.update(place=None, place_since=ev["ts"], left=was, place_said=False)
        elif here:
            notes.append(f"{ramu_profile.NAME}'s phone arrived at '{here}' at {hhmm}.")
            p.update(place=here, place_since=ev["ts"], place_said=False)
    return p, (" ".join(notes) or None)


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _parse(ts, now):
    try:
        return datetime.fromisoformat(str(ts)).astimezone(now.tzinfo)
    except (TypeError, ValueError):
        return None


def _ago(ts, now):
    t = _parse(ts, now)
    if t is None:
        return None
    mins = int((now - t).total_seconds() // 60)
    return f"{mins}m ago" if mins < 90 else f"{mins // 60}h ago" if mins < 48 * 60 else f"{mins // 1440}d ago"


def presence_block(p, now):
    """One line of live facts for every prompt. Empty when nothing reports in."""
    if not p:
        return ""
    bits = []
    fix = _parse((p.get("last_location") or {}).get("ts"), now)
    gps_dead = fix is not None and now - fix > STALE_FIX
    if p.get("place") and gps_dead and not p.get("place_said"):
        bits.append(f"last known at '{p['place']}' {_ago(p.get('place_since'), now)}, but location hasn't "
                    f"reported for {_ago(fix.isoformat(), now)[:-4]}: where {ramu_profile.NAME} is now is UNKNOWN "
                    "(what they say in chat wins; don't build anything on them still being there)")
    elif p.get("place") and p.get("place_said"):
        bits.append(f"said they're at '{p['place']}' {_ago(p.get('place_since'), now)}")
    elif p.get("place"):
        bits.append(f"at '{p['place']}' since {_ago(p.get('place_since'), now)}")
    elif p.get("left"):
        bits.append(f"left '{p['left']}' {_ago(p.get('place_since'), now)}, not at a known place")
    if p.get("driving"):
        bits.append("in the car now")
    if p.get("woke_at"):
        bits.append(f"woke ~{_ago(p['woke_at'], now)}")
    if p.get("last_unlock"):
        bits.append(f"phone last unlocked {_ago(p['last_unlock'], now)}")
    m = p.get("mac_day") or {}
    if m.get("active_min") and m.get("day") == (now - timedelta(hours=5)).date().isoformat():
        top = ", ".join(f"{a} {v / 60:.1f}h" for a, v in sorted(m["apps"].items(), key=lambda x: -x[1])[:2])
        bits.append(f"{m['active_min'] / 60:.1f}h at the Mac today ({top}), last active {_ago(m.get('last'), now)}"
                    + (f", worked till {m['late_until'][11:16]} last night" if m.get("late_until") else ""))
    h = p.get("health") or {}
    for key in ("sleep_hours", "steps", "workout_minutes"):
        if key in h:
            bits.append(f"{key.replace('_', ' ')} {h[key]}")
    if not bits:
        return ""
    upd = _parse(p.get("updated"), now)
    if upd and now - upd > timedelta(hours=3):
        bits.append(f"BUT the phone last reported {_ago(p['updated'], now)}, so this may be stale")
    return (f"Device signals (live, from {ramu_profile.NAME}'s own phone/watch; trust these over guesses, "
            f"never over what {ramu_profile.NAME} tells you): " + "; ".join(bits) + ".\n---\n")
