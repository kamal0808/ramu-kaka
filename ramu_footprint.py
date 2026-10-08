"""Digital footprint: what the user's inbox says about their life, beyond bank alerts.

Tickets and bookings, orders and deliveries, food orders, subscriptions and
renewals, bills due.

Flow (daemon.footprint_loop): a narrow Gmail query pulls candidate mails,
one Sonnet call classifies only the NEW ones into signals, signals are kept in
footprint.json, the live ones go into every prompt, and the important new ones
wake Ramu Kaka as an automatic note (trip-mode for a ticket, money-desk for a
renewal, nutrition for a food order).

Pure functions; no I/O except what the caller passes in.
"""
import json
import re
from datetime import datetime, timedelta

import ramu_profile

# Narrow on purpose: senders and subjects that carry life signals. Newsletters
# and bank alerts (handled by bank_mail_loop) stay out. The sender list is the
# profile's `footprint_senders` (the travel, shopping, food, ticketing and
# utility services the user actually gets mail from), with a generic default.
_DEFAULT_SENDERS = [
    "booking.com", "airbnb.com", "expedia.com", "amazon.com", "ebay.com", "uber.com",
    "doordash.com", "ticketmaster.com", "netflix.com", "spotify.com", "google.com", "apple.com",
]
_SENDERS = ramu_profile.PROFILE.get("footprint_senders") or _DEFAULT_SENDERS
_BANKS = ramu_profile.PROFILE.get("bank_alert_senders") or []
_NOT_BANKS = f"-from:({' OR '.join(_BANKS)}) " if _BANKS else ""
QUERY = (
    f"newer_than:2d {_NOT_BANKS}("
    f"from:({' OR '.join(_SENDERS)}) "
    "OR subject:(ticket OR booking OR PNR OR itinerary OR shipped OR \"out for delivery\" OR delivered "
    "OR renewal OR \"auto-pay\" OR mandate OR subscription OR \"bill due\" OR \"payment due\" OR invoice))"
)

TYPES = ("travel", "order", "food", "subscription", "bill", "event", "other")
KEEP_DAYS = 10


def compact(messages, seen, text_of):
    """New mails -> short records for the classifier. `text_of` turns HTML into text."""
    out = []
    for m in messages or []:
        mid = m.get("messageId") or m.get("id")
        if not mid or mid in seen:
            continue
        body = re.sub(r"\d{9,}", "•••", text_of(m.get("messageText") or ""))[:600]
        out.append({"id": mid, "from": (m.get("sender") or "")[:60], "subject": (m.get("subject") or "")[:120],
                    "at": m.get("messageTimestamp"), "text": body})
    return out


def prompt(mails, now):
    name = ramu_profile.NAME
    tz = now.tzname() or str(ramu_profile.TZ)
    return (
        f"Classify these emails from {name}'s personal inbox into life signals. Today is "
        f"{now.strftime('%a %d %b %Y %H:%M')} {tz}. For each email that matters to {name}'s life, output one "
        "signal; skip marketing, newsletters, OTPs and anything with no concrete action or event.\n"
        f"type: one of {', '.join(TYPES)} (food = a food delivery order: that's a meal {name} is eating).\n"
        "title: <=10 plain words, e.g. 'Flight to Lisbon, booking ending 4821' or 'Parcel out for delivery'.\n"
        f"when: ISO date/time of the THING (departure, delivery, renewal charge, due date), {tz}, or null.\n"
        f"amount: number (in {name}'s local currency) or null. importance: high (needs {name} to know or act "
        f"within ~48h: travel, a charge {name} may not expect, a bill due soon, a delivery today), normal, or low.\n"
        "Output ONLY JSON: {\"signals\": [{\"id\": \"<email id>\", \"type\": \"...\", \"title\": \"...\", "
        "\"when\": \"...\", \"amount\": null, \"importance\": \"...\"}]}\n\n"
        + json.dumps(mails, ensure_ascii=False)
    )


def parse(out, mails):
    """Classifier output -> clean signals. Unknown ids and junk are dropped."""
    try:
        start, end = out.index("{"), out.rindex("}") + 1
        data = json.loads(out[start:end])
    except (ValueError, AttributeError):
        return []
    ids = {m["id"] for m in mails}
    sigs = []
    for s in data.get("signals") or []:
        if not isinstance(s, dict) or s.get("id") not in ids or s.get("type") not in TYPES:
            continue
        title = str(s.get("title") or "").strip()[:100]
        if not title:
            continue
        amt = s.get("amount")
        sigs.append({
            "id": s["id"], "type": s["type"], "title": title,
            "when": s.get("when") if isinstance(s.get("when"), str) else None,
            "amount": amt if isinstance(amt, (int, float)) else None,
            "importance": s.get("importance") if s.get("importance") in ("high", "normal", "low") else "normal",
        })
    return sigs


def _parse_ts(ts, now):
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=now.tzinfo)).astimezone(now.tzinfo)


def merge(store, sigs, now):
    """Add new signals, drop ones whose moment has long passed."""
    keep = []
    for s in (store or []) + [{**s, "seen_at": now.isoformat(timespec="minutes")} for s in sigs]:
        anchor = _parse_ts(s.get("when"), now) or _parse_ts(s.get("seen_at"), now) or now
        if now - anchor <= timedelta(days=KEEP_DAYS):
            keep.append(s)
    dedup = {s["id"]: s for s in keep}
    return list(dedup.values())


def note(sig):
    cur = (ramu_profile.PROFILE.get("income") or {}).get("currency_symbol", "")
    amt = f", {cur}{sig['amount']:,.0f}" if sig.get("amount") else ""
    when = f", {sig['when'][:16].replace('T', ' ')}" if sig.get("when") else ""
    return f"Inbox: {sig['type']}: {sig['title']}{when}{amt}."


def block(store, now):
    """The live signals, for every prompt: upcoming things and today's arrivals."""
    rows = []
    for s in store or []:
        at = _parse_ts(s.get("when"), now)
        if s.get("importance") == "low":
            continue
        if at is not None and at < now - timedelta(hours=12):
            continue
        rows.append((at or now, s))
    if not rows:
        return ""
    rows.sort(key=lambda r: r[0])
    lines = "; ".join(note(s)[7:-1] for _, s in rows[:8])
    return f"From {ramu_profile.NAME}'s inbox (bookings, orders, renewals): {lines}.\n---\n"
