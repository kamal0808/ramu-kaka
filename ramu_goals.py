"""Goals with live progress: a few goals, each with a target, a deadline and a
number that updates itself.

Progress is measured from the user's real data, never typed in by hand.

goals.json holds the goals; METRICS turn life.db / people.json / presence into
this week's or this month's number; block() puts them in every prompt so
chats, the overnight shift and the weekly reset all work toward them.
"""
import calendar
from datetime import datetime, timedelta

import ramu_profile

# Starter goals, proposed (not active) until the user agrees to them. They come
# from the profile's optional "goals" key (same shape as a goal: id, title,
# metric, target, direction, why); with no key there are none, e.g.
#   {"id": "walk", "title": "Walk 30 minutes a day", "metric": "...", "target": 7,
#    "direction": "at_least", "why": "..."}
SEED = [dict(g) for g in (ramu_profile.PROFILE.get("goals") or []) if isinstance(g, dict) and g.get("id")]


def seed(now):
    return [{**g, "status": "proposed", "created": now.isoformat(timespec="seconds"), "deadline": None,
             "next_action": None} for g in SEED]


EDITABLE = ("title", "target", "deadline", "status", "next_action", "why")


def apply_updates(goals, updates, now):
    """Reply-envelope goal edits: {"id": ..., <field>: ...}. A new id with a
    title, metric and target adds a goal. Returns (goals, changed ids)."""
    goals = [dict(g) for g in goals or []]
    by_id = {g["id"]: g for g in goals}
    changed = []
    for u in updates or []:
        if not isinstance(u, dict) or not u.get("id"):
            continue
        g = by_id.get(u["id"])
        if g is None:
            if u.get("title") and u.get("metric") in METRICS and isinstance(u.get("target"), (int, float)):
                g = {"id": str(u["id"])[:30], "title": str(u["title"])[:80], "metric": u["metric"],
                     "target": u["target"], "direction": u.get("direction", "at_least"),
                     "status": "active", "created": now.isoformat(timespec="seconds"),
                     "deadline": None, "next_action": None, "why": ""}
                goals.append(g)
                by_id[g["id"]] = g
                changed.append(g["id"])
            continue
        for k in EDITABLE:
            if k in u:
                if k == "status" and u[k] not in ("proposed", "active", "done", "dropped"):
                    continue
                if k == "target" and not isinstance(u[k], (int, float)):
                    continue
                g[k] = u[k]
        g["updated"] = now.isoformat(timespec="seconds")
        changed.append(g["id"])
    return goals, changed


def _week_start(now):
    return (now - timedelta(days=now.weekday())).date()


def gym_sessions_week(ctx, now):
    start = _week_start(now).isoformat()
    row = ctx["db"].execute(
        "SELECT COUNT(DISTINCT date(ts)) FROM training_log WHERE is_rest_day = 0 AND date(ts) >= ?", (start,)
    ).fetchone()
    return (row[0] if row else 0), "sessions this week"


def tracked_habit_per_day_week(ctx, now):
    """Average per logged day of the habit the profile tracks (tracked_habit)."""
    start = _week_start(now).isoformat()
    rows = ctx["db"].execute(
        "SELECT date(ts) AS d, COUNT(*) FROM habits_log WHERE kind = 'tracked' AND date(ts) >= ? GROUP BY d",
        (start,),
    ).fetchall()
    if not rows:
        return None, "nothing logged for the tracked habit this week"
    return round(sum(r[1] for r in rows) / len(rows), 1), f"avg/day over {len(rows)} logged day(s)"


def new_people_week(ctx, now):
    start = _week_start(now).isoformat()
    n = sum(1 for p in ctx.get("people") or []
            if p.get("source") != "seed" and str(p.get("added", ""))[:10] >= start)
    return n, "new people added this week"


def income_mtd(ctx, now):
    row = ctx["db"].execute(
        "SELECT total, ts FROM wealth_snapshot WHERE ts LIKE ? ORDER BY ts DESC LIMIT 1",
        (now.strftime("%Y-%m") + "%",),
    ).fetchone()
    if not row or row[0] is None:
        return None, "no money snapshot this month"
    return row[0], f"month to date (snapshot {row[1][:10]})"


def sleep_avg_week(ctx, now):
    start = _week_start(now).isoformat()
    rows = ctx["db"].execute(
        "SELECT hours FROM sleep_session WHERE date(wake_ts) >= ?", (start,)).fetchall()
    hours = [r[0] for r in rows if r[0]]
    watch = ((ctx.get("presence") or {}).get("health") or {}).get("sleep_hours")
    if not hours and watch:
        hours = [watch]
    if not hours:
        return None, "no sleep hours recorded this week"
    return round(sum(hours) / len(hours), 1), f"avg over {len(hours)} night(s)"


METRICS = {f.__name__: f for f in (gym_sessions_week, tracked_habit_per_day_week, new_people_week,
                                   income_mtd, sleep_avg_week)}


def evaluate(goal, ctx, now):
    """(value, detail, verdict). verdict: on_track / behind / met / no_data."""
    fn = METRICS.get(goal.get("metric"))
    if fn is None:
        return None, "unknown metric", "no_data"
    try:
        value, detail = fn(ctx, now)
    except Exception as e:  # noqa: BLE001 - a broken metric must not break the prompt
        return None, f"error: {e}", "no_data"
    if value is None:
        return None, detail, "no_data"
    target = goal["target"]
    if goal.get("direction") == "at_most":
        return value, detail, "met" if value <= target else "behind"
    if value >= target:
        return value, detail, "met"
    # Pace: how far through the week/month are we?
    if goal["metric"].endswith("_week"):
        frac = (now.weekday() + now.hour / 24) / 7
    else:
        days = calendar.monthrange(now.year, now.month)[1]
        frac = (now.day - 1 + now.hour / 24) / days
    return value, detail, "on_track" if value >= target * frac else "behind"


def _fmt(v):
    if isinstance(v, (int, float)) and v >= 10000:
        cur = (ramu_profile.PROFILE.get("income") or {}).get("currency_symbol", "")
        return f"{cur}{v:,.0f}"
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def block(goals, ctx, now):
    """Live goals for every prompt."""
    live = [g for g in goals or [] if g.get("status") in ("active", "proposed")]
    if not live:
        return ""
    lines = []
    for g in live:
        value, detail, verdict = evaluate(g, ctx, now)
        shown = "no data" if value is None else f"{_fmt(value)} vs {_fmt(g['target'])}"
        tag = f" (PROPOSED, not yet agreed with {ramu_profile.NAME})" if g.get("status") == "proposed" else ""
        dl = f", deadline {g['deadline']}" if g.get("deadline") else ""
        lines.append(f"- {g['title']}{tag}: {shown} ({detail}) -> {verdict.replace('_', ' ')}{dl}")
    return (f"{ramu_profile.NAME}'s goals, with live progress (work toward these; never nag about them unasked, "
            "use them to choose what matters):\n" + "\n".join(lines) + "\n---\n")
