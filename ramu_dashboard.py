"""The user's live personal life dashboard: six tiles pulled straight from
the ClickUp Life OS HQ task comments and Toggl, no manual entry.

Two layers, split for testability:
  - pure functions (parse_ts, the six tile builders, render_html) take
    already-fetched data and do no I/O -- these are what tests/test_ramu_dashboard.py
    exercises.
  - fetch_* functions call out to the `composio` CLI, same subprocess pattern
    daemon.py's composio_execute uses (see its module docstring for why: MCP
    tool access from `claude -p` isn't available, composio execute is).

Everything specific to the person (task ids, the habit being tracked, income
sources, targets) comes from the profile (ramu_profile.py); an empty value
just leaves that tile without data.

Run directly (`python3 ramu_dashboard.py`) to fetch live data, write
dashboard.html next to this file, and open it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import webbrowser
from datetime import date, datetime, timedelta, timezone

import ramu_charts as rc
import ramu_profile
import ramu_store as rs

TZ = ramu_profile.TZ
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_PATH = os.path.join(PROJECT_DIR, "dashboard.html")

# ClickUp HQ task ids per area. "activity"/"appearance" have no tile/chart of
# their own yet -- ingested into the store anyway so the database covers every
# hq_key, not just the ones the dashboard currently renders.
_HQ = ramu_profile.PROFILE.get("hq_task_ids", {}) or {}
TASK_IDS = {
    "body": _HQ.get("body") or _HQ.get("training") or "",
    "nutrition": _HQ.get("nutrition", ""),
    "habits": _HQ.get("habits", ""),
    "sleep": _HQ.get("sleep", ""),
    "activity": _HQ.get("activity", ""),
    "appearance": _HQ.get("appearance", ""),
}

SERIES_DAYS = 14
WEALTH_SERIES_DAYS = 30

TOGGL_ACCOUNT = ramu_profile.PROFILE.get("toggl_account", "")

# Income: hours logged in Toggl against one project at an hourly rate (net of
# any withholding), plus a fixed estimate of other monthly income.
_INCOME = ramu_profile.PROFILE.get("income", {}) or {}
HOURLY_PROJECT_ID = _INCOME.get("hourly_project_id")
HOURLY_RATE = float(_INCOME.get("hourly_rate") or 0)
HOURLY_WITHHOLDING = float(_INCOME.get("withholding") or 0)
OTHER_MONTHLY_INCOME = int(_INCOME.get("other_monthly") or 0)
WEALTH_TARGET = int(_INCOME.get("target_monthly") or 0)
CURRENCY = _INCOME.get("currency_symbol", "")

# One habit the user wants to count and cap (e.g. "coffee"). An entry in the
# habits HQ log containing `keyword` counts as one. The cap applies between
# cap_start and cap_end (ISO dates); with no dates it always applies.
_HABIT = ramu_profile.PROFILE.get("tracked_habit", {}) or {}
HABIT_KEYWORD = str(_HABIT.get("keyword") or "").strip().lower()
HABIT_CAP = _HABIT.get("cap")


def _iso_date(v):
    try:
        return date.fromisoformat(str(v)) if v else None
    except ValueError:
        return None


CAP_START = _iso_date(_HABIT.get("cap_start"))
CAP_END = _iso_date(_HABIT.get("cap_end"))
CAP_LIMIT = HABIT_CAP


# ---- fetch (I/O) --------------------------------------------------------------

def composio_execute(slug, data, account=None, timeout=60):
    """Runs `composio execute <slug> -d <json>`, returns parsed body or None
    on any failure. Same offload-to-file handling as daemon.py's
    composio_execute (see its docstring)."""
    cmd = ["composio", "execute", slug, "-d", json.dumps(data)]
    if account:
        cmd += ["--account", account]
    try:
        result = subprocess.run(cmd, cwd=PROJECT_DIR, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    out_text = result.stdout or ""
    i = out_text.find("{")
    if i < 0:
        return None
    try:
        envelope = json.loads(out_text[i:])
    except json.JSONDecodeError:
        return None
    if not envelope.get("successful"):
        return None
    if envelope.get("storedInFile") and envelope.get("outputFilePath"):
        try:
            with open(envelope["outputFilePath"]) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
    return envelope


def fetch_comments(task_id):
    """[comment_text, ...] newest first, or [] on failure -- a fetch error
    should degrade a tile to 'no data', never crash the whole dashboard."""
    parsed = composio_execute("CLICKUP_GET_TASK_COMMENTS", {"task_id": task_id})
    if not parsed:
        return []
    comments = ((parsed.get("data") or {}).get("comments")) or []
    return [c.get("comment_text", "") for c in comments if c.get("comment_text")]


def fetch_open_tasks(list_id):
    parsed = composio_execute("CLICKUP_GET_TASKS", {"page": 0, "list_id": list_id, "include_closed": False})
    if not parsed:
        return []
    return ((parsed.get("data") or {}).get("tasks")) or []


def fetch_closed_tasks(list_id):
    parsed = composio_execute("CLICKUP_GET_TASKS", {"page": 0, "list_id": list_id, "include_closed": True})
    if not parsed:
        return []
    tasks = ((parsed.get("data") or {}).get("tasks")) or []
    return [t for t in tasks if (t.get("status") or {}).get("status") not in (None, "to do")]


def fetch_toggl_month_entries(now):
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    parsed = composio_execute(
        "TOGGL_GET_TIME_ENTRIES",
        {"start_date": start.date().isoformat(), "end_date": now.date().isoformat()},
        account=TOGGL_ACCOUNT,
    )
    if not parsed:
        return []
    return ((parsed.get("data") or {}).get("time_entries")) or []


# ---- parsing (pure) -----------------------------------------------------------

_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})(?: (\d{2}:\d{2}))?\]\s*(.*)$", re.DOTALL)


def parse_ts(comment_text):
    """'[2030-01-02 07:00] Sleep: okay (tap check-in).' ->
    (datetime|None, rest_text). datetime is None for narrative comments with
    no bracket prefix (e.g. a plan writeup) -- those never count as
    log entries. A date-only bracket (no time) parses to local midnight and is
    still usable for day-bucketing but not for duration math."""
    m = _TS_RE.match(comment_text or "")
    if not m:
        return None, comment_text or ""
    date_s, time_s, rest = m.groups()
    try:
        if time_s:
            dt = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        else:
            dt = datetime.strptime(date_s, "%Y-%m-%d").replace(tzinfo=TZ)
    except ValueError:
        return None, comment_text or ""
    return dt, rest.strip()


_TIMED_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})\]\s*(.*)$", re.DOTALL)


def _dated(comments):
    """[(dt, rest), ...] for comments with a full '[date time]' bracket --
    the shape every real check-in/log entry uses. A date-only bracket (e.g.
    a plan writeup, '[2030-01-02] ...') is narrative, not a log
    entry, and is deliberately excluded so it can't get miscounted as one."""
    out = []
    for c in comments:
        m = _TIMED_RE.match(c or "")
        if not m:
            continue
        date_s, time_s, rest = m.groups()
        try:
            dt = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        except ValueError:
            continue
        out.append((dt, rest.strip()))
    return out


# ---- tile builders (pure) ------------------------------------------------------

def body_tile(comments, now):
    entries = _dated(comments)
    workout_days = set()
    last_workout = None
    for dt, rest in entries:
        low = rest.lower()
        if "not today" in low or low.startswith("did not go"):
            continue
        workout_days.add(dt.date())
        if last_workout is None or dt > last_workout[0]:
            last_workout = (dt, rest)

    streak = 0
    day = now.date()
    while day in workout_days:
        streak += 1
        day -= timedelta(days=1)

    return {
        "label": "Body",
        "last": last_workout[1][:140] if last_workout else "No workout logged yet",
        "last_when": last_workout[0].strftime("%a %d %b, %H:%M") if last_workout else None,
        "streak_days": streak,
        "status": "good" if streak >= 2 else ("warning" if streak == 1 else "serious"),
    }


def nutrition_tile(comments, now):
    entries = _dated(comments)
    today = [(dt, rest) for dt, rest in entries if dt.date() == now.date()]
    today.sort(key=lambda x: x[0])
    return {
        "label": "Nutrition",
        "today_log": [f"{dt.strftime('%H:%M')} — {rest[:100]}" for dt, rest in today],
        "logged_count": len(today),
        "status": "good" if len(today) >= 2 else ("warning" if len(today) == 1 else "serious"),
    }


_SLEEP_QUALITY_RE = re.compile(r"^sleep:\s*([a-z ]+?)\s*\(tap check-in\)\.?$", re.IGNORECASE)


def _sleep_facts(comments):
    """(qualities, pairs) shared by sleep_tile (status) and ingest_sleep (DB
    rows) so the sleep/wake pairing logic lives in exactly one place.
    qualities: [(dt, quality), ...]. pairs: [(sleep_dt, wake_dt), ...]."""
    entries = _dated(comments)
    entries.sort(key=lambda x: x[0])

    qualities = []
    for dt, rest in entries:
        m = _SLEEP_QUALITY_RE.match(rest)
        if m:
            qualities.append((dt, m.group(1).strip().lower()))

    # Anchored to the START of the entry, not a bare keyword search -- a
    # comment like "no specific wake time given" contains the word "wake"
    # without being a wake event, and would otherwise get paired as one.
    sleep_events = [(dt, rest) for dt, rest in entries if re.match(r"going to sleep|sleep onset", rest, re.IGNORECASE)]
    wake_events = [(dt, rest) for dt, rest in entries if re.match(r"woke\b", rest, re.IGNORECASE)]

    pairs = []
    for wdt, wrest in wake_events:
        prior_sleep = [s for s in sleep_events if s[0] < wdt]
        if not prior_sleep:
            continue
        sdt, srest = max(prior_sleep, key=lambda s: s[0])
        gap_hours = (wdt - sdt).total_seconds() / 3600
        if 0 < gap_hours <= 16:
            pairs.append((sdt, wdt))

    return qualities, pairs


def sleep_tile(comments, now):
    qualities, pairs = _sleep_facts(comments)

    last_pair = None
    for sdt, wdt in pairs:
        if last_pair is None or wdt > last_pair[1]:
            last_pair = (sdt, wdt)

    hours = round((last_pair[1] - last_pair[0]).total_seconds() / 3600, 1) if last_pair else None

    last_quality = qualities[-1][1] if qualities else None
    status_map = {"good": "good", "okay": "warning", "bad": "serious", "barely slept": "critical"}
    status = status_map.get(last_quality, "warning")

    trend = [{"date": dt.strftime("%d %b"), "quality": q} for dt, q in qualities[-6:]]

    return {
        "label": "Sleep",
        "hours_last_night": hours,
        "last_quality": last_quality,
        "target_hours": 8,
        "trend": trend,
        "status": status,
    }


def habits_tile(comments, now):
    entries = _dated(comments)
    today_count = sum(
        1 for dt, rest in entries
        if dt.date() == now.date() and HABIT_KEYWORD and HABIT_KEYWORD in rest.lower()
    )
    d = now.date()
    in_window = (CAP_START is None or CAP_START <= d) and (CAP_END is None or d <= CAP_END)
    if CAP_LIMIT is not None and in_window:
        cap = CAP_LIMIT
        cap_note = f"cap through {CAP_END.strftime('%a %-d %b')}" if CAP_END else "daily cap"
    else:
        cap = None
        cap_note = "no active cap for today"

    remaining = (cap - today_count) if cap is not None else None
    if cap is None:
        status = "warning"
    elif today_count > cap:
        status = "critical"
    elif remaining == 0:
        status = "warning"
    else:
        status = "good"

    return {
        "label": "Habits",
        "count_today": today_count,
        "cap": cap,
        "cap_note": cap_note,
        "remaining": remaining,
        "status": status,
    }


def wealth_tile(toggl_entries, now):
    hours = sum(
        (e.get("duration") or 0)
        for e in toggl_entries
        if HOURLY_PROJECT_ID is not None and e.get("project_id") == HOURLY_PROJECT_ID
        and (e.get("duration") or 0) > 0
    ) / 3600
    hourly_net = round(hours * HOURLY_RATE * (1 - HOURLY_WITHHOLDING))

    other_income = OTHER_MONTHLY_INCOME

    total = hourly_net + other_income
    pct_of_target = total / WEALTH_TARGET if WEALTH_TARGET else 0

    return {
        "label": "Wealth",
        "hourly_hours_mtd": round(hours, 1),
        "hourly_net": hourly_net,
        "other_income": other_income,
        "total": total,
        "target": WEALTH_TARGET,
        "status": "good" if pct_of_target >= 1 else ("warning" if pct_of_target >= 0.5 else "serious"),
    }


_EXPERIENCE_RE = re.compile(r"\b(comedy|show|event|brunch|meetup|dinner|date|party)\b")


def _is_experience_task(name):
    """Shared by social_tile (status) and ingest_social (DB rows): a closed
    task name that reads like an actual social experience, not scaffolding.
    \\b-bounded: a substring hit inside a compound word (e.g. "show" inside
    "Showcase") is not the same as the word actually appearing."""
    low = (name or "").lower()
    if any(w in low for w in ("ticket", "rsvp", "plan for")):
        return False  # scaffolding/logistics tasks, not an actual experience record
    return bool(_EXPERIENCE_RE.search(low))


def social_tile(closed_life_os_tasks, open_life_os_tasks, now):
    last = None
    for t in closed_life_os_tasks:
        name = (t.get("name") or "")
        if not _is_experience_task(name):
            continue
        closed_ms = t.get("date_closed")
        if not closed_ms:
            continue
        dt = datetime.fromtimestamp(int(closed_ms) / 1000, tz=TZ)
        if last is None or dt > last[0]:
            last = (dt, name)

    next_up = None
    next_date_re = re.compile(r"(mon|tue|wed|thu|fri|sat|sun)\w*\s+(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", re.IGNORECASE)
    candidates = []
    for t in open_life_os_tasks:
        name = t.get("name") or ""
        if next_date_re.search(name):
            candidates.append(name)
    if candidates:
        next_up = sorted(candidates)[0]  # dated names sort close enough for a single-month horizon

    return {
        "label": "Social",
        "last_experience": last[1] if last else None,
        "last_when": last[0].strftime("%a %d %b") if last else None,
        "next_queued": next_up,
        "status": None,
    }


# ---- ingest: turn parsed comments/entries into structured DB rows -------------
#
# Reuses the same parsing already done above (_dated, _sleep_facts,
# _is_experience_task) so a log entry is read the same way whether it is
# driving a tile's live status or a row in life.db -- one parser, two
# consumers, instead of a second copy that could drift.

def _store_ts(dt):
    """Local wall-clock, no UTC offset -- see ramu_store's module docstring
    for why: an explicit offset would make SQLite's date() convert to UTC
    first and shift some entries onto the wrong calendar day."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _meal_slot(dt, rest):
    low = rest.lower()
    for slot in ("breakfast", "lunch", "dinner", "snack"):
        if low.startswith(slot):
            return slot
    hour = dt.hour + dt.minute / 60
    if hour < 11:
        return "breakfast"
    if hour < 16:
        return "lunch"
    if hour < 19:
        return "snack"
    return "dinner"


def _habit_kind(rest):
    return "tracked" if HABIT_KEYWORD and HABIT_KEYWORD in rest.lower() else "other"


def ingest_training(conn, comments):
    entries = _dated(comments)
    rows = []
    for dt, rest in entries:
        low = rest.lower()
        is_rest = "not today" in low or low.startswith("did not go")
        rows.append((_store_ts(dt), rest, is_rest))
    return rs.insert_training(conn, rows)


def ingest_nutrition(conn, comments):
    entries = _dated(comments)
    rows = [(_store_ts(dt), _meal_slot(dt, rest), rest) for dt, rest in entries]
    return rs.insert_nutrition(conn, rows)


def ingest_sleep(conn, comments):
    qualities, pairs = _sleep_facts(comments)
    n1 = rs.insert_sleep_checkins(conn, [(_store_ts(dt), q) for dt, q in qualities])
    hours_rows = [
        (_store_ts(sdt), _store_ts(wdt), round((wdt - sdt).total_seconds() / 3600, 1))
        for sdt, wdt in pairs
    ]
    n2 = rs.insert_sleep_sessions(conn, hours_rows)
    return n1 + n2


def ingest_habits(conn, comments):
    entries = _dated(comments)
    rows = [(_store_ts(dt), _habit_kind(rest), rest) for dt, rest in entries]
    return rs.insert_habits(conn, rows)


def ingest_activity(conn, comments):
    entries = _dated(comments)
    return rs.insert_activity(conn, [(_store_ts(dt), rest) for dt, rest in entries])


def ingest_appearance(conn, comments):
    entries = _dated(comments)
    return rs.insert_appearance(conn, [(_store_ts(dt), rest) for dt, rest in entries])


def _parse_toggl_start(start):
    """Toggl returns UTC ISO timestamps; convert to local time before storing so
    day-bucketing lines up with every other table (see _store_ts)."""
    if not start:
        return None
    try:
        dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


def ingest_wealth_toggl(conn, entries):
    """Stores every entry's raw project_id/duration -- unfiltered, unlike
    wealth_tile's single-project view -- so the query side (wealth_daily_series)
    can filter per caller instead of the store deciding what "counts"."""
    rows = []
    for e in entries:
        dur = e.get("duration") or 0
        if dur <= 0:
            continue  # negative = still running, zero = not a real logged entry
        dt = _parse_toggl_start(e.get("start"))
        if dt is None:
            continue
        key = str(e.get("id") or f"{e.get('start')}:{e.get('project_id')}:{dur}")
        rows.append((key, _store_ts(dt), e.get("project_id"), dur, e.get("description") or ""))
    return rs.insert_wealth_entries(conn, rows)


def ingest_social(conn, closed_tasks):
    """Only closed experiences are logged as dated events -- an *open*
    "next queued" task has no "happened at time T" moment yet, so it stays
    something the tile reads live rather than a row in the store."""
    rows = []
    for t in closed_tasks:
        name = t.get("name") or ""
        if not _is_experience_task(name):
            continue
        closed_ms = t.get("date_closed")
        if not closed_ms:
            continue
        dt = datetime.fromtimestamp(int(closed_ms) / 1000, tz=TZ)
        rows.append((_store_ts(dt), "experience_closed", name))
    return rs.insert_social(conn, rows)


# ---- render (pure) --------------------------------------------------------------

STATUS_COLOR = {
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
    None: "#898781",
}
STATUS_LABEL = {"good": "On track", "warning": "Watch", "serious": "Off track", "critical": "Over", None: "—"}


def _esc(s):
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _status_chip(status):
    if status is None:
        return ""
    color = STATUS_COLOR[status]
    label = STATUS_LABEL[status]
    return f'<span class="chip" style="color:{color};border-color:{color}">{label}</span>'


def _tile(title, value_html, sub_html, status, chart_svg=""):
    chart_block = f'<div class="chart-wrap">{chart_svg}</div>' if chart_svg else ""
    return f"""
    <section class="tile">
      <div class="tile-head">
        <span class="tile-label">{_esc(title)}</span>
        {_status_chip(status)}
      </div>
      <div class="tile-value">{value_html}</div>
      <div class="tile-sub">{sub_html}</div>
      {chart_block}
    </section>
    """


def render_html(body, nutrition, sleep, habits, wealth, social, generated_at, series=None):
    """series (optional): {"training": [...], "nutrition": [...], "sleep": [...],
    "habits": [...], "wealth": [...]} from ramu_store's series queries -- adds a
    real time-series chart under the matching tile. Omitted/empty entries just
    render that tile without a chart, so this stays backward compatible with
    callers (and tests) that only pass the six status dicts."""
    series = series or {}

    body_chart = ""
    if series.get("training"):
        pts = [{"label": d["date"], "value": 1 if d["trained"] else 0} for d in series["training"]]
        body_chart = rc.svg_bar_chart(pts, color=STATUS_COLOR["good"], cap=1, empty_label="No training logs yet")

    nutrition_chart = ""
    if series.get("nutrition"):
        pts = [{"label": d["date"], "value": d["count"]} for d in series["nutrition"]]
        nutrition_chart = rc.svg_bar_chart(pts, color=STATUS_COLOR["good"], empty_label="No nutrition logs yet")

    sleep_chart = ""
    if series.get("sleep"):
        pts = [{"label": d["date"], "value": d["hours"]} for d in series["sleep"]]
        sleep_chart = rc.svg_line_chart(pts, color=STATUS_COLOR["warning"], target=sleep["target_hours"], empty_label="No sleep data yet")

    habits_chart = ""
    if series.get("habits"):
        pts = [{"label": d["date"], "value": d["count"]} for d in series["habits"]]
        habits_chart = rc.svg_bar_chart(pts, color=STATUS_COLOR["serious"], cap=habits.get("cap"), empty_label="No habit logs yet")

    wealth_chart = ""
    if series.get("wealth"):
        pts = [{"label": d["date"], "value": d["net"]} for d in series["wealth"]]
        wealth_chart = rc.svg_line_chart(pts, color=STATUS_COLOR["good"], empty_label="No income data yet")

    body_val = f"{body['streak_days']}-day streak" if body["streak_days"] else "No streak"
    body_sub = _esc(body["last"]) + (f" &middot; {_esc(body['last_when'])}" if body["last_when"] else "")

    nutrition_val = f"{nutrition['logged_count']} logged today"
    nutrition_sub = "<br>".join(_esc(x) for x in nutrition["today_log"]) or "Nothing logged yet today"

    if sleep["hours_last_night"] is not None:
        sleep_val = f"{sleep['hours_last_night']}h <span class='vs'>vs {sleep['target_hours']}h target</span>"
    else:
        sleep_val = f"{_esc((sleep['last_quality'] or 'no data').title())} <span class='vs'>vs {sleep['target_hours']}h target</span>"
    trend_str = " ".join(f"{t['date']}: {_esc(t['quality'])}" for t in sleep["trend"])
    sleep_sub = trend_str or "No recent check-ins"

    cap_str = habits["cap"] if habits["cap"] is not None else "?"
    habits_val = f"{habits['count_today']} <span class='vs'>vs {cap_str} cap</span>"
    rem = habits["remaining"]
    rem_str = f"{rem} left today" if rem is not None and rem >= 0 else (f"{-rem} over cap" if rem is not None else "")
    habits_sub = f"{_esc(rem_str)} &middot; {_esc(habits['cap_note'])}"

    cur = _esc(CURRENCY)
    wealth_val = f"{cur}{wealth['total']:,} <span class='vs'>vs {cur}{wealth['target']:,} target</span>"
    wealth_sub = (
        f"Hourly: {wealth['hourly_hours_mtd']}h &rarr; {cur}{wealth['hourly_net']:,} net"
        f" &middot; Other: {cur}{wealth['other_income']:,}"
    )

    social_val = _esc(social["next_queued"]) if social["next_queued"] else "Nothing queued"
    last_str = f"Last: {social['last_experience']} ({social['last_when']})" if social["last_experience"] else "Last: nothing logged yet"
    social_sub = _esc(last_str)

    tiles = "".join([
        _tile("Body", body_val, body_sub, body["status"], body_chart),
        _tile("Nutrition", nutrition_val, nutrition_sub, nutrition["status"], nutrition_chart),
        _tile("Sleep", sleep_val, sleep_sub, sleep["status"], sleep_chart),
        _tile("Habits", habits_val, habits_sub, habits["status"], habits_chart),
        _tile("Wealth", wealth_val, wealth_sub, wealth["status"], wealth_chart),
        _tile("Social", social_val, social_sub, social["status"]),
    ])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ramu Kaka — Dashboard</title>
<style>
  .viz-root {{
    color-scheme: light;
    --surface-1:      #fcfcfb;
    --page-plane:      #f9f9f7;
    --text-primary:    #0b0b0b;
    --text-secondary:  #52514e;
    --text-muted:      #898781;
    --gridline:        #e1e0d9;
    --border:          rgba(11,11,11,0.10);
  }}
  @media (prefers-color-scheme: dark) {{
    :root:where(:not([data-theme="light"])) .viz-root {{
      color-scheme: dark;
      --surface-1:      #1a1a19;
      --page-plane:      #0d0d0d;
      --text-primary:    #ffffff;
      --text-secondary:  #c3c2b7;
      --text-muted:      #898781;
      --gridline:        #2c2c2a;
      --border:          rgba(255,255,255,0.10);
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    background: var(--page-plane);
    color: var(--text-primary);
  }}
  .wrap {{ max-width: 960px; margin: 0 auto; padding: 32px 20px 60px; }}
  h1 {{ font-size: 20px; font-weight: 600; margin: 0 0 2px; }}
  .generated {{ color: var(--text-muted); font-size: 13px; margin: 0 0 28px; }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 16px;
  }}
  .tile {{
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 18px 20px;
  }}
  .tile-head {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 10px;
  }}
  .tile-label {{
    font-size: 13px;
    font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }}
  .chip {{
    font-size: 11px;
    font-weight: 600;
    border: 1px solid;
    border-radius: 999px;
    padding: 2px 8px;
  }}
  .tile-value {{
    font-size: 26px;
    font-weight: 600;
    margin-bottom: 8px;
  }}
  .tile-value .vs {{
    font-size: 14px;
    font-weight: 400;
    color: var(--text-muted);
  }}
  .tile-sub {{
    font-size: 13px;
    color: var(--text-secondary);
    line-height: 1.5;
  }}
  .chart-wrap {{ margin-top: 12px; }}
  .chart {{ display: block; overflow: visible; }}
  .chart-empty {{ fill: var(--text-muted); font-size: 12px; }}
  .chart-axis {{ fill: var(--text-muted); font-size: 10px; }}
  .chart-target {{ stroke: var(--text-muted); stroke-width: 1; }}
</style>
</head>
<body>
<div class="viz-root wrap">
  <h1>Life OS Dashboard</h1>
  <p class="generated">Live from ClickUp + Toggl, backed by a local SQLite store &middot; generated {_esc(generated_at)}</p>
  <div class="grid">
    {tiles}
  </div>
</div>
</body>
</html>
"""


# ---- orchestration (I/O) --------------------------------------------------------

def build(now=None, db_path=None):
    """Fetch everything live, ingest it into life.db as structured rows, and
    return the rendered HTML string (tiles + real time-series charts)."""
    now = now or datetime.now(TZ)
    conn = rs.connect(db_path or rs.DB_PATH)
    try:
        def comments(key):
            return fetch_comments(TASK_IDS[key]) if TASK_IDS.get(key) else []

        body_comments = comments("body")
        nutrition_comments = comments("nutrition")
        sleep_comments = comments("sleep")
        habits_comments = comments("habits")
        activity_comments = comments("activity")
        appearance_comments = comments("appearance")
        toggl_entries = fetch_toggl_month_entries(now) if TOGGL_ACCOUNT else []

        life_os_list_id = ramu_profile.PROFILE.get("clickup_list_id", "")
        closed_tasks = fetch_closed_tasks(life_os_list_id) if life_os_list_id else []
        open_tasks = fetch_open_tasks(life_os_list_id) if life_os_list_id else []

        ingest_training(conn, body_comments)
        ingest_nutrition(conn, nutrition_comments)
        ingest_sleep(conn, sleep_comments)
        ingest_habits(conn, habits_comments)
        ingest_activity(conn, activity_comments)
        ingest_appearance(conn, appearance_comments)
        ingest_wealth_toggl(conn, toggl_entries)
        ingest_social(conn, closed_tasks)

        body = body_tile(body_comments, now)
        nutrition = nutrition_tile(nutrition_comments, now)
        sleep = sleep_tile(sleep_comments, now)
        habits = habits_tile(habits_comments, now)
        wealth = wealth_tile(toggl_entries, now)
        social = social_tile(closed_tasks, open_tasks, now)

        rs.upsert_wealth_snapshot(
            conn, now.date().isoformat(), wealth["hourly_hours_mtd"], wealth["hourly_net"],
            wealth["other_income"], wealth["total"], wealth["target"],
        )

        today = now.date()
        series = {
            "training": rs.training_streak_series(conn, today, days=SERIES_DAYS),
            "nutrition": rs.nutrition_daily_counts(conn, today, days=SERIES_DAYS),
            "sleep": rs.sleep_hours_series(conn, today, days=SERIES_DAYS),
            "habits": rs.habits_tracked_series(conn, today, days=SERIES_DAYS),
            "wealth": rs.wealth_daily_series(
                conn, today, HOURLY_PROJECT_ID, HOURLY_RATE, HOURLY_WITHHOLDING,
                days=WEALTH_SERIES_DAYS,
            ),
        }
    finally:
        conn.close()

    return render_html(
        body, nutrition, sleep, habits, wealth, social,
        now.strftime("%a %d %b %Y, %H:%M %Z"), series=series,
    )


def main():
    html = build()
    with open(DASHBOARD_PATH, "w") as f:
        f.write(html)
    print(DASHBOARD_PATH)
    webbrowser.open(f"file://{DASHBOARD_PATH}")


if __name__ == "__main__":
    main()
