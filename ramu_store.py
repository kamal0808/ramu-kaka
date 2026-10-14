"""SQLite-backed structured store for Ramu Kaka's life data.

Replaces "re-parse raw ClickUp/Toggl text on every page load" with one row
per logged data point, timestamped, in a table per hq_key -- so the
dashboard can draw real time-series graphs instead of showing a snapshot.

ramu_dashboard.py's ingest_* functions still pull from the same real
sources (ClickUp comments, Toggl) and do the parsing (reusing its existing
regexes); this module only owns the schema, idempotent writes, and the
read-side series queries the charts use. It knows nothing about ClickUp,
Toggl, or dashboard rendering -- kept that way so it has no business
constants (rates, project ids) baked in; callers pass those in.

Single-user, personal, gitignored (see .gitignore) -- SQLite is plenty.

Timestamps are stored as naive "YYYY-MM-DD HH:MM:SS" local wall-clock
strings (the profile's timezone), deliberately without a UTC offset.
SQLite's date()/datetime() functions convert any string carrying an
explicit offset to UTC before extracting the date, which would shift
late-night/early-morning local entries onto the wrong calendar day.
Storing the local wall-clock value directly means date(ts) always matches
the day a human would say the entry happened on.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import timedelta

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PROJECT_DIR, "life.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS training_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  note TEXT NOT NULL,
  is_rest_day INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'clickup_comment',
  UNIQUE(ts, note)
);

CREATE TABLE IF NOT EXISTS nutrition_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  meal_slot TEXT,
  note TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'clickup_comment',
  UNIQUE(ts, note)
);

CREATE TABLE IF NOT EXISTS sleep_checkin (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  quality TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'clickup_comment',
  UNIQUE(ts, quality)
);

CREATE TABLE IF NOT EXISTS sleep_session (
  id INTEGER PRIMARY KEY,
  sleep_ts TEXT NOT NULL,
  wake_ts TEXT NOT NULL,
  hours REAL NOT NULL,
  source TEXT NOT NULL DEFAULT 'clickup_comment',
  UNIQUE(sleep_ts, wake_ts)
);

CREATE TABLE IF NOT EXISTS habits_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  note TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'clickup_comment',
  UNIQUE(ts, note)
);

CREATE TABLE IF NOT EXISTS activity_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  note TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'clickup_comment',
  UNIQUE(ts, note)
);

CREATE TABLE IF NOT EXISTS appearance_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  note TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'clickup_comment',
  UNIQUE(ts, note)
);

CREATE TABLE IF NOT EXISTS wealth_toggl_entry (
  id INTEGER PRIMARY KEY,
  entry_key TEXT NOT NULL UNIQUE,
  ts TEXT NOT NULL,
  project_id INTEGER,
  duration_seconds INTEGER NOT NULL,
  description TEXT,
  source TEXT NOT NULL DEFAULT 'toggl'
);

CREATE TABLE IF NOT EXISTS wealth_snapshot (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL UNIQUE,
  hourly_hours_mtd REAL,
  hourly_net INTEGER,
  other_income INTEGER,
  total INTEGER,
  target INTEGER,
  source TEXT NOT NULL DEFAULT 'computed'
);

CREATE TABLE IF NOT EXISTS social_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'clickup',
  UNIQUE(ts, kind, name)
);
"""


def connect(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ---- writes -------------------------------------------------------------
# Every insert is INSERT OR IGNORE against a UNIQUE constraint, so
# re-ingesting the same comments/entries (the normal case -- fetch_* always
# pulls the full recent history, not just what's new) is a no-op rather
# than a pile of duplicate rows.

def _insert_many(conn, sql, rows):
    inserted = 0
    for row in rows:
        cur = conn.execute(sql, row)
        if cur.rowcount and cur.rowcount > 0:
            inserted += 1
    conn.commit()
    return inserted


def insert_training(conn, rows):
    """rows: [(ts, note, is_rest_day), ...]"""
    return _insert_many(
        conn,
        "INSERT OR IGNORE INTO training_log (ts, note, is_rest_day) VALUES (?, ?, ?)",
        [(ts, note, int(bool(is_rest))) for ts, note, is_rest in rows],
    )


def insert_nutrition(conn, rows):
    """rows: [(ts, meal_slot, note), ...]"""
    return _insert_many(
        conn, "INSERT OR IGNORE INTO nutrition_log (ts, meal_slot, note) VALUES (?, ?, ?)", rows,
    )


def insert_sleep_checkins(conn, rows):
    """rows: [(ts, quality), ...]"""
    return _insert_many(conn, "INSERT OR IGNORE INTO sleep_checkin (ts, quality) VALUES (?, ?)", rows)


def insert_sleep_sessions(conn, rows):
    """rows: [(sleep_ts, wake_ts, hours), ...]"""
    return _insert_many(
        conn, "INSERT OR IGNORE INTO sleep_session (sleep_ts, wake_ts, hours) VALUES (?, ?, ?)", rows,
    )


def insert_habits(conn, rows):
    """rows: [(ts, kind, note), ...]"""
    return _insert_many(conn, "INSERT OR IGNORE INTO habits_log (ts, kind, note) VALUES (?, ?, ?)", rows)


def insert_activity(conn, rows):
    """rows: [(ts, note), ...]"""
    return _insert_many(conn, "INSERT OR IGNORE INTO activity_log (ts, note) VALUES (?, ?)", rows)


def insert_appearance(conn, rows):
    """rows: [(ts, note), ...]"""
    return _insert_many(conn, "INSERT OR IGNORE INTO appearance_log (ts, note) VALUES (?, ?)", rows)


def insert_wealth_entries(conn, rows):
    """rows: [(entry_key, ts, project_id, duration_seconds, description), ...]"""
    return _insert_many(
        conn,
        "INSERT OR IGNORE INTO wealth_toggl_entry "
        "(entry_key, ts, project_id, duration_seconds, description) VALUES (?, ?, ?, ?, ?)",
        rows,
    )


def upsert_wealth_snapshot(conn, ts, hourly_hours_mtd, hourly_net, other_income, total, target):
    """One row per day: re-running the same day updates it in place rather
    than growing a snapshot per build."""
    conn.execute(
        "INSERT INTO wealth_snapshot (ts, hourly_hours_mtd, hourly_net, other_income, total, target) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(ts) DO UPDATE SET hourly_hours_mtd=excluded.hourly_hours_mtd, "
        "hourly_net=excluded.hourly_net, other_income=excluded.other_income, "
        "total=excluded.total, target=excluded.target",
        (ts, hourly_hours_mtd, hourly_net, other_income, total, target),
    )
    conn.commit()


def insert_social(conn, rows):
    """rows: [(ts, kind, name), ...]"""
    return _insert_many(conn, "INSERT OR IGNORE INTO social_log (ts, kind, name) VALUES (?, ?, ?)", rows)


# ---- reads: time series for the dashboard's charts -----------------------

_QUALITY_HOURS = {"good": 8.0, "okay": 6.5, "bad": 4.5, "barely slept": 2.5, "barely": 2.5}


def _date_range(end_date, days):
    start_date = end_date - timedelta(days=days - 1)
    return [start_date + timedelta(days=i) for i in range(days)]


def sleep_hours_series(conn, end_date, days=14):
    """[{"date": "25 Sep", "hours": float|None, "estimated": bool}, ...]
    oldest first, one point per calendar day. A real paired sleep/wake
    session wins; a day with only a quality tap check-in falls back to an
    estimate (so the graph isn't mostly empty before every night has a
    paired session); a day with neither is a gap (None), not a zero."""
    start_date = end_date - timedelta(days=days - 1)
    sessions = conn.execute(
        "SELECT wake_ts, hours FROM sleep_session WHERE date(wake_ts) BETWEEN ? AND ?",
        (start_date.isoformat(), end_date.isoformat()),
    ).fetchall()
    by_day_hours = {r["wake_ts"][:10]: round(r["hours"], 1) for r in sessions}

    checkins = conn.execute(
        "SELECT ts, quality FROM sleep_checkin WHERE date(ts) BETWEEN ? AND ?",
        (start_date.isoformat(), end_date.isoformat()),
    ).fetchall()
    by_day_quality = {r["ts"][:10]: r["quality"] for r in checkins}

    out = []
    for d in _date_range(end_date, days):
        key = d.isoformat()
        if key in by_day_hours:
            out.append({"date": d.strftime("%d %b"), "hours": by_day_hours[key], "estimated": False})
        elif key in by_day_quality:
            out.append({
                "date": d.strftime("%d %b"),
                "hours": _QUALITY_HOURS.get(by_day_quality[key]),
                "estimated": True,
            })
        else:
            out.append({"date": d.strftime("%d %b"), "hours": None, "estimated": False})
    return out


def nutrition_daily_counts(conn, end_date, days=14):
    """[{"date": "25 Sep", "count": int}, ...] -- a day with no logs is a
    real 0, not a gap."""
    start_date = end_date - timedelta(days=days - 1)
    rows = conn.execute(
        "SELECT date(ts) AS d, COUNT(*) AS c FROM nutrition_log WHERE date(ts) BETWEEN ? AND ? GROUP BY d",
        (start_date.isoformat(), end_date.isoformat()),
    ).fetchall()
    counts = {r["d"]: r["c"] for r in rows}
    return [{"date": d.strftime("%d %b"), "count": counts.get(d.isoformat(), 0)} for d in _date_range(end_date, days)]


def training_streak_series(conn, end_date, days=14):
    """[{"date": "25 Sep", "trained": bool}, ...]"""
    start_date = end_date - timedelta(days=days - 1)
    rows = conn.execute(
        "SELECT date(ts) AS d FROM training_log WHERE is_rest_day = 0 AND date(ts) BETWEEN ? AND ? GROUP BY d",
        (start_date.isoformat(), end_date.isoformat()),
    ).fetchall()
    trained = {r["d"] for r in rows}
    return [{"date": d.strftime("%d %b"), "trained": d.isoformat() in trained} for d in _date_range(end_date, days)]


def habits_tracked_series(conn, end_date, days=14):
    """[{"date": "25 Sep", "count": int}, ...] for the habit the profile
    tracks (rows of kind 'tracked')."""
    start_date = end_date - timedelta(days=days - 1)
    rows = conn.execute(
        "SELECT date(ts) AS d, COUNT(*) AS c FROM habits_log "
        "WHERE kind = 'tracked' AND date(ts) BETWEEN ? AND ? GROUP BY d",
        (start_date.isoformat(), end_date.isoformat()),
    ).fetchall()
    counts = {r["d"]: r["c"] for r in rows}
    return [{"date": d.strftime("%d %b"), "count": counts.get(d.isoformat(), 0)} for d in _date_range(end_date, days)]


def wealth_daily_series(conn, end_date, project_id, rate_per_hour, withholding, days=30):
    """[{"date": "25 Sep", "net": int|None}, ...] -- per-day net income
    computed straight from the raw Toggl entries, so the trend has real
    history from day one instead of waiting for wealth_snapshot rows to
    accumulate one per day going forward. A day with no logged hours is a
    gap (None), not a zero-income day."""
    start_date = end_date - timedelta(days=days - 1)
    rows = conn.execute(
        "SELECT date(ts) AS d, SUM(duration_seconds) AS secs FROM wealth_toggl_entry "
        "WHERE project_id = ? AND duration_seconds > 0 AND date(ts) BETWEEN ? AND ? GROUP BY d",
        (project_id, start_date.isoformat(), end_date.isoformat()),
    ).fetchall()
    secs_by_day = {r["d"]: r["secs"] for r in rows}
    out = []
    for d in _date_range(end_date, days):
        secs = secs_by_day.get(d.isoformat())
        if secs is None:
            out.append({"date": d.strftime("%d %b"), "net": None})
        else:
            net = round((secs / 3600) * rate_per_hour * (1 - withholding))
            out.append({"date": d.strftime("%d %b"), "net": net})
    return out
