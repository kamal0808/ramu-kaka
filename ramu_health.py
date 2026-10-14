"""Health monitor: Ramu Kaka notices when its own senses go dark.

Phone-side feeds (location logger, notification forwarder, health webhook) can
die silently overnight and nobody notices until someone happens to look.

Each feed/loop has a normal rhythm. check() compares last-heard times against
it and returns problems with a plain one-line fix; transitions() decides what
to tell the user (new problem, recovery) without repeating itself.
Pure functions; daemon.health_loop does the I/O.
"""
from datetime import datetime, timedelta

# name -> (max silence, phone feed?, what to tell the user)
# Phone feeds only count once they've reported at least once (never set up
# = not broken), and only in the daytime (a phone off at night isn't a fault).
FEEDS = {
    "location": (timedelta(hours=3), True,
                 "Location hasn't reported since {since}. The phone probably stopped the location logger app: open it and restart logging."),
    "whatsapp": (timedelta(hours=24), True,
                 "No WhatsApp notifications since {since}. Check the phone automation app that forwards them is still running."),
    "health": (timedelta(hours=6), True,
               "Health data hasn't synced since {since}. Open the health-data webhook app and sync."),
    "telegram": (timedelta(minutes=20), False,
                 "I haven't been able to read Telegram since {since}; messages to me may be stuck."),
    "bankmail": (timedelta(hours=2), False,
                 "Gmail reads failing since {since}; bank alerts aren't coming in. The Composio Gmail link may need re-connecting."),
    "footprint": (timedelta(hours=3), False,
                  "Inbox scan failing since {since}. Likely the Gmail connection."),
    "goals": (timedelta(hours=8), False,
              "Dashboard/goal refresh failing since {since}; ClickUp or Toggl connection may be down."),
}
DAY_HOURS = (10, 23)  # phone feeds are judged only between these local hours


def _parse(ts, tz):
    try:
        dt = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).astimezone(tz)


def check(last_seen, now, started_at):
    """{name: message} for feeds that have gone silent. `last_seen` maps feed
    name -> ISO time it last worked; `started_at` is daemon start (loops get
    one full window of grace after a restart)."""
    problems = {}
    daytime = DAY_HOURS[0] <= now.hour < DAY_HOURS[1]
    for name, (window, phone, msg) in FEEDS.items():
        seen = _parse(last_seen.get(name), now.tzinfo)
        if phone:
            if seen is None or not daytime:
                continue
        else:
            anchor = seen or started_at
            if now - started_at < window or anchor is None:
                continue
            seen = anchor
        if now - seen > window:
            since = seen.strftime("%H:%M") if seen.date() == now.date() else seen.strftime("%d %b %H:%M")
            problems[name] = msg.format(since=since)
    return problems


def transitions(problems, state, now, remind_after=timedelta(hours=24), last_seen=None):
    """(messages to send, new state). Tell the user once when something breaks,
    remind at most daily while it stays broken, and once when it's back.

    A phone feed drops out of `problems` at night simply because check() stops
    judging it, so "back" needs proof: it must have reported after we last told
    the user. Otherwise "Location is back" goes out at night, followed by "hasn't
    reported since yesterday" the next morning."""
    state = dict(state or {})
    last_seen = last_seen or {}
    out = []
    for name, msg in problems.items():
        last = _parse(state.get(name), now.tzinfo)
        if last is None or now - last >= remind_after:
            out.append(msg)
            state[name] = now.isoformat(timespec="seconds")
    for name in [n for n in state if n not in problems]:
        if FEEDS.get(name, (None, False))[1]:
            seen, told = _parse(last_seen.get(name), now.tzinfo), _parse(state[name], now.tzinfo)
            if seen is None or (told is not None and seen <= told):
                continue
        out.append(f"{name.capitalize()} is back. ✓")
        del state[name]
    return out, state


def block(problems):
    """For the prompt: so Ramu Kaka doesn't trust a dead feed."""
    if not problems:
        return ""
    return ("Broken feeds right now (don't rely on these; the monitor already told the user): "
            + ", ".join(sorted(problems)) + ".\n---\n")
