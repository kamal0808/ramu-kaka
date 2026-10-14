#!/usr/bin/env python3
"""
daemon.py — Ramu Kaka's always-on background loop.

A plain OS process (run under launchd or any supervisor) that runs
independently of any Claude desktop app, so it can poll Telegram fast
(long-poll, seconds rather than minutes) and feel like texting a person.

Who it looks after, and which of their accounts it may use, comes from
ramu_profile.py (profile.json). Nothing personal is hardcoded here.

NOTE ON listen.sh
-----------------
listen.sh's own header says: "Only ONE poller may run per bot" and it tracks
a single shared offset in `.last_update` with no lock of its own. Don't run
another consumer of listen.sh (e.g. an older scheduled routine) alongside
this daemon: two processes reading/advancing the same offset file race.

WHY THE CLICKUP/CALENDAR LOOPS FETCH VIA `composio execute` DIRECTLY FROM
PYTHON, NOT VIA claude -p TOOL CALLS
--------------------------------------------------------------------------
A bare `claude -p` process has no access to the desktop app's first-party
connectors (`claude mcp list` under the standalone CLI reports "No MCP
servers configured"), so asking it to "check ClickUp" fails and it says so
on Telegram.

The fix: Composio (a separate integration system, already used for Telegram
via say.sh/listen.sh) has ClickUp, Google Calendar, Gmail etc. connected. So
`daemon.py` calls `composio execute CLICKUP_GET_TASKS` /
`composio execute GOOGLECALENDAR_EVENTS_LIST` directly -- a plain subprocess,
no LLM involved -- to fetch a small, pre-filtered, compact summary. That
summary is embedded as JSON in the `claude -p` prompt; the model's job
narrows to pure judgment (does this qualify as due/upcoming/newly commented,
given pending.json and the user's instructions file) with zero tool calls of
its own needed for the fetch. If the Composio fetch fails, the `claude -p`
call is skipped entirely for that cycle -- no wasted model call, and no
"I couldn't check ClickUp" message either. Ramu Kaka never narrates its own
plumbing to its person: a failed fetch is something to log and retry next
cycle, not something to apologize about on Telegram.

pending.json SCHEMA
--------------------
A single JSON array at the top level (not an object-with-array, to keep it
trivially "empty means no work" -- `[]` is both the empty state and valid
JSON). Each element is an object:

    {
      "id":         string, unique. Recommend f"{source}-{yyyymmddThhmmss}-{shortrand}".
      "source":     "clickup" | "calendar" | "manual",
      "summary":    string, one line, human-readable -- e.g.
                    "ClickUp abc123 (Quarterly report) due in 3 days, no update since 5 Sep",
                    "Calendar: dentist appt moved to 15:00 today",
                    "Manual: asked to be reminded about X".
      "priority":   "low" | "medium" | "high".
      "created_at": ISO 8601 string with offset, e.g. "2026-09-12T19:45:00+00:00".
      "status":     "new" | "processed" | "dismissed".
                    Written as "new" by the clickup/calendar fetch loops.
                    The queue-drain loop's `claude -p` call is the one that
                    changes "new" -> "processed" (folded into something said
                    to the user) or "new" -> "dismissed" (decided not to
                    surface), and this daemon then physically removes anything
                    not "new" the next time it writes the file (see
                    _compact_pending below) so the file doesn't grow forever.
      "detail":     OPTIONAL, longer free-text context for the entry.
      "clickup_task_id":   OPTIONAL, present when source == "clickup".
      "calendar_event_id": OPTIONAL, present when source == "calendar".
    }

This is intentionally separate from state.json's `open_questions` / `queue` /
`commitments` arrays, which serve overlapping-but-different purposes.
pending.json is a clean surface: "things a background check noticed that a
human hasn't seen yet." Reconciling the two is future work.

CONCURRENCY LOCK
-----------------
<project dir>/.daemon.lock holds a one-line JSON blob:
    {"pid": 12345, "acquired_at": "2026-09-12T19:45:00+00:00"}
Every one of this daemon's timers (telegram reply, clickup fetch, calendar
fetch, queue drain, ...) calls `LOCK.acquire()` immediately before it would
shell out to `claude -p`, and `LOCK.release()` right after. acquire() is
NON-BLOCKING: if the lock is already held, it returns False immediately, the
caller logs a skip, and waits for its own next cycle -- it does NOT queue up
or retry within the same cycle (the Telegram path is the exception, see
RELIABILITY FIXES). This keeps the Telegram path from feeling laggy behind a
slow ClickUp check.
If a lock file exists but is older than `lock_stale_seconds`
(daemon-config.json, default 600s) OR its PID is no longer a running process,
it is treated as abandoned (e.g. the daemon crashed mid-call) and reclaimed
rather than wedging the whole daemon forever.
All timers are threads in one `daemon.py` run, so a plain in-process
threading.Lock would already prevent overlap -- the file-based lock is used
anyway (a) so there is a visible lockfile an operator can inspect/rm by hand
if something ever wedges, and (b) so that if launchd ever double-launches
this script, or someone runs a second copy by hand, the second instance's
calls still back off instead of racing the first.

LOGGING
--------
Plain append-only text lines to <project dir>/daemon.log:
    2026-09-12T19:45:03+00:00 [telegram] 1 new message, invoking claude
    2026-09-12T19:45:07+00:00 [telegram] replied, 214 chars
    2026-09-12T20:00:00+00:00 [clickup] lock busy, skipping this cycle
    2026-09-12T20:15:00+00:00 [queue] pending.json empty, nothing to do
    2026-09-12T20:15:00+00:00 [ERROR] [calendar] claude -p exited 1: <stderr>
One line per event, size-based rotation (daemon.log -> daemon.log.1) past
`log_max_bytes` in daemon-config.json, so it never grows unbounded.
launchd's own stdout/stderr redirection is a second, coarser safety net for
things that happen before this module's own logger is even set up (e.g. a
Python syntax error).

RELIABILITY FIXES (2026-09-14) -- ways an incoming message could be silently
dropped, and how each is closed:
  1. A Telegram reply that lost the lock race used to just `continue` and
     give up -- the message was already consumed from Telegram's offset by
     that point (listen.sh's own invariant), so it was gone forever, no log
     trace beyond "lock busy". Fixed: telegram_loop now retries acquiring the
     lock for up to `telegram_lock_retry_seconds` (default 120s, polling every
     `telegram_lock_retry_poll_seconds`) before truly giving up -- and if it
     still can't get the lock, it now sends a short, hardcoded (no claude -p
     call) fallback reply instead of silence, so the user always hears
     SOMETHING.
  2. A `claude -p` call that timed out (180s) also just logged an ERROR and
     sent nothing. Fixed: same hardcoded fallback reply on any run_claude
     failure in the Telegram path -- never total silence.
  3. `launchctl unload` (or any SIGTERM) gave in-flight threads only 5 seconds
     to finish before the process died, which could kill a thread mid-`claude
     -p`-call after the subprocess had already returned but before
     `send_telegram()` ran. Fixed: shutdown now waits up to
     `shutdown_grace_seconds` (default 200s, comfortably longer than
     `claude_timeout_seconds`) for threads to actually finish, not 5s.
  4. Separately (a correctness bug, not a reliability one): the clickup/
     calendar fetch loops re-considered the same overdue task/event every
     single cycle with no memory of having already surfaced it, so the
     queue-drain step kept re-adding it with manufactured, increasingly
     anxious urgency language instead of recognizing it had already been
     said. Fixed: `_recent_pending_ids()` now enforces a real cooldown in
     code (`resurface_cooldown_hours`, default 6) -- an item that was
     surfaced recently is filtered out of the fetch itself, before any
     `claude -p` judgment call even sees it. This is deliberately a
     code-level guarantee, not a prompt instruction, because the
     prompt-level version of this rule already existed and still failed in
     practice.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import signal
import string
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta

import ramu_blockers
import ramu_capabilities
import ramu_media
import ramu_memory
import ramu_checkins
import ramu_friction
import ramu_learning
import ramu_lookahead
import ramu_health
import ramu_goals
import ramu_radar
import ramu_footprint
import ramu_ingest
import ramu_promises
import ramu_pinggate
import ramu_profile
import ramu_tasks
import ramu_telegram

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PROJECT_DIR, "daemon-config.json")
PENDING_PATH = os.path.join(PROJECT_DIR, "pending.json")
LOCK_PATH = os.path.join(PROJECT_DIR, ".daemon.lock")
LOG_PATH = os.path.join(PROJECT_DIR, "daemon.log")
LISTEN_SH = os.path.join(PROJECT_DIR, "listen.sh")
SAY_SH = os.path.join(PROJECT_DIR, "say.sh")
# The user's standing instructions for the agent (profile: instructions_file).
INSTRUCTIONS_PATH = os.path.join(PROJECT_DIR, ramu_profile.PROFILE.get("instructions_file") or "CLAUDE.md")
DAYBUILD_STATE_PATH = os.path.join(PROJECT_DIR, "daybuild_state.json")
PATTERN_STATE_PATH = os.path.join(PROJECT_DIR, "pattern_state.json")
CONVERSATION_HISTORY_PATH = os.path.join(PROJECT_DIR, "conversation_history.json")
EMAIL_WATCHES_PATH = os.path.join(PROJECT_DIR, "email_watches.json")
ENV_PATH = os.path.join(PROJECT_DIR, ".env")  # gitignored; holds TELEGRAM_BOT_TOKEN
TELEGRAM_MEDIA_DIR = os.path.join(PROJECT_DIR, "telegram_media")  # downloaded photos land here
# Durable memory (2026-09-20) -- what Ramu Kaka itself learns from living with
# its person. Distinct from MEMORY_DIR below, which is the curated Claude Code
# auto-memory that interactive sessions maintain. Gitignored; see ramu_memory.py.
MEMORY_STORE_DIR = os.path.join(PROJECT_DIR, "memory")
MEMORY_CONSOLIDATE_STAMP = os.path.join(MEMORY_STORE_DIR, ".last_consolidate")
# Capability library (2026-09-20): things Ramu Kaka has worked out how to do. See
# ramu_capabilities.py. Gitignored.
CAPABILITIES_DIR = os.path.join(PROJECT_DIR, "capabilities")
# Blockers (2026-09-20): what Ramu Kaka is waiting on, and the inbox the watcher uses to
# wake it through the normal reply pipeline. See ramu_blockers.py.
BLOCKERS_PATH = os.path.join(PROJECT_DIR, "blockers.json")
SYNTHETIC_INBOX_PATH = os.path.join(PROJECT_DIR, "synthetic_inbox.json")
_synthetic_lock = threading.Lock()
# RPC inbox (2026-09-21): the round-trip door. The synthetic inbox above is
# one-way -- whatever goes in gets answered on Telegram, which is right for the
# blocker watcher but useless for a caller that wants the answer back. An RPC
# request carries a request_id; its reply is written to RPC_RESPONSES_DIR under
# that id for the caller to collect. Used by ramu_mcp.py so ChatGPT (or any MCP
# client) can hold an actual conversation. Same prompt, same memory, same
# conversation history as Telegram -- one Ramu Kaka, two doors.
RPC_INBOX_PATH = os.path.join(PROJECT_DIR, "rpc_inbox.json")
RPC_RESPONSES_DIR = os.path.join(PROJECT_DIR, "rpc_responses")
_rpc_lock = threading.Lock()
PRESENCE_PATH = os.path.join(PROJECT_DIR, "presence.json")  # live device facts (ramu_ingest.py)
DEVICE_LOG_PATH = os.path.join(PROJECT_DIR, "device_events.jsonl")  # raw device events, local only
PLACES_PATH = os.path.join(PROJECT_DIR, "places.json")  # the user's named places, learned from "I'm at X"
OVERNIGHT_STATE_PATH = os.path.join(PROJECT_DIR, "overnight_state.json")
FOOTPRINT_PATH = os.path.join(PROJECT_DIR, "footprint.json")  # inbox life-signals (ramu_footprint.py)
WEEKLY_STATE_PATH = os.path.join(PROJECT_DIR, "weekly_state.json")
PEOPLE_PATH = os.path.join(PROJECT_DIR, "people.json")  # relationship radar (ramu_radar.py)
GOALS_PATH = os.path.join(PROJECT_DIR, "goals.json")  # goals with live progress (ramu_goals.py)
HEALTH_STATE_PATH = os.path.join(PROJECT_DIR, "health_state.json")  # feed monitor (ramu_health.py)
LEARNING_STATE_PATH = os.path.join(PROJECT_DIR, "learning_state.json")  # daily learning digest
FRICTIONS_PATH = os.path.join(PROJECT_DIR, "frictions.json")  # self-improvement loop (ramu_friction.py)

# Last time each loop/feed actually worked (in-memory; health_loop reads it).
_heartbeats: dict[str, str] = {}
_started_at = None


def _beat(name: str) -> None:
    _heartbeats[name] = datetime.now(_TZ).isoformat(timespec="seconds")
PROMISES_PATH = os.path.join(PROJECT_DIR, "promises.json")  # scheduled "I'll ping you at X" (ramu_promises.py)
CONVERSATION_LOG_PATH = os.path.join(PROJECT_DIR, "conversation_log.jsonl")  # full, append-only
MEMORY_REFLECT_STATE_PATH = os.path.join(PROJECT_DIR, "memory_reflect_state.json")


def load_env_file(path: str) -> None:
    """Minimal .env loader (KEY=VALUE per line, # comments, blank lines
    skipped) -- no external dependency needed for one variable. Added
    2026-09-14 so daemon.py can talk to Telegram's raw Bot API directly for
    file downloads, which Composio's Telegram toolkit does not expose."""
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())
    except FileNotFoundError:
        pass
# Claude Code's per-project auto-memory dir: ~/.claude/projects/<slug>/memory/,
# where <slug> is the project's absolute path with every non-alphanumeric char turned into "-".
MEMORY_DIR = os.path.join(
    os.path.expanduser("~/.claude/projects"),
    re.sub(r"[^A-Za-z0-9]", "-", PROJECT_DIR),
    "memory",
) + os.sep

_DEFAULT_CONFIG = {
    "telegram_poll_interval_seconds": 45,
    "clickup_check_interval_seconds": 600,
    "calendar_check_interval_seconds": 600,
    "queue_drain_interval_seconds": 900,
    "lock_stale_seconds": 600,
    "claude_timeout_seconds": 180,
    "claude_binary": "claude",
    "claude_model": "sonnet",
    "log_max_bytes": 5 * 1024 * 1024,
    "project_dir": PROJECT_DIR,
    "mail_enabled": False,
    "whatsapp_enabled": False,
    # Reliability fixes (2026-09-14) so an incoming message is never silently
    # dropped -- see module docstring section "RELIABILITY FIXES (2026-09-14)".
    "telegram_lock_retry_seconds": 120,  # how long to keep retrying the lock
                                          # for a Telegram reply before giving up
    "telegram_lock_retry_poll_seconds": 3,
    "shutdown_grace_seconds": 200,  # > claude_timeout_seconds, so an in-flight
                                     # claude -p call gets to actually finish
                                     # and send its reply before the process dies
    "resurface_cooldown_hours": 6,  # don't re-surface the same ClickUp task /
                                     # calendar event within this many hours of
                                     # it last being surfaced, no matter how
                                     # overdue ClickUp still thinks it is
    # Day-build (added 2026-09-14): "ClickUp stores WHAT, Calendar stores
    # WHEN". This was in the original design and got dropped when the daemon
    # replaced the old routine. Runs automatically, once a day, no manual
    # trigger needed. Hours named *_local below are local hours in the
    # profile's timezone (the suffix is historical).
    "daybuild_check_interval_seconds": 1800,  # cheap check; only actually
                                               # builds once/day (see below)
    "daybuild_trigger_hour_local": 9,  # earliest local hour it's allowed to
                                      # build today's day
    "daybuild_max_anchors": 6,       # "4-6 anchors maximum"
    # Added 2026-09-14 so the day's specific plan never overwrites the
    # standing task's own definition. These are ClickUp ids of recurring
    # TEMPLATE tasks (the daily-rhythm tasks from the instructions file), not
    # day-specific tasks -- when an anchor is tied to one of these, day-build
    # creates a real subtask for today's instance instead of
    # overwriting/annotating the template. Set in daemon-config.json.
    "daily_rhythm_task_ids": [],
    # Added 2026-09-15 so a habit-relevant statement is recorded, not just
    # replied to. These map well-being areas to standing ClickUp "HQ" task ids;
    # a habit-relevant statement gets logged as a timestamped ClickUp comment
    # on the matching one, not a new data store. Set the ids in
    # daemon-config.json; an empty id means that area isn't tracked.
    "hq_task_ids": {
        "training": "",     # Health / exercise
        "nutrition": "",    # Nutrition
        "habits": "",       # Habits
        "activity": "",     # Activity
        "appearance": "",   # Self-care
        # Added 2026-09-22: sleep tracking reuses the same comment-log
        # mechanism as everything else, not a new data store.
        "sleep": "",        # Sleep
    },
    # Pattern-reading layer, added 2026-09-15: logging (above) captures raw
    # facts; this is what actually reads the history back and says something
    # only when a genuine, multi-data-point trend exists. Runs once/day,
    # deliberately -- patterns don't change hour to hour, and checking too
    # often risks manufacturing a "trend" out of noise.
    "pattern_check_trigger_hour_local": 20,  # evening, after the day's data exists
    "pattern_check_min_comments": 3,  # don't even ask the model for a trend
                                       # off fewer real data points than this
    # Quiet hours, added 2026-09-15: no routine reminders overnight. Starting
    # default -- adjust to the user's real sleep pattern.
    # Enforced in CODE (queue_drain_loop), not just a prompt instruction --
    # prompt-only rules eventually get ignored.
    "quiet_hours_start_local": 0,   # midnight
    "quiet_hours_end_local": 8,     # 8am
    # Ping gate (see ramu_pinggate.py). Unsolicited messages are held in CODE:
    # no override, one unanswered at a time. The 2026-09-22 gap and daily cap
    # were removed on 2026-09-23 -- they silenced Ramu Kaka too long. Instead, an
    # unanswered message gets chased, harder the more it matters.
    "chase_poll_interval_seconds": 60,
    # Tap-to-answer check-ins (ramu_checkins.py): one silent opener, then each
    # tap triggers the next question, up to this many questions per 30 min.
    "checkin_poll_interval_seconds": 600,
    "checkin_chain_max": 3,
    # Speed, 2026-09-22: long-poll Telegram (returns the moment the user writes) and
    # keep one Claude session across turns (--resume), so a reply is one short
    # call on a warm, cached context instead of a cold call that re-reads files.
    "telegram_long_poll_seconds": 20,
    "telegram_session_max_turns": 20,
    # Background workers (ramu_tasks.py), up to this many at once.
    "worker_max_concurrent": 3,
    "worker_timeout_seconds": 900,
    # Device inbox (ingest_loop): bind addresses. Add your machine's Tailscale
    # address in daemon-config.json if you want devices on your tailnet to
    # reach it; never 0.0.0.0.
    # 127.0.0.1 is for `tailscale serve --https=8443` (tailnet-only HTTPS), since
    # some phone apps refuse plain http and the macOS Tailscale app can only
    # front a local address.
    # 127.0.0.1:8790, not 8787: the ChatGPT MCP door (ramu_mcp.py) owns 127.0.0.1:8787.
    "ingest_bind": ["127.0.0.1:8790"],
    "ingest_port": 8787,
    # Overnight shift: one worker per night inside this local-time window, once
    # the user has been silent this long.
    "overnight_window_local": [1, 6],
    "overnight_min_silence_minutes": 60,
    "telegram_session_max_age_hours": 12,
    # Conversation memory, added 2026-09-16: every `claude -p` call is a
    # brand-new process with zero memory of any prior exchange, so context
    # must be carried forward explicitly. This keeps a short rolling window of
    # recent turns, fed into every Telegram reply prompt.
    "conversation_history_max_turns": 16,  # ~8 exchanges; a window, not a full log
    # Email watch, added 2026-09-19, so "I'll keep an eye out" is backed by a
    # real mechanism: a watch list, checked periodically
    # against the user's personal Gmail (see GMAIL_PERSONAL_ACCOUNT), surfaced through the same pending.json/
    # quiet-hours pipeline as everything else once a real match is found.
    "email_watch_check_interval_seconds": 1800,  # 30 min; email isn't urgent-fast
    # Durable memory, added 2026-09-20 (see ramu_memory.py). Recall is chosen
    # by relevance x importance x recency and packed into this budget --
    # it is NOT a dump of every memory.
    "memory_enabled": True,
    "memory_recall_budget_tokens": 700,
    "memory_max_entries": 400,
    # Nightly reflection (2026-09-20): one claude call per day, only if the
    # user actually talked that day, to keep what the in-the-moment reply missed.
    "memory_reflect_trigger_hour_local": 22,
    "memory_reflect_max_turns": 60,
    "capabilities_enabled": True,
    "capabilities_recall_budget_tokens": 500,
    "capabilities_max_recall": 4,
    # Blockers: an idle registry costs nothing (no open blockers -> no composio call).
    "blockers_enabled": True,
    "blockers_check_interval_seconds": 90,
    "blockers_expire_days": 5,
    # RPC (2026-09-21): the round-trip door used by ramu_mcp.py. Idle cost is a
    # local file read, same as the queue drain -- no claude call unless someone
    # actually asked something. Poll fast: a caller is sitting there waiting.
    "rpc_enabled": True,
    "rpc_poll_interval_seconds": 3,
    "rpc_response_ttl_seconds": 900,
    "rpc_max_queue": 20,
}


def load_config() -> dict:
    cfg = dict(_DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r") as f:
            on_disk = json.load(f)
        # Only pull in keys we know about; ignore the "_foo_comment" keys.
        for k in _DEFAULT_CONFIG:
            if k in on_disk:
                cfg[k] = on_disk[k]
    except FileNotFoundError:
        log(f"WARNING: {CONFIG_PATH} not found, using built-in defaults")
    except (json.JSONDecodeError, OSError) as e:
        log(f"WARNING: could not read {CONFIG_PATH} ({e}), using built-in defaults")
    return cfg


# ---------------------------------------------------------------------------
# Logging (simple, append-only, size-rotated)
# ---------------------------------------------------------------------------

_log_lock = threading.Lock()


def _now_iso() -> str:
    # The profile's timezone, to match how the user and state.json talk about time.
    return datetime.now(ramu_profile.TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"{_now_iso()} {msg}"
    print(line, flush=True)
    with _log_lock:
        try:
            max_bytes = _DEFAULT_CONFIG["log_max_bytes"]
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > max_bytes:
                rotated = LOG_PATH + ".1"
                try:
                    os.replace(LOG_PATH, rotated)
                except OSError:
                    pass
            with open(LOG_PATH, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass  # logging must never crash the daemon


# ---------------------------------------------------------------------------
# Concurrency lock (file-based, PID + timestamp)
# ---------------------------------------------------------------------------


class ClaudeLock:
    """Non-blocking file lock guarding every `claude -p` invocation.

    acquire() returns True/False immediately -- callers must not block on it.
    See the big module docstring above ("CONCURRENCY LOCK") for the full
    reasoning.
    """

    def __init__(self, path: str, stale_after_seconds: int):
        self.path = path
        self.stale_after_seconds = stale_after_seconds
        self._inproc = threading.Lock()

    def _read_existing(self):
        try:
            with open(self.path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def _is_stale(self, holder: dict) -> bool:
        pid = holder.get("pid")
        acquired_at = holder.get("acquired_at")
        if not pid or not acquired_at:
            return True
        try:
            acquired_dt = datetime.fromisoformat(acquired_at)
        except ValueError:
            return True
        age = (datetime.now(acquired_dt.tzinfo) - acquired_dt).total_seconds()
        if age > self.stale_after_seconds:
            return True
        try:
            os.kill(pid, 0)  # raises if pid doesn't exist
        except ProcessLookupError:
            return True
        except PermissionError:
            pass  # exists, just not ours -- not stale
        return False

    def acquire(self, tag: str) -> bool:
        # In-process fast path: two threads in *this* process never both
        # proceed even if the file dance below has a tiny race window.
        if not self._inproc.acquire(blocking=False):
            log(f"[{tag}] lock busy (in-process), skipping this cycle")
            return False

        existing = self._read_existing()
        if existing is not None and not self._is_stale(existing):
            self._inproc.release()
            log(
                f"[{tag}] lock busy (held by pid {existing.get('pid')} "
                f"since {existing.get('acquired_at')}), skipping this cycle"
            )
            return False

        if existing is not None:
            log(
                f"[{tag}] reclaiming stale lock from pid {existing.get('pid')} "
                f"(acquired {existing.get('acquired_at')})"
            )

        try:
            with open(self.path, "w") as f:
                json.dump({"pid": os.getpid(), "acquired_at": _now_iso()}, f)
        except OSError as e:
            log(f"[{tag}] could not write lock file: {e}")
            self._inproc.release()
            return False

        return True

    def release(self) -> None:
        try:
            os.remove(self.path)
        except OSError:
            pass
        self._inproc.release()


# ---------------------------------------------------------------------------
# claude -p invocation
# ---------------------------------------------------------------------------


def run_claude(prompt: str, config: dict, tag: str) -> tuple[bool, str]:
    """Runs `claude -p --model sonnet "<prompt>"`. Returns (ok, stdout_text).

    Background work runs on the configured `claude_model` (Sonnet by
    default), never Opus.
    """
    cmd = [
        config["claude_binary"],
        "-p",
        "--model",
        config["claude_model"],
        prompt,
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=config["claude_timeout_seconds"],
        )
    except subprocess.TimeoutExpired:
        log(f"[{tag}] ERROR: claude -p timed out after {config['claude_timeout_seconds']}s")
        return False, ""
    except OSError as e:
        log(f"[{tag}] ERROR: could not launch claude -p: {e}")
        return False, ""

    if result.returncode != 0:
        stderr_tail = (result.stderr or "").strip()[-500:]
        log(f"[{tag}] ERROR: claude -p exited {result.returncode}: {stderr_tail}")
        return False, ""

    return True, (result.stdout or "").strip()


def run_claude_session(prompt: str, config: dict, tag: str, resume_id: str | None = None, tools: str | None = None) -> tuple[bool, str, str | None]:
    """Like run_claude, but returns the session id and can resume one.
    Returns (ok, text, session_id). Sonnet only, same as run_claude."""
    cmd = [config["claude_binary"], "-p", "--model", config["claude_model"], "--output-format", "json"]
    if tools is not None:
        # Enforced in code, not in the prompt: the fast lane cannot do tool work.
        cmd += ["--tools", tools, "--strict-mcp-config"]
    if resume_id:
        cmd += ["--resume", resume_id]
    cmd.append(prompt)
    try:
        result = subprocess.run(
            cmd, cwd=PROJECT_DIR, capture_output=True, text=True,
            timeout=config["claude_timeout_seconds"],
        )
    except subprocess.TimeoutExpired:
        log(f"[{tag}] ERROR: claude -p timed out after {config['claude_timeout_seconds']}s")
        return False, "", None
    except OSError as e:
        log(f"[{tag}] ERROR: could not launch claude -p: {e}")
        return False, "", None
    if result.returncode != 0:
        log(f"[{tag}] ERROR: claude -p exited {result.returncode}: {(result.stderr or '').strip()[-300:]}")
        return False, "", None
    raw = (result.stdout or "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return True, raw, None  # not JSON: treat as plain text, no session to keep
    if not isinstance(data, dict) or data.get("is_error"):
        log(f"[{tag}] ERROR: claude -p reported an error result")
        return False, "", None
    return True, str(data.get("result") or "").strip(), data.get("session_id")


# ---------------------------------------------------------------------------
# Composio direct fetch (ClickUp / Calendar) -- no LLM involved, plain API
# calls via the `composio` CLI, same mechanism say.sh/listen.sh already use
# for Telegram. See the module docstring ("WHY THE CLICKUP/CALENDAR LOOPS
# FETCH VIA composio execute") for why this replaced MCP-tool-based fetching.
# ---------------------------------------------------------------------------

# Every id below comes from the profile (profile.json). Empty = not connected;
# the code that needs it skips quietly.
CLICKUP_LIFE_OS_LIST_ID = str(ramu_profile.PROFILE.get("clickup_list_id") or "")
CLICKUP_USER_ID = ramu_profile.PROFILE.get("clickup_user_id")
# Composio can have more than one Gmail account connected (e.g. work and
# personal); toolkits with several connections need `--account <word_id>` to
# pick the right one. Verify which is which with GMAIL_GET_PROFILE rather
# than assuming. Ramu Kaka only ever reads the personal one.
GMAIL_PERSONAL_ACCOUNT = ramu_profile.PROFILE.get("gmail_personal_account") or ""
# Work calendar. "primary" as an alias 403s through some Composio
# connections -- the explicit calendar id (usually the account's address) is
# required (verified 2026-09-13). Read-only uses only (the 30h-lookahead
# ping, and day-build's conflict check below). Never a write target: the
# agent's personal planning never goes onto a company calendar.
CALENDAR_ID = ramu_profile.PROFILE.get("work_calendar_id") or ""
WORK_CALENDAR_ACCOUNT = ramu_profile.PROFILE.get("work_calendar_account") or ""
# Added 2026-09-22: personal Google Calendar -- day-build's actual write
# target. Personal planning must never be written to the work calendar, so
# create_calendar_event and fetch_today_calendar_events use these, never
# CALENDAR_ID. Check each connected account's primary calendar with
# GOOGLECALENDAR_LIST_CALENDARS when filling these in.
PERSONAL_CALENDAR_ID = ramu_profile.PROFILE.get("personal_calendar_id") or ""
PERSONAL_CALENDAR_ACCOUNT = ramu_profile.PROFILE.get("personal_calendar_account") or ""
SLACK_ACCOUNT = ramu_profile.PROFILE.get("slack_account") or ""
SLACK_TEAM_ID = ramu_profile.PROFILE.get("slack_team_id") or ""
# The user's name for prompts, and third-person pronoun forms.
USER_NAME = ramu_profile.NAME
_NAME_CAP = USER_NAME[:1].upper() + USER_NAME[1:]  # for the start of a sentence
_PRONOUNS = {"he": ("he", "him", "his"), "she": ("she", "her", "her")}
PRONOUN_SUBJ, PRONOUN_OBJ, PRONOUN_POSS = _PRONOUNS.get(
    (ramu_profile.PRONOUN or "").lower(), ("they", "them", "their"))


def _work_context_block() -> str:
    """The profile's free-text work context (what's in or out of scope), as a
    prompt block. Empty when the profile has none."""
    wc = (ramu_profile.PROFILE.get("work_context") or "").strip()
    return f"About {USER_NAME}'s work and what's out of scope for you: {wc}\n---\n" if wc else ""


_apps_cache: dict = {"at": 0.0, "block": ""}


def _composio_apps_block(ttl: int = 3600) -> str:
    """Every app live in the user's Composio network, for the reply and the workers,
    so "what can you connect to?" gets the full live list, not a guess."""
    if time.time() - _apps_cache["at"] < ttl and _apps_cache["block"]:
        return _apps_cache["block"]
    try:
        exe = shutil.which("composio") or os.path.expanduser("~/.local/bin/composio")
        out = subprocess.run([exe, "connections", "list"], cwd=PROJECT_DIR,
                             capture_output=True, text=True, timeout=60).stdout
        conns = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return _apps_cache["block"]
    live = sorted(k for k, v in conns.items() if any(c.get("status") == "ACTIVE" for c in v or []))
    dead = sorted(k for k in conns if k not in live)
    personal = []
    if GMAIL_PERSONAL_ACCOUNT:
        personal.append("Gmail " + GMAIL_PERSONAL_ACCOUNT)
    if PERSONAL_CALENDAR_ACCOUNT:
        personal.append("Calendar " + PERSONAL_CALENDAR_ACCOUNT)
    block = (f"{_NAME_CAP}'s Composio network ({PRONOUN_POSS} own integrations; live list): connected = " + ", ".join(live)
             + (f". Linked once but expired, needs {PRONOUN_OBJ} to re-link = " + ", ".join(dead) if dead else "")
             + ". Workers can use any connected one through the composio CLI; for anything not listed, "
             "`composio search <app>` before ever saying you can't. "
             + ("Personal accounts: " + ", ".join(personal) + "; any other account of the same app is work, "
                "read-only at most. " if personal else "")
             + (f"Slack means only the workspace with team_id {SLACK_TEAM_ID}"
                + (f" (account {SLACK_ACCOUNT})" if SLACK_ACCOUNT else "")
                + ": check SLACK_TEST_AUTH's team_id before using it, and never touch any other Slack workspace. "
                if SLACK_TEAM_ID else "")
             + f"Known hard limits: WhatsApp and Instagram toolkits are business-only (can't read {PRONOUN_POSS} personal chats "
             f"or saved posts); Google Photos only sees photos an app uploaded, not {PRONOUN_POSS} library.\n---\n")
    _apps_cache.update(at=time.time(), block=block)
    return block


def composio_execute(slug: str, data: dict, tag: str, account: str | None = None) -> tuple[bool, dict | None]:
    """Runs `composio execute <slug> -d <json>`, returns (ok, parsed_result).

    Composio offloads large responses to a file (`storedInFile` /
    `outputFilePath` in the returned envelope) instead of inlining them --
    this is transparent to callers of this function, which always returns
    the actual parsed body either way.

    `account`: added 2026-09-19 for Gmail specifically -- there can be more
    than one connected Gmail account (e.g. work and personal), so toolkits
    with more than one connection need `--account <word_id>` to pick the
    right one. Composio's own account identifier, not an email address --
    see GMAIL_PERSONAL_ACCOUNT above.
    """
    cmd = ["composio", "execute", slug, "-d", json.dumps(data)]
    if account:
        cmd += ["--account", account]
    try:
        result = subprocess.run(
            cmd,
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        log(f"[{tag}] ERROR: composio execute {slug} timed out")
        return False, None
    except OSError as e:
        log(f"[{tag}] ERROR: could not run composio: {e}")
        return False, None

    if result.returncode != 0:
        stderr_tail = (result.stderr or "").strip()[-500:]
        log(f"[{tag}] ERROR: composio execute {slug} exited {result.returncode}: {stderr_tail}")
        return False, None

    out_text = result.stdout or ""
    i = out_text.find("{")
    if i < 0:
        log(f"[{tag}] ERROR: no JSON from composio execute {slug}: {out_text[:300]!r}")
        return False, None
    try:
        envelope = json.loads(out_text[i:])
    except json.JSONDecodeError as e:
        log(f"[{tag}] ERROR: could not parse composio output for {slug}: {e}")
        return False, None

    if not envelope.get("successful"):
        log(f"[{tag}] ERROR: composio {slug} not successful: {json.dumps(envelope)[:300]}")
        return False, None

    if envelope.get("storedInFile") and envelope.get("outputFilePath"):
        try:
            with open(envelope["outputFilePath"], "r") as f:
                return True, json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log(f"[{tag}] ERROR: could not read offloaded output for {slug}: {e}")
            return False, None

    return True, envelope


def _recent_pending_ids(source: str, cooldown_hours: float) -> set:
    """IDs (clickup_task_id or calendar_event_id) already surfaced in
    pending.json for this source within the last cooldown_hours, regardless
    of status (new/processed/dismissed all count -- a dismissal is still a
    surfacing). Used to enforce a real, code-level cooldown so the same
    overdue task doesn't get re-considered every single fetch cycle just
    because ClickUp still thinks it's overdue. See "RELIABILITY FIXES
    (2026-09-14)" in the module docstring."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)
    id_field = "clickup_task_id" if source == "clickup" else "calendar_event_id"
    ids = set()
    for entry in _read_pending():
        if entry.get("source") != source:
            continue
        item_id = entry.get(id_field)
        if not item_id:
            continue
        try:
            created = datetime.fromisoformat(entry["created_at"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        if created >= cutoff:
            ids.add(item_id)
    return ids


def fetch_clickup_due_soon(tag: str, cooldown_hours: float) -> list[dict] | None:
    """Compact list of Life OS tasks due within 3 days (or overdue), open
    only, MINUS anything already surfaced within cooldown_hours (see
    _recent_pending_ids). Returns None on fetch failure (caller must skip the
    claude -p call entirely in that case -- see module docstring)."""
    if not CLICKUP_LIFE_OS_LIST_ID:
        return None  # ClickUp not configured in the profile
    horizon_ms = int((datetime.now(timezone.utc) + timedelta(days=3)).timestamp() * 1000)
    ok, parsed = composio_execute(
        "CLICKUP_GET_TASKS",
        {
            "page": 0,
            "list_id": CLICKUP_LIFE_OS_LIST_ID,
            "include_closed": False,
            "due_date_lt": horizon_ms,
        },
        tag,
    )
    if not ok or not parsed:
        return None
    tasks = ((parsed.get("data") or {}).get("tasks")) or []
    recent_ids = _recent_pending_ids("clickup", cooldown_hours)
    compact = []
    for t in tasks:
        if t.get("id") in recent_ids:
            continue  # real cooldown -- already surfaced recently, skip
        compact.append({
            "id": t.get("id"),
            "name": t.get("name"),
            "due_date_ms": t.get("due_date"),
            "status": (t.get("status") or {}).get("status"),
            "date_updated_ms": t.get("date_updated"),
        })
    return compact


def fetch_calendar_next_30h(tag: str, cooldown_hours: float) -> list[dict] | None:
    """Compact list of calendar events in the next 30 hours, MINUS anything
    already surfaced within cooldown_hours. Returns None on fetch failure
    (caller must skip the claude -p call entirely)."""
    if not CALENDAR_ID:
        return None  # work calendar not configured in the profile
    now = datetime.now(timezone.utc)
    ok, parsed = composio_execute(
        "GOOGLECALENDAR_EVENTS_LIST",
        {
            "calendarId": CALENDAR_ID,
            "timeMin": now.isoformat(),
            "timeMax": (now + timedelta(hours=30)).isoformat(),
            "singleEvents": True,
            "orderBy": "startTime",
        },
        tag,
    )
    if not ok or not parsed:
        return None
    items = ((parsed.get("data") or {}).get("items")) or []
    recent_ids = _recent_pending_ids("calendar", cooldown_hours)
    compact = []
    for e in items:
        if e.get("id") in recent_ids:
            continue  # real cooldown -- already surfaced recently, skip
        start = e.get("start") or {}
        end = e.get("end") or {}
        compact.append({
            "id": e.get("id"),
            "summary": e.get("summary"),
            "start": start.get("dateTime") or start.get("date"),
            "end": end.get("dateTime") or end.get("date"),
        })
    return compact


# ---------------------------------------------------------------------------
# Email watch (added 2026-09-19). A promise like "I'll keep an eye out" needs
# a real mechanism behind it, or it's a lie, even an accidental one. This is
# that mechanism: a persistent watch list,
# checked periodically against the user's personal Gmail (via Composio,
# GMAIL_PERSONAL_ACCOUNT), surfaced
# through the same pending.json/quiet-hours pipeline as everything else.
# ---------------------------------------------------------------------------


def _read_email_watches() -> list[dict]:
    try:
        with open(EMAIL_WATCHES_PATH, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _write_email_watches(watches: list[dict]) -> None:
    try:
        with open(EMAIL_WATCHES_PATH, "w") as f:
            json.dump(watches, f, indent=2)
    except OSError as e:
        log(f"[email] WARNING: could not write email_watches.json: {e}")


def create_email_watch(query: str, description: str, tag: str) -> bool:
    """Called from the Telegram reply flow when the user asks to be told when
    something arrives by email. Only real, honest way to back that promise
    -- see module comment above."""
    watches = _read_email_watches()
    watches.append({
        "id": _short_id("email"),
        "query": query,
        "description": description,
        "created_at": _now_iso(),
        "status": "watching",
    })
    _write_email_watches(watches)
    log(f"[{tag}] email watch created: {query!r} ({description!r})")
    return True


def fetch_gmail_matches(query: str, tag: str, max_results: int = 5) -> list[dict] | None:
    """Searches the user's PERSONAL Gmail (never the work one) for `query`.
    Returns compact [{id, subject, sender, timestamp}], or None on fetch
    failure (caller must not treat that as 'no matches'). None too when no
    personal Gmail account is configured in the profile."""
    if not GMAIL_PERSONAL_ACCOUNT:
        return None
    ok, parsed = composio_execute(
        "GMAIL_FETCH_EMAILS",
        {"query": query, "max_results": max_results},
        tag,
        account=GMAIL_PERSONAL_ACCOUNT,
    )
    if not ok or not parsed:
        return None
    messages = (parsed.get("data") or {}).get("messages") or []
    return [
        {
            "id": m.get("messageId") or m.get("id"),
            "subject": m.get("subject"),
            "sender": m.get("sender"),
            "timestamp": m.get("messageTimestamp"),
        }
        for m in messages
    ]


def email_watch_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    tag = "email"
    interval = config["email_watch_check_interval_seconds"]
    log(f"[{tag}] loop starting, interval={interval}s")
    while not stop_event.is_set():
        watches = _read_email_watches()
        active = [w for w in watches if w.get("status") == "watching"]
        if not active:
            log(f"[{tag}] no active watches, nothing to check (0 composio calls)")
        else:
            changed = False
            for w in active:
                matches = fetch_gmail_matches(w["query"], tag)
                if matches is None:
                    log(f"[{tag}] fetch failed for watch {w['id']!r}, will retry next cycle")
                    continue
                if matches:
                    top = matches[0]
                    summary = (
                        f"Email found for a tracked thing ({w.get('description', w['query'])}): "
                        f"\"{top.get('subject')}\" from {top.get('sender')}."
                    )
                    pending = _read_pending()
                    pending.append({
                        "id": _short_id("email"),
                        "source": "email",
                        "summary": summary,
                        "priority": "medium",
                        "created_at": _now_iso(),
                        "status": "new",
                    })
                    try:
                        with open(PENDING_PATH, "w") as f:
                            json.dump(pending, f, indent=2)
                    except OSError as e:
                        log(f"[{tag}] WARNING: could not write pending.json: {e}")
                    w["status"] = "found"
                    changed = True
                    log(f"[{tag}] match found for watch {w['id']!r}, queued and marked found")
                else:
                    log(f"[{tag}] watch {w['id']!r}: no match yet")
            if changed:
                _write_email_watches(watches)
        stop_event.wait(interval)
    log(f"[{tag}] loop stopped")


# ---------------------------------------------------------------------------
# Day-build (added 2026-09-14): "ClickUp stores WHAT, Calendar stores WHEN"
# is the actual architecture -- this reads ClickUp priorities and
# writes 4-6 anchor blocks onto the calendar, automatically, once a day.
# Same Composio-direct pattern as everything else: Python fetches and
# creates, claude -p only judges WHAT the anchors should be.
# ---------------------------------------------------------------------------

# The user's local timezone (profile: timezone). Every "today", "hour" and
# quiet-hours check below is in this zone.
_TZ = ramu_profile.TZ


def fetch_life_os_open_tasks(tag: str) -> list[dict] | None:
    """All open (non-closed) Life OS tasks, no due-date filter -- day-build
    needs the full priority picture, not just what's due soon. Returns None
    on fetch failure.

    FIXED 2026-09-14: without each task's own detail (only its name), the
    model writes generic anchors that don't say what to actually do. Requests
    `include_markdown_description` and passes a capped excerpt through so
    the model can write something a person could act on."""
    if not CLICKUP_LIFE_OS_LIST_ID:
        return None  # ClickUp not configured in the profile
    ok, parsed = composio_execute(
        "CLICKUP_GET_TASKS",
        {
            "page": 0,
            "list_id": CLICKUP_LIFE_OS_LIST_ID,
            "include_closed": False,
            "include_markdown_description": True,
        },
        tag,
    )
    if not ok or not parsed:
        return None
    tasks = ((parsed.get("data") or {}).get("tasks")) or []
    result = []
    for t in tasks:
        desc = t.get("text_content") or t.get("description") or ""
        result.append({
            "id": t.get("id"),
            "name": t.get("name"),
            "due_date_ms": t.get("due_date"),
            "status": (t.get("status") or {}).get("status"),
            "priority": ((t.get("priority") or {}) or {}).get("priority"),
            "detail": desc[:400],
        })
    return result


def fetch_today_calendar_events(tag: str) -> list[dict] | None:
    """Today's existing events on the PERSONAL calendar, local-time day
    boundaries -- so day-build never proposes something that collides with
    what's already there. Returns None on fetch failure.

    FIXED 2026-09-22: was reading CALENDAR_ID (work) -- see PERSONAL_CALENDAR_ID
    comment above. Reads the calendar day-build actually writes to now."""
    if not PERSONAL_CALENDAR_ID:
        return None  # personal calendar not configured in the profile
    now_local = datetime.now(_TZ)
    start_of_day = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = start_of_day + timedelta(days=1)
    ok, parsed = composio_execute(
        "GOOGLECALENDAR_EVENTS_LIST",
        {
            "calendarId": PERSONAL_CALENDAR_ID,
            "timeMin": start_of_day.isoformat(),
            "timeMax": end_of_day.isoformat(),
            "singleEvents": True,
            "orderBy": "startTime",
        },
        tag,
        account=PERSONAL_CALENDAR_ACCOUNT,
    )
    if not ok or not parsed:
        return None
    items = ((parsed.get("data") or {}).get("items")) or []
    compact = []
    for e in items:
        start = e.get("start") or {}
        end = e.get("end") or {}
        ext = (e.get("extendedProperties") or {}).get("private") or {}
        compact.append({
            "id": e.get("id"),
            "summary": e.get("summary"),
            "start": start.get("dateTime") or start.get("date"),
            "end": end.get("dateTime") or end.get("date"),
            "ramu_kaka_daybuild": ext.get("ramu_kaka_daybuild"),
            "clickup_task_id": ext.get("clickup_task_id"),
        })
    return compact


def fetch_events_ahead(tag: str, hours: int = 48, personal: bool = True) -> list[dict] | None:
    """Events from now to +hours, personal (editable) or work (read-only)."""
    if not (PERSONAL_CALENDAR_ID if personal else CALENDAR_ID):
        return None  # that calendar isn't configured in the profile
    now = datetime.now(_TZ)
    ok, parsed = composio_execute(
        "GOOGLECALENDAR_EVENTS_LIST",
        {"calendarId": PERSONAL_CALENDAR_ID if personal else CALENDAR_ID, "timeMin": now.isoformat(),
         "timeMax": (now + timedelta(hours=hours)).isoformat(), "singleEvents": True, "orderBy": "startTime"},
        tag, account=PERSONAL_CALENDAR_ACCOUNT if personal else None,
    )
    if not ok or not parsed:
        return None
    out = []
    for e in ((parsed.get("data") or {}).get("items")) or []:
        start, end = e.get("start") or {}, e.get("end") or {}
        ev = {"start": start.get("dateTime") or start.get("date"), "end": end.get("dateTime") or end.get("date"),
              "summary": e.get("summary")}
        if personal:
            ev.update(id=e.get("id"), location=e.get("location"), description=(e.get("description") or "")[:500])
        out.append(ev)
    return out


def fetch_today_company_calendar_events(tag: str) -> list[dict] | None:
    """Today's events on the WORK calendar, READ-ONLY -- so day-build can
    avoid scheduling a personal anchor on top of a real company meeting
    (work sources are read only to catch things that affect the user's
    personal day). Never written to. Returns None on fetch failure (caller should still
    proceed with personal-only conflict checking rather than block on this --
    it's a nice-to-have overlap check, not the write path)."""
    if not CALENDAR_ID:
        return None  # work calendar not configured in the profile
    now_local = datetime.now(_TZ)
    start_of_day = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = start_of_day + timedelta(days=1)
    ok, parsed = composio_execute(
        "GOOGLECALENDAR_EVENTS_LIST",
        {
            "calendarId": CALENDAR_ID,
            "timeMin": start_of_day.isoformat(),
            "timeMax": end_of_day.isoformat(),
            "singleEvents": True,
            "orderBy": "startTime",
        },
        tag,
        account=WORK_CALENDAR_ACCOUNT,
    )
    if not ok or not parsed:
        return None
    items = ((parsed.get("data") or {}).get("items")) or []
    compact = []
    for e in items:
        start = e.get("start") or {}
        end = e.get("end") or {}
        compact.append({
            "id": e.get("id"),
            "summary": e.get("summary"),
            "start": start.get("dateTime") or start.get("date"),
            "end": end.get("dateTime") or end.get("date"),
            "source": "company_calendar_readonly",
        })
    return compact


def create_calendar_event(anchor: dict, tag: str) -> bool:
    """Creates one day-build anchor as a private calendar event, tagged via
    extended_properties so future day-builds can recognize it and never
    duplicate it. `anchor` needs summary, start_datetime, end_datetime, and
    optionally clickup_task_id/description.

    BUG FIXED 2026-09-14: start_datetime/end_datetime are naive (no
    timezone suffix) per build_daybuild_prompt's instructions -- Composio's
    schema says a naive datetime defaults to UTC unless a `timezone` field
    is given. Without it, the very first live run created every anchor
    offset by the user's UTC offset -- found by fetching the created events
    back and comparing, not assumed correct. Always pass the profile's
    timezone explicitly now.

    BUG FIXED 2026-09-22: was writing to CALENDAR_ID (work) -- see
    PERSONAL_CALENDAR_ID comment above. Personal anchors never go there."""
    if not PERSONAL_CALENDAR_ID:
        return False  # never fall back to another calendar
    data = {
        "calendar_id": PERSONAL_CALENDAR_ID,
        "summary": anchor.get("summary", "Ramu Kaka"),
        "start_datetime": anchor["start_datetime"],
        "end_datetime": anchor["end_datetime"],
        "timezone": ramu_profile.PROFILE.get("timezone") or "UTC",
        "visibility": "private",
        "transparency": "opaque",
        "create_meeting_room": False,  # these are personal life blocks, not meetings
        "extended_properties": {
            "private": {
                "ramu_kaka_daybuild": "1",
                "clickup_task_id": str(anchor.get("clickup_task_id") or ""),
            }
        },
    }
    # Always append the ClickUp deep-link ourselves, in code -- not left to
    # the model to remember. Fixed 2026-09-14: an opened anchor had no way
    # to get from "what is this" to the actual task with
    # instructions. https://app.clickup.com/t/<id> is ClickUp's own task
    # permalink format.
    desc_parts = []
    if anchor.get("description"):
        desc_parts.append(anchor["description"])
    task_id = anchor.get("clickup_task_id")
    if task_id:
        desc_parts.append(f"https://app.clickup.com/t/{task_id}")
    if desc_parts:
        data["description"] = "\n\n".join(desc_parts)
    ok, _parsed = composio_execute("GOOGLECALENDAR_CREATE_EVENT", data, tag, account=PERSONAL_CALENDAR_ACCOUNT)
    return ok


def create_daily_subtask(parent_task_id: str, name: str, description: str, list_id: str, tag: str) -> str | None:
    """Added 2026-09-14, replacing an earlier "append a note to the parent"
    attempt -- the real fix: today's specific instance of
    a recurring daily-rhythm slot should be its OWN task, a
    ClickUp subtask of the standing template, not text stuffed into the
    template's description. This gives real per-day tracking (each subtask
    completable on its own, visible in ClickUp history) and stops the
    "which one do I actually look at" confusion -- the calendar event links
    to THIS subtask, never the parent template. Returns the new subtask's
    id, or None on failure."""
    due_dt = datetime.now(_TZ).replace(hour=23, minute=59, second=0, microsecond=0)
    data = {
        "name": name,
        "list_id": list_id,
        "parent": parent_task_id,
        "description": description,
        "due_date": int(due_dt.timestamp() * 1000),
    }
    ok, parsed = composio_execute("CLICKUP_CREATE_TASK", data, tag)
    if not ok or not parsed:
        return None
    return (parsed.get("data") or {}).get("id")


def create_clickup_task(name: str, priority: str, list_id: str, tag: str, description: str = "", due_in_days: int | None = None) -> bool:
    """Idea-capture (added 2026-09-14): a new idea from the user becomes a
    ClickUp task with a priority instead of them working on it immediately.
    priority: 'urgent'|'high'|'normal'|'low' -> ClickUp's 1-4 integer scale.
    description: optional -- carries the pressure-test line for
    business/startup ideas (see build_telegram_reply_prompt).

    FIXED 2026-09-14: tasks created with no due date at all were invisible
    to fetch_clickup_due_soon (it filters by due_date_lt) -- a captured idea
    would never resurface on its own. Default a due date from priority if
    the model didn't give one, so nothing gets silently orphaned."""
    priority_map = {"urgent": 1, "high": 2, "normal": 3, "low": 4}
    default_due_days = {"urgent": 1, "high": 3, "normal": 7, "low": 14}
    data = {"name": name, "list_id": list_id}
    if priority in priority_map:
        data["priority"] = priority_map[priority]
    if description:
        data["description"] = description
    days = due_in_days if due_in_days is not None else default_due_days.get(priority, 7)
    due_dt = datetime.now(timezone.utc) + timedelta(days=days)
    data["due_date"] = int(due_dt.timestamp() * 1000)
    ok, _parsed = composio_execute("CLICKUP_CREATE_TASK", data, tag)
    return ok


def close_clickup_task(task_id: str, tag: str) -> bool:
    """Added 2026-09-15 so "that's done" / "no longer needed" over Telegram
    actually closes the task -- resurface_cooldown only delays re-nagging, it
    is not a resolution. Assumes the list's ClickUp status values are
    "to do" (open) / "complete" (closed); check yours via CLICKUP_GET_LIST."""
    ok, _parsed = composio_execute(
        "CLICKUP_UPDATE_TASK", {"task_id": task_id, "status": "complete"}, tag,
    )
    return ok


def log_hq_comment(hq_key: str, note: str, hq_task_ids: dict, tag: str) -> bool:
    """Added 2026-09-15 so a habit-related update told to Ramu Kaka is
    recorded, not just replied to. Rather than invent a new
    data store, this logs a timestamped comment on the relevant HQ task
    (training/nutrition/habits/activity/appearance/sleep) -- ClickUp already holds
    "what needs to happen"; this extends it to also hold "what happened",
    queryable via that task's comment history. hq_key must be one of the
    keys in config["hq_task_ids"]; unknown keys are refused rather than
    guessing a task id."""
    task_id = hq_task_ids.get(hq_key)
    if not task_id:
        log(f"[{tag}] hq_key {hq_key!r} has no task id configured (hq_task_ids), not logged")
        return False
    stamp = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M")
    ok, _parsed = composio_execute(
        "CLICKUP_CREATE_TASK_COMMENT",
        {
            "task_id": task_id,
            "comment_text": f"[{stamp}] {note}",
            "notify_all": False,
            "assignee": CLICKUP_USER_ID,
        },
        tag,
    )
    return ok


def fetch_hq_comment_history(hq_task_ids: dict, tag: str) -> dict[str, list[str]] | None:
    """Pattern-reading layer, added 2026-09-15. Fetches recent comments off
    every HQ task -- log_hq_comment's write side, this is the read side.
    Returns {hq_key: [comment_text, ...]}, oldest-relevant-first isn't
    guaranteed (ClickUp's own order is used as-is; the model doing the
    pattern judgment doesn't need strict ordering, just the data). Returns
    None only on a fetch failure that affects ALL keys (e.g. composio itself
    down); a single key failing just comes back with an empty list for that
    key so one bad HQ task doesn't block reading the other four."""
    history: dict[str, list[str]] = {}
    any_ok = False
    for hq_key, task_id in hq_task_ids.items():
        if not task_id:
            continue  # area not configured
        ok, parsed = composio_execute("CLICKUP_GET_TASK_COMMENTS", {"task_id": task_id}, tag)
        if not ok or not parsed:
            history[hq_key] = []
            continue
        any_ok = True
        comments = (parsed.get("data") or {}).get("comments") or []
        history[hq_key] = [
            (c.get("comment_text") or c.get("comment") or "").strip()
            for c in comments if (c.get("comment_text") or c.get("comment"))
        ]
    return history if any_ok else None


def build_pattern_prompt(history: dict[str, list[str]], min_comments: int) -> str:
    total = sum(len(v) for v in history.values())
    return (
        _COMMON_CONTEXT_PREFIX
        + _VOICE_GUARDRAIL
        + "This is the pattern-reading pass over the habit logs Ramu "
        "Kaka keeps -- logging alone isn't a system, noticing what the "
        "log actually shows is. Here is the recent comment history per HQ "
        "area (training/nutrition/habits/activity/appearance), oldest-first "
        "or however ClickUp returned it: " + json.dumps(history) + f". "
        f"There are {total} total comments across all areas right now. "
        f"HARD RULE: do not manufacture a pattern from fewer than "
        f"{min_comments} real, relevant data points in a single area -- if "
        "there isn't enough history yet to say something honest, output "
        "nothing rather than pad a thin observation into something that "
        "sounds like insight. A pattern worth surfacing is a real, "
        "countable trend (e.g. 'slept under 6 hours on 4 of the last 6 nights "
        "logged, up from the first week' or 'gym logged 0 times in 10 days despite "
        "being anchored most days') -- not a vague summary, and never "
        "moralizing (be supportive, never moralize -- state the "
        "count, never 'you should'). At most ONE pattern, the single most "
        "worth mentioning right now, chosen the same way any other open "
        "loop is chosen -- not one per HQ area every time this runs. "
        "Output ONLY a JSON object, no markdown fences, no prose outside "
        "it: "
        '{"pattern": null} if nothing is genuinely worth surfacing, or '
        '{"pattern": {"hq_key": "training|nutrition|habits|activity|'
        'appearance|sleep", "summary": "<one plain factual sentence, the actual '
        'countable trend, no judgment language>", "priority": '
        '"low|medium|high"}} if there is exactly one real pattern. Do not '
        "wrap this in prose, do not explain your reasoning outside the JSON."
    )


def pattern_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    tag = "pattern"
    interval = config["daybuild_check_interval_seconds"]  # reuse: cheap check, same cadence style as day-build
    trigger_hour = config["pattern_check_trigger_hour_local"]
    min_comments = config["pattern_check_min_comments"]
    hq_task_ids = config.get("hq_task_ids", {})
    log(f"[{tag}] loop starting, interval={interval}s, trigger_hour={trigger_hour} local")
    while not stop_event.is_set():
        today = datetime.now(_TZ).strftime("%Y-%m-%d")
        now_hour = datetime.now(_TZ).hour
        state = {}
        try:
            with open(PATTERN_STATE_PATH, "r") as f:
                state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

        if state.get("last_checked_date") == today:
            pass  # already checked today
        elif now_hour < trigger_hour:
            pass  # too early
        else:
            history = fetch_hq_comment_history(hq_task_ids, tag)
            if history is None:
                log(f"[{tag}] fetch failed, will retry next cycle (no claude call)")
            elif sum(len(v) for v in history.values()) < min_comments:
                log(f"[{tag}] not enough logged history yet "
                    f"({sum(len(v) for v in history.values())} total comments) -- "
                    f"skipping, marking checked so this doesn't re-run all day")
                try:
                    with open(PATTERN_STATE_PATH, "w") as f:
                        json.dump({"last_checked_date": today}, f)
                except OSError as e:
                    log(f"[{tag}] WARNING: could not write state: {e}")
            elif lock.acquire(tag):
                try:
                    ok, out = run_claude(_prepend_memory(build_pattern_prompt(history, min_comments),
                        "habits training nutrition sleep mood " + json.dumps(history)[:1500], config, tag), config, tag)
                    if not ok:
                        log(f"[{tag}] claude call failed, will retry next cycle")
                    else:
                        try:
                            envelope = json.loads(out) if out else {}
                            pattern = envelope.get("pattern")
                        except (json.JSONDecodeError, AttributeError):
                            pattern = None
                        if pattern:
                            # Reuse the existing queue/pending.json path
                            # rather than a new send mechanism -- this goes
                            # through the same one-open-loop-per-message
                            # discipline as everything else.
                            entry = {
                                "id": f"pattern-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{random.randint(1000,9999)}",
                                "source": "pattern",
                                "summary": pattern.get("summary", ""),
                                "priority": pattern.get("priority", "medium"),
                                "created_at": _now_iso(),
                                "status": "new",
                            }
                            pending = _read_pending()
                            pending.append(entry)
                            try:
                                with open(PENDING_PATH, "w") as f:
                                    json.dump(pending, f, indent=2)
                                log(f"[{tag}] found a real pattern, queued: {entry['summary'][:150]!r}")
                            except OSError as e:
                                log(f"[{tag}] WARNING: could not write pending.json: {e}")
                        else:
                            log(f"[{tag}] checked, no pattern worth surfacing today")
                        try:
                            with open(PATTERN_STATE_PATH, "w") as f:
                                json.dump({"last_checked_date": today}, f)
                        except OSError as e:
                            log(f"[{tag}] WARNING: could not write state: {e}")
                finally:
                    lock.release()
        stop_event.wait(interval)
    log(f"[{tag}] loop stopped")


def build_daybuild_prompt(tasks: list[dict], existing_events: list[dict], max_anchors: int) -> str:
    now_local = datetime.now(_TZ)
    return (
        _COMMON_CONTEXT_PREFIX
        + _VOICE_GUARDRAIL
        + f"The current date and time, stated explicitly so there's no "
        f"ambiguity: {now_local.strftime('%A, %Y-%m-%d, %H:%M %Z')}. "
        "Every anchor's start_datetime MUST be at or after the current time "
        "above. Do not plan an idealized full day from scratch -- plan what "
        "actually still fits between now and a reasonable end to the day. "
        "If something like a morning routine has already passed, it's "
        "simply not an anchor today, not something to schedule anyway. "
        "It's time to build today's day. Here are the open Life OS tasks "
        "(pre-fetched via Composio): " + json.dumps(tasks) + ". Here are "
        "today's EXISTING calendar events, already on the calendar (also "
        "pre-fetched) -- events with ramu_kaka_daybuild=1 were already "
        "created by a previous day-build run and must NOT be duplicated; "
        "events with source=company_calendar_readonly are read-only "
        f"awareness of {PRONOUN_POSS} COMPANY calendar -- never create or "
        "edit anything there, just don't "
        "place a personal anchor on top of one; anything else is a real "
        "commitment (meeting, etc.) that anchors must not overlap: "
        + json.dumps(existing_events) + ". "
        "DEFAULT DAY SHAPE: if the instructions file or the Life OS tasks "
        f"describe a default day template for {USER_NAME}, that is the "
        "template on a normal day, not just one option among many -- read "
        "any task it points to before writing the anchor, don't invent a "
        "program. Any anchor that suggests an outside event or place must "
        "be dated and verifiable; if no verifiable option exists for today "
        "specifically, say so in the anchor's description rather than "
        "inventing one -- a well-corroborated recurring option is fine to "
        "name with its confidence noted, an invented one-off is not. A "
        "template does not automatically consume the whole "
        f"{max_anchors}-anchor budget -- a genuinely urgent aging task "
        "still competes for a slot per the PRIORITY rule below, and if the "
        "day is already mostly spoken for (existing events above), skip "
        "whichever template block no longer fits rather than forcing it. "
        f"Pick at most {max_anchors} anchors for today -- this is a "
        "hard ceiling, not a target ('4-6 anchors "
        "maximum, never a rigid lifetime timetable'). Fewer is fine if the "
        "day's real scope is thin; do not pad to hit the max. For each "
        "anchor, pick a specific, realistic start/end time that does not "
        f"overlap any existing event above, in {ramu_profile.PROFILE.get('timezone') or 'UTC'} "
        "local time (the current local time and date are stated above). "
        "PRIORITY: ClickUp's own priority field is a real signal, not "
        "decoration. An 'urgent' or 'high' item that's overdue or aging "
        "beats a generic, no-priority daily-rhythm placeholder for a slot "
        "-- if something urgent has been sitting for days, it needs a slot "
        "TODAY, even if that means a daily-rhythm task doesn't get one. "
        "The daily-rhythm tasks from the person's instructions file are the "
        "standing skeleton, not "
        "automatic winners -- fold a genuinely urgent item INTO one of "
        "those slots where it fits (e.g. a generic exercise slot becomes the "
        "specific session that's due) rather than defaulting to "
        "the placeholder's own generic wording. "
        "DESCRIPTION: each task above includes a "
        "'detail' field pulled from its real ClickUp description -- use it. "
        "Write the anchor's description as an actual instruction a person "
        "could follow (what to do, any specifics from 'detail' that matter "
        "-- where, what the backup plan was, why it matters), not a "
        "restatement of the task name. A ClickUp link is added "
        "automatically afterward -- don't add one yourself, and don't "
        "worry about which ClickUp task it points to: for the standing "
        "daily-rhythm tasks, code (not you) automatically creates a real "
        "subtask for today's specific instance and links there instead of "
        "the template -- so this anchor's own description IS what ends up "
        "on that subtask. Write it accordingly: a one-off task's link goes "
        "straight to that task (it already has real detail), a daily-rhythm anchor's description becomes today's "
        "dedicated subtask content, detailed enough to actually act on. "
        f"This is proactive, unprompted -- {USER_NAME} did not just ask for "
        f"this, so once the day is built, tell {PRONOUN_OBJ}. Output ONLY a JSON object, "
        "nothing else, no markdown fences, no prose outside the JSON, "
        "shaped exactly like: "
        '{"anchors": [{"summary": "<short, plain, like a person labeling '
        'their own day>", "start_datetime": "<YYYY-MM-DDTHH:MM:SS>", '
        '"end_datetime": "<YYYY-MM-DDTHH:MM:SS>", '
        '"clickup_task_id": "<id or null>", '
        '"description": "<the real, detailed, actionable instruction -- '
        'this becomes either the linked task\'s content or today\'s '
        'dedicated subtask content, see above>"}], '
        f'"announcement": "<the message to actually send {USER_NAME} on Telegram, '
        "one to three short sentences per the length rule above -- naming "
        "what got placed, not a report. If anchors is empty, a brief line "
        'saying today didn\'t need anything new is fine.">}. '
        "If today already has enough real commitments and nothing more "
        'needs anchoring, output {"anchors": [], "announcement": "<short '
        'line>"}.'
    )


def daybuild_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    tag = "daybuild"
    interval = config["daybuild_check_interval_seconds"]
    trigger_hour = config["daybuild_trigger_hour_local"]
    max_anchors = config["daybuild_max_anchors"]
    log(f"[{tag}] loop starting, interval={interval}s, trigger_hour={trigger_hour} local")
    while not stop_event.is_set():
        today = datetime.now(_TZ).strftime("%Y-%m-%d")
        now_hour = datetime.now(_TZ).hour
        state = {}
        try:
            with open(DAYBUILD_STATE_PATH, "r") as f:
                state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

        if state.get("last_built_date") == today:
            pass  # already built today, nothing to do
        elif now_hour < trigger_hour:
            pass  # too early, wait for the trigger hour
        else:
            tasks = fetch_life_os_open_tasks(tag)
            events = fetch_today_calendar_events(tag)
            # Best-effort: a failed company-calendar read shouldn't block
            # building the personal day, it just means less conflict-
            # avoidance awareness this cycle (see docstring).
            company_events_raw = fetch_today_company_calendar_events(tag)
            if company_events_raw is None:
                log(f"[{tag}] company calendar read failed, proceeding without it")
            all_events = (events or []) + (company_events_raw or [])
            if tasks is None or events is None:
                log(f"[{tag}] fetch failed, will retry next cycle (no claude call)")
            elif lock.acquire(tag):
                try:
                    ok, out = run_claude(_prepend_memory(build_daybuild_prompt(tasks, all_events, max_anchors),
                        _texts(tasks, "name") + " " + _texts(all_events, "summary"), config, tag), config, tag)
                    if not ok:
                        log(f"[{tag}] claude call failed, will retry next cycle")
                    else:
                        try:
                            envelope = json.loads(out) if out else {}
                            anchors = envelope.get("anchors", [])
                            announcement = envelope.get("announcement", "")
                        except (json.JSONDecodeError, AttributeError):
                            anchors, announcement = None, ""
                        if anchors is None:
                            log(f"[{tag}] ERROR: could not parse day-build JSON: {out[:300]!r}")
                        else:
                            # Safety net, not just a prompt instruction (the
                            # prompt already says "no past anchors", but a
                            # model can ignore it). now_local has no seconds precision
                            # requirement; a couple minutes of slack for the
                            # time this whole cycle takes.
                            now_local = datetime.now(_TZ)
                            past, future = [], []
                            for a in anchors[:max_anchors]:
                                try:
                                    start = datetime.fromisoformat(a["start_datetime"]).replace(tzinfo=_TZ)
                                except (KeyError, ValueError):
                                    future.append(a)
                                    continue
                                (future if start >= now_local - timedelta(minutes=5) else past).append(a)
                            if past:
                                log(f"[{tag}] WARNING: dropped {len(past)} anchor(s) scheduled in the past: "
                                    f"{[p.get('summary') for p in past]}")
                            daily_rhythm_ids = set(config.get("daily_rhythm_task_ids", []))
                            created, subtasked = 0, 0
                            for a in future:
                                task_id = a.get("clickup_task_id")
                                if task_id in daily_rhythm_ids:
                                    # Today's specific instance of a standing
                                    # template -> its own subtask, not a
                                    # note on the template itself. Calendar
                                    # links to the subtask.
                                    sub_id = create_daily_subtask(
                                        task_id,
                                        f"{a.get('summary', 'Today')} ({now_local.strftime('%-d %b')})",
                                        a.get("description", ""),
                                        CLICKUP_LIFE_OS_LIST_ID,
                                        tag,
                                    )
                                    if sub_id:
                                        a["clickup_task_id"] = sub_id
                                        subtasked += 1
                                    else:
                                        log(f"[{tag}] WARNING: subtask creation failed for {task_id}, "
                                            f"calendar link will point at the template instead")
                                if create_calendar_event(a, tag):
                                    created += 1
                            log(f"[{tag}] built today: {created}/{len(future)} anchor(s) created, "
                                f"{subtasked} daily-rhythm subtask(s) made")
                            # Proactive: the user didn't ask for this, so
                            # tell them what got placed (2026-09-14).
                            if announcement:
                                allowed, why = _unsolicited_gate(config, tag, kind="briefing")
                                if allowed:
                                    sent = send_unsolicited(announcement, tag, config)
                                else:
                                    sent = False
                                    log(f"[{tag}] announcement held ({why}), not sent")
                                log(f"[{tag}] announced ({len(announcement)} chars), sent={sent}")
                            # Only mark done once we got a real, parseable
                            # decision -- a fetch/claude failure retries next
                            # cycle instead of silently never building today.
                            try:
                                with open(DAYBUILD_STATE_PATH, "w") as f:
                                    json.dump({"last_built_date": today}, f)
                            except OSError as e:
                                log(f"[{tag}] WARNING: could not write state: {e}")
                finally:
                    lock.release()
        stop_event.wait(interval)
    log(f"[{tag}] loop stopped")


# ---------------------------------------------------------------------------
# Telegram helpers (shell out to the SAME scripts listen.sh / say.sh use --
# no Telegram/Composio credentials are touched or duplicated here)
# ---------------------------------------------------------------------------


def download_telegram_file(file_id: str, tag: str) -> str | None:
    """Added 2026-09-14: photos sent to the bot were never actually read -- diagnosis confirmed by checking listen.sh (it already
    correctly extracts photo file_id, that part was fine) and daemon.py
    (nothing ever downloaded or looked at it -- caption or a placeholder
    like "[photo, no caption]" was all the model ever saw).

    Composio's Telegram toolkit has no file-download tool at all (checked:
    `composio search` and a direct `TELEGRAM_GET_FILE` probe both come back
    empty/404) -- Composio deliberately doesn't expose the raw bot token,
    which is exactly what Telegram's real getFile/download endpoints need.
    So this talks to Telegram's public Bot API directly, using
    TELEGRAM_BOT_TOKEN from .env (NOT the same credential Composio holds --
    the owner gets it from @BotFather in Telegram: message @BotFather,
    /mybots, select the bot, API Token).

    Returns a local file path, or None if the token isn't configured yet or
    the download fails -- callers must degrade gracefully (reply based on
    caption/text alone), never crash the loop over a missing token.

    Uses `curl` via subprocess, not Python's urllib -- verified live on this
    machine that urllib's SSL context fails with "self signed certificate
    in certificate chain" (a broken/incomplete cert store for this Python
    install, a known python.org-on-macOS issue), while `curl` -- what
    Composio's own CLI relies on -- works fine. Real error caught by
    testing, not assumed."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log(f"[{tag}] TELEGRAM_BOT_TOKEN not set in .env -- can't download image, "
            f"replying on caption/text alone")
        return None
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "15",
             f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}"],
            capture_output=True, text=True, timeout=20,
        )
        meta = json.loads(result.stdout)
        if not meta.get("ok"):
            log(f"[{tag}] ERROR: Telegram getFile failed: {json.dumps(meta)[:300]}")
            return None
        file_path = meta["result"]["file_path"]
        os.makedirs(TELEGRAM_MEDIA_DIR, exist_ok=True)
        ext = os.path.splitext(file_path)[1] or ".jpg"
        local_path = os.path.join(TELEGRAM_MEDIA_DIR, f"{file_id}{ext}")
        url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        dl = subprocess.run(
            ["curl", "-s", "--max-time", "30", "-o", local_path, url],
            capture_output=True, text=True, timeout=35,
        )
        if dl.returncode != 0 or not os.path.exists(local_path) or os.path.getsize(local_path) == 0:
            log(f"[{tag}] ERROR: curl download failed for {file_id}: {dl.stderr[-300:]}")
            return None
        return local_path
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, OSError) as e:
        log(f"[{tag}] ERROR: could not download telegram file {file_id}: {e}")
        return None


_tg_token_cache: str | None = None


def _tg_token() -> str:
    """The bot token for direct Telegram calls, or "" (then everything falls back
    to the Composio scripts). RAMU_TELEGRAM_DIRECT=0 forces the fallback."""
    global _tg_token_cache
    if os.environ.get("RAMU_TELEGRAM_DIRECT", "1") == "0":
        return ""
    if _tg_token_cache is None:
        _tg_token_cache = ramu_telegram.load_token(PROJECT_DIR)
    return _tg_token_cache


def poll_telegram(tag: str, long_poll_seconds: int = 0) -> list[dict]:
    token = _tg_token()
    if token:
        try:
            messages, dropped = ramu_telegram.poll(
                token, os.path.join(PROJECT_DIR, ".last_update"), long_poll_seconds)
            if dropped:
                log(f"[{tag}] SECURITY: dropped {dropped} update(s) from someone other than the configured chat")
            _beat("telegram")
            return messages
        except ramu_telegram.TelegramError as e:
            log(f"[{tag}] direct poll failed ({e}); falling back to listen.sh")
            long_poll_seconds = 0
    env = dict(os.environ)
    if long_poll_seconds:
        env["LISTEN_TIMEOUT"] = str(long_poll_seconds)
    try:
        result = subprocess.run(
            [LISTEN_SH],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=30 + long_poll_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired:
        log(f"[{tag}] ERROR: listen.sh timed out")
        return []
    except OSError as e:
        log(f"[{tag}] ERROR: could not run listen.sh: {e}")
        return []

    if result.returncode != 0:
        stderr_tail = (result.stderr or "").strip()[-500:]
        log(f"[{tag}] ERROR: listen.sh exited {result.returncode}: {stderr_tail}")
        return []

    _beat("telegram")
    messages = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            log(f"[{tag}] WARNING: could not parse listen.sh line: {line[:200]}")
    return messages


def send_telegram(text: str, tag: str, buttons: list | None = None, silent: bool = False) -> bool:
    if not text:
        return False
    token = _tg_token()
    if token:
        try:
            return ramu_telegram.send_message(
                token, ramu_telegram.ALLOWED_CHAT_ID, text, buttons=buttons, silent=silent)
        except ramu_telegram.TelegramError as e:
            log(f"[{tag}] direct send failed ({e}); falling back to say.sh")
    env = dict(os.environ)
    if buttons:
        env["RAMU_BUTTONS"] = json.dumps(buttons)
    if silent:
        env["RAMU_SILENT"] = "1"
    try:
        result = subprocess.run(
            [SAY_SH, text],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
    except subprocess.TimeoutExpired:
        log(f"[{tag}] ERROR: say.sh timed out")
        return False
    except OSError as e:
        log(f"[{tag}] ERROR: could not run say.sh: {e}")
        return False

    if result.returncode != 0:
        stderr_tail = (result.stderr or "").strip()[-500:]
        log(f"[{tag}] ERROR: say.sh exited {result.returncode}: {stderr_tail}")
        return False
    return True


# ---------------------------------------------------------------------------
# pending.json helpers
# ---------------------------------------------------------------------------


def _read_pending() -> list:
    try:
        with open(PENDING_PATH, "r") as f:
            data = json.load(f)
        if not isinstance(data, list):
            log(f"[queue] WARNING: pending.json is not a list, treating as empty")
            return []
        return data
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError) as e:
        log(f"[queue] WARNING: could not read pending.json ({e}), treating as empty")
        return []


def pending_is_empty() -> bool:
    """Cheap local check -- zero claude/subprocess calls. This is what makes
    an empty queue cost nothing, per the stage-1 spec's 'critical zero-idle-
    cost requirement'."""
    return not _new_pending()


def _new_pending() -> list:
    """Only entries still waiting. Feeding processed/dismissed entries back to
    the drain makes it re-raise things that were already said."""
    return [e for e in _read_pending() if e.get("status", "new") == "new"]


def _short_id(source: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{source}-{ts}-{rand}"


# ---------------------------------------------------------------------------
# Conversation memory (added 2026-09-16) -- a short rolling window of recent
# Telegram turns, the fix for every claude -p call otherwise starting from
# zero. Not a full transcript/archive -- just enough for a reply to sound
# like it remembers the last few things said, the way a real conversation
# does. Full history, if ever wanted, still lives in Telegram itself.
# ---------------------------------------------------------------------------


def _read_conversation_history() -> list[dict]:
    try:
        with open(CONVERSATION_HISTORY_PATH, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _append_conversation_turns(turns: list[dict], max_turns: int) -> None:
    _log_conversation(turns)
    history = _read_conversation_history()
    history.extend(turns)
    history = history[-max_turns:]
    try:
        with open(CONVERSATION_HISTORY_PATH, "w") as f:
            json.dump(history, f, indent=2)
    except OSError as e:
        log(f"[telegram] WARNING: could not write conversation history: {e}")


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

_THEM, _THEIR = PRONOUN_OBJ, PRONOUN_POSS
_STATE_PATH = os.path.join(PROJECT_DIR, "state.json")

_COMMON_CONTEXT_PREFIX = (
    f"You are Ramu Kaka, {USER_NAME}'s personal life-OS assistant, running as a "
    "background `claude -p` call from daemon.py (not an interactive session). "
    f"First read {INSTRUCTIONS_PATH} (especially anything on how you must "
    f"sound when you actually speak), {_STATE_PATH}, and the memory "
    f"files under {MEMORY_DIR}. "
    + _work_context_block()
)

_FAST_PREFIX = (
    f"You are Ramu Kaka, {USER_NAME}'s personal life assistant, running as a background "
    "call from daemon.py (not an interactive session). The project instructions "
    f"are already loaded and {_THEIR} memory is included below, so do not go reading "
    "files for context. "
    + _work_context_block()
)

_VOICE_GUARDRAIL = (
    "VOICE, non-negotiable: you are Ramu Kaka, a calm, capable chief-of-staff "
    "voice. Calm, precise, quick, quietly funny, always a step ahead of "
    f"{_THEM}. Not a servant waiting for orders, not a cheerleader: a chief of staff "
    f"who happens to care about {_THEM}. You already know {_THEIR} life (memory, tasks, "
    "calendar), so you speak like someone who has been paying attention. "
    "\n"
    "HOW YOU ACT: "
    "1) Lead with the outcome. 'Done', 'booked for', 'the answer is', never the "
    "process. Never narrate checking or looking. "
    "2) Act first on anything reversible or free, report after. Ask first only "
    f"for spending money, sending something as {_THEM}, or anything you can't undo, "
    "and then in one line with your default ('Sending the request at 6 "
    "unless you say otherwise'). "
    f"3) Anticipate. Surface the one thing {USER_NAME} would want to know before "
    "being asked. If there is nothing, say nothing. "
    f"4) Decide for {_THEM}. Name the pick and the order; never hand the choice back "
    "('The report, then the errand, then email. In that order.'). "
    "5) Push back once, with a reason and a recommendation ('I'd wait on that "
    f"until the deadline's met. Your call.'), then respect {_THEIR} decision. "
    "6) Verify before saying done. Never say you'll watch for something or "
    f"follow up unless it is actually scheduled or queued. If a step needs {_THEM}, "
    "name the single tap or single fact needed, nothing else. "
    f"7) Never ask for the same thing twice. Infer sleep from {_THEIR} silence. Never "
    "send more than one message at a time, and never a list of questions. "
    "\n"
    f"HOW YOU SOUND: dry wit, at most one light line, and never about {_THEIR} "
    f"health, money worries, or when {USER_NAME} is low. One apology per real mistake, "
    "brief, then the fix ('That link expired. Fresh one below.'). No 'my "
    "mistake' spirals. Reply in the language the user writes in. "
    "\n"
    f"LENGTH: match {_THEIR} register. Short in, short out, ONE TO THREE SHORT "
    "SENTENCES unless it is a genuine briefing, and even then tight. "
    "\n"
    "NEVER mention tools, MCP, connectors, APIs, or ClickUp/Calendar/Composio "
    "by name, or anything about your own plumbing. If something could not be "
    "checked, say nothing about it or fold it into one plain sentence. You are "
    f"a presence to {_THEM}, not software. "
    "\n"
    "EXAMPLES of the voice (illustrative, do not copy facts):\n"
    f"{_NAME_CAP}: 'Rough day so far'\n"
    "You: 'Noted. I've kept the afternoon light and moved the non-urgent "
    "items to tomorrow.'\n"
    f"{_NAME_CAP}: 'too many messages'\n"
    "You: 'Understood. I'll only reach out when it matters, one message at a time.'\n"
    f"{_NAME_CAP}: 'link didn't work'\n"
    "You: 'Fresh link: <link>. Good for about ten minutes.'\n"
    f"{_NAME_CAP}: 'What first?'\n"
    "You: 'The report, then the errand, then email. Everything else waits.'\n"
    "Morning, unprompted, after they surface:\n"
    "You: 'Morning. Two things today: the workout, and your afternoon call.'\n"
    "A few days with nothing logged:\n"
    "You: 'Nothing logged for a few days. One word when you can, and I'll "
    "keep the rest.'\n"
)


def _utc_offset_str() -> str:
    """The profile timezone's current UTC offset, e.g. "+05:30" or "-04:00"."""
    off = datetime.now(_TZ).utcoffset() or timedelta(0)
    mins = int(off.total_seconds() // 60)
    sign = "+" if mins >= 0 else "-"
    mins = abs(mins)
    return f"{sign}{mins // 60:02d}:{mins % 60:02d}"


_PROMISE_RULE = (
    "PROMISES: if your reply commits to doing something at a later time (\"I'll "
    "ping you at 9\", \"checking back tonight\", \"I'll remind you Tuesday "
    "10:30\"), add it to \"promises\" as {\"at\": \"<ISO time with "
    f"{_THEIR} UTC offset, {_utc_offset_str()}>\", "
    f"\"say\": \"<the exact message to send {_THEM} then>\"}}, and the daemon sends it "
    f"at that time. Add \"call\": true when {USER_NAME} asked to be CALLED (a wake-up call, \"call me at 9 "
    f"and push me\"): {_THEIR} phone rings with your voice, and the message is sent too. "
    "No entry = don't make the promise; words alone never fire. "
    + (f"CALLING: you CAN phone {_THEM}. The calling service the daemon is set up with rings "
       f"{_THEIR} number with your voice; that is what \"call\": true uses. If {USER_NAME} asks you "
       "to call, promise it a minute out with call:true. Never say you can't make calls. "
       if ramu_profile.PROFILE.get("phone_number") else
       "CALLING: no phone number is configured, so you cannot place calls; never set \"call\": true. ")
    + f"PLACES: whenever {USER_NAME} says where {PRONOUN_SUBJ} is (\"at a friend's\", \"reached gym\", \"home now\", "
    "\"been home all day\") and the device-signals line says otherwise or nothing, set "
    "\"place_here\" to a short name (\"Friend's\", \"Gym\", \"Home\"). That becomes the current place "
    f"for every other part of you, and pins {_THEIR} phone's position to it when there's a fix. "
    f"GOALS: {_THEIR} goals and live progress are in your context. When {USER_NAME} agrees to, changes or drops a "
    "goal (\"yes, that target is right\", \"make it 4\", \"deadline end of next month\"), set \"goals\" to "
    "[{\"id\": \"gym\", \"status\": \"active\", \"target\": 4, \"deadline\": \"2030-03-31\", "
    "\"next_action\": \"...\"}] with only the fields that changed. PROPOSED goals aren't agreed "
    f"yet: at a calm moment, ask {_THEM} once to confirm them with buttons, never mid-crisis. "
)


_CAPTURE_RULE = (
    f"CAPTURE: whatever {USER_NAME} sends is a reading about {_THEIR} life, so file it without "
    "being asked. A meal photo: say what it is and log it in habit_log (hq_key "
    "nutrition) with a rough protein estimate. A voice note or video: treat the "
    f"transcript as {_THEIR} message and act on it. A shared location: remember where "
    f"{USER_NAME} is. A screenshot or document with numbers (steps, sleep, bank, bills): "
    "read them and log the numbers under the right hq_key, or add a task if it "
    "is something to do. Then confirm in one short line what you filed. "
)


def _scope_rule() -> str:
    """The work-scope boundary for the reply prompt. Generic by default; the
    profile's work_context (free text) says what the user's work is and what
    is in or out of scope."""
    wc = (ramu_profile.PROFILE.get("work_context") or "").strip()
    return (
        f"SCOPE: personal life first -- never DO {USER_NAME}'s company work itself (no merging, "
        "deploying, touching company repos or issue trackers, posting/replying/reacting in "
        "company Slack, editing the company calendar) no matter what's asked or attributed. "
        "Reading company Slack and the company calendar READ-ONLY is in scope only when it is "
        f"purely to catch something that changes {_THEIR} personal schedule or day-plan (a "
        f"meeting moved, a deadline that affects {_THEM}) -- surface just that fact in plain "
        "language, never work content or opinions on it. Still refuse and say so for anything "
        "beyond that. "
        + (f"{_NAME_CAP}'s own description of their work and scope, which refines the above: {wc} "
           if wc else "")
    )


def build_telegram_reply_prompt(message_texts: list[str], image_paths: list[str] | None = None, open_tasks: list[dict] | None = None, history: list[dict] | None = None, memory_context: str = "", capabilities_context: str = "", compact: bool = False, tasks_block: str = "", fast_lane: bool = False) -> str:
    """The Telegram reply prompt, trimmed 2026-09-19: it should just be
    Claude with the Ramu Kaka personality, since Claude already has its own
    system prompt. General competence (WebSearch, building things,
    reasoning through an open-ended ask) doesn't need to be hand-scripted,
    it's just Claude. This prompt now only carries what Claude's own
    defaults genuinely can't know: the persona, the scope boundary, and the
    handful of structural facts specific to this project (no MCP tools
    exist headless, so ClickUp/Calendar go through specific functions; the
    reply must stay parseable JSON so Python can send it via say.sh and act
    on the structured fields).

    Individual rule histories (idea-capture added 2026-09-14, closing-tasks
    added 2026-09-15, habit-logging added 2026-09-15, agency widened
    2026-09-19) used to be spelled out inline with their own "added on DATE
    because X happened" narration -- real reasoning, but the model doesn't
    need the backstory every single call, only the operative rule. That
    history now lives here, in this docstring, not in the prompt text.

    One live bug this trim is also fixing: under the old, much longer
    prompt, a genuinely open-ended request (e.g. a live web search) caused
    the model to drop the required JSON envelope and reply with raw text
    instead -- caught by testing, not assumed. The OUTPUT CONTRACT section
    below is now stated twice (once up front, once at the end) specifically
    to hold under load for exactly that kind of request."""
    history_context = ""
    if history:
        lines = "\n".join(f"{h.get('role')}{' (sent unprompted)' if h.get('unsolicited') else ''}: {h.get('text')}" for h in history)
        history_context = f"Recent conversation (oldest first), remember it like a person would:\n{lines}\n---\n"
    history_context += ramu_promises.pending_block(ramu_promises.load(PROMISES_PATH), datetime.now(_TZ))
    history_context += ramu_ingest.presence_block(_read_json(PRESENCE_PATH, {}), datetime.now(_TZ))
    history_context += _orders_block()
    history_context += ramu_footprint.block(_read_json(FOOTPRINT_PATH, {}).get("signals"), datetime.now(_TZ))
    history_context += _goals_block()
    history_context += ramu_friction.block(_read_json(FRICTIONS_PATH, {}).get("frictions"), datetime.now(_TZ))
    history_context += ramu_health.block(_read_json(HEALTH_STATE_PATH, {}).get("current"))
    memory_block = ""
    if memory_context:
        memory_block = (
            f"What you remember about {USER_NAME} and {_THEIR} life (your own durable "
            "memory, most relevant first -- treat as true unless corrected, "
            "and correct it when it is):\n" + memory_context + "\n---\n"
        )
    capabilities_block = ""
    if capabilities_context:
        capabilities_block = (
            "Things you already know how to do (your own capability library, "
            "most relevant first -- prefer these over rediscovering):\n"
            + capabilities_context + "\n---\n"
        )
    tasks_context = ""
    if open_tasks:
        tasks_context = (
            f"{_NAME_CAP}'s currently open Life OS tasks, for matching anything "
            "mentioned (id/name/priority): "
            + json.dumps([{"id": t.get("id"), "name": t.get("name"), "priority": t.get("priority")} for t in open_tasks])
            + ". "
        )
    tasks_context = tasks_block + tasks_context
    if fast_lane:
        tasks_context = _now_line() + tasks_context
    if len(message_texts) == 1:
        incoming = f"{_NAME_CAP} just sent this Telegram message: " + json.dumps(message_texts[0])
    else:
        joined = "\n".join(f"{i+1}. {m}" for i, m in enumerate(message_texts))
        incoming = (
            f"{_NAME_CAP} just sent {len(message_texts)} Telegram messages in a "
            "quick burst -- treat as ONE thought, not separate requests:\n" + joined
        )
    if any(ramu_friction.sounds_low(t) for t in message_texts):
        incoming = (f"{USER_NAME.upper()} SOUNDS LOW. Load and follow the bad-day-recovery skill before anything else: "
                    f"acknowledge {_THEM} first, take something off {_THEIR} plate, no plans, schedules or food "
                    "lists unless asked.\n" + incoming)
    image_note = ""
    if image_paths:
        image_note = (
            f" {USER_NAME} also sent " + ("an attachment" if len(image_paths) == 1 else f"{len(image_paths)} attachments")
            + " (images, PDFs, or still frames from a video) -- Read it (" + (image_paths[0] if len(image_paths) == 1 else json.dumps(image_paths))
            + ") before replying, it's real content, not a placeholder."
        )
    has_media = bool(image_paths) or any(("\n[" in t) or t.startswith("[") for t in message_texts)
    if compact:
        # A resumed session already holds the persona, the contract and the files
        # read on its first turn, so this only carries what changed.
        return (
            history_context + memory_block + capabilities_block + tasks_context + incoming + image_note
            + ("\n\n" + _CAPTURE_RULE if has_media else "")
            + "\n\nSame rules as your first message in this session: your Ramu Kaka voice, "
            "one to three short sentences unless a real briefing, and your ENTIRE output "
            "is ONE JSON object per the OUTPUT CONTRACT (reply, new_tasks, close_task_ids, "
            "habit_log, email_watch, remember, forget, learn, capability_used, blocked, "
            "buttons, spawn_tasks, task_notes, promises, place_here, goals). Nothing outside the JSON. " + _PROMISE_RULE
        )
    personal_gmail = (f" ({_THEIR} personal Gmail is account {GMAIL_PERSONAL_ACCOUNT}; never the work one)"
                      if GMAIL_PERSONAL_ACCOUNT else "")
    return (
        (_FAST_PREFIX if fast_lane else _COMMON_CONTEXT_PREFIX)
        + _VOICE_GUARDRAIL
        + "\n\nOUTPUT CONTRACT (read this now, and again at the end -- hold "
        "to it even for an open-ended request that pulls you toward a "
        "normal conversational answer): your entire output must be ONE "
        "JSON object, nothing else, no markdown fences, no prose outside "
        'it: {"reply": "<text for Telegram, per the voice/length rules '
        'above>", "new_tasks": [], "close_task_ids": [], "habit_log": [], '
        '"email_watch": null, "remember": [], "forget": [], "learn": [], '
        '"capability_used": [], "blocked": [], "buttons": [], "spawn_tasks": [], "task_notes": [], "promises": [], "place_here": null, "goals": []}. '
        f"BUTTONS: save {_THEM} typing. Whenever your reply asks something a "
        "few taps can answer (yes/no, pick one, done/not yet), set \"buttons\" "
        "to two to four short labels, e.g. [\"Yes\", \"Not yet\"]. A tap comes "
        f"back to you as {_THEIR} message, so each label must read as a complete "
        "answer. Leave it [] when a tap cannot answer what you asked. " + _PROMISE_RULE + _CAPTURE_RULE + (ramu_tasks.FAST_LANE_RULE if fast_lane else "") +
        f"The \"reply\" field is the only thing that reaches {_THEM} -- if you "
        "did real work (searched, read a file, built something), say what "
        "you found/did in plain Ramu Kaka voice, not a tool-use log.\n\n"
        "AGENCY: you have real tool access -- Read, Write, Edit, Bash, "
        "WebSearch, same as an interactive Claude Code session, because "
        f"that's what this is. If {USER_NAME} asks for something actionable, do it "
        "for real this turn (research, build a file, check something) "
        "instead of just describing it. THIS BEATS IDEA CAPTURE BELOW "
        "(added 2026-09-19): a request phrased AT you "
        "-- 'can you...', 'find me...', 'check...', 'set that up', 'call "
        "them and ask...' -- is something to actually DO with your tools "
        f"right now, never something to hand back to {_THEM} as {_THEIR} own task. "
        f"Idea capture is only for something {USER_NAME} said {PRONOUN_SUBJ} wants to do/build "
        f"{_THEM}self, mentioned in passing, not a request made of you. "
        "When genuinely unsure which one it is, default to trying to "
        "actually help first. "
        + _scope_rule()
        + "COMPOSIO (`composio execute <SLUG> -d '<json>'`): READING through it is "
        f"fine -- search, list, get, fetch on {_THEIR} personal accounts{personal_gmail}. "
        "WRITING to ClickUp/Calendar/Telegram goes through the fields "
        "below, not raw composio. Any other write, and anything that "
        "reaches other people or spends money, is external: describe it "
        f"and wait for {_THEIR} yes -- {_THEIR} Composio account has far more "
        "connected than this should touch, and nobody's watching live to "
        "catch a mistake the way there is in an interactive session. If "
        "something needs a ClickUp/Calendar action with no field for it "
        "below, say so rather than improvise. "
        "SAFETY is unchanged from any Claude Code session, just with no one "
        "live to ask: no credentials/payment details, no financial action, "
        f"no permanent deletion, no messaging anyone but {USER_NAME}. Anything "
        f"that would normally need {_THEIR} live yes -- describe it and wait for "
        "an actual yes, don't act because no one's there to stop you.\n\n"
        "UNBLOCK PROTOCOL (added 2026-09-20): BEFORE you "
        f"ever tell {_THEM} you can't do something, or that you need "
        f"something from {_THEM}, work this ladder for real and report only "
        "what is genuinely left. (1) DISCOVER: `composio search "
        "\"<what is wanted>\" --limit 5` searches "
        "1000+ apps; a tool existing is not the same as it being "
        "linked. (2) CHECK what is connected: `composio connections list "
        "--toolkit <slug>` (ACTIVE = usable, INITIATED = link started, "
        f"not finished). If {USER_NAME} says {PRONOUN_SUBJ} already signed up for or connected "
        "something, VERIFY it that way (or a personal-Gmail search for "
        "the service's welcome mail) instead of asking or guessing. (3) "
        "LINK: if the toolkit exists but is not ACTIVE, run `composio "
        "link <slug> --no-wait --no-browser` yourself and put the "
        "redirect_url in your reply -- a link to tap; credentials go "
        f"into Composio, NEVER through Telegram, so never ask {_THEM} to "
        "paste an API key or password into chat. (4) REGISTER the wait "
        "in \"blocked\": [{\"goal\": \"<what is wanted>\", "
        "\"toolkit\": \"<slug>\", \"word_id\": \"<the new "
        "connection's word_id from connections list>\", \"resume\": "
        "\"<instructions to yourself for when it is live>\"}] -- the "
        "daemon watches, and wakes you with a message starting "
        f"\"[auto]\" (a note from the daemon, NOT from {USER_NAME}) the "
        f"moment it is connected; act on it, then tell {_THEM} briefly what "
        f"you did. {_NAME_CAP} should never have to remember to ask twice. (5) "
        "Only after all that, say what is still missing, specifically, "
        "in one line. Connecting a service does not lift the external "
        f"rule: buying, calling or messaging anyone but {USER_NAME} still needs "
        f"{_THEIR} yes.\n\n"
        "HIT A WALL (added 2026-09-19): don't just refuse and stop when "
        "you can't do something directly. Investigate for real -- is there "
        "an API/service for this, is something already connected that "
        "could do it, what's actually missing. Report the REAL specific "
        "gap (not a vague 'I can't'), and if it needs an account, "
        "credentials, or money to close, say exactly what's needed and "
        f"ask {_THEM} for it -- same as anything else needing {_THEIR} explicit yes. "
        "This does NOT relax SAFETY above -- 'I found a way to technically "
        "do X' is not permission to skip a required yes; it's permission "
        "to ask the right, specific question instead of giving up. And once "
        "you find a way that actually works, save it with \"learn\" (see "
        "CAPABILITIES) so you never have to rediscover it.\n\n"
        "HONESTY (added 2026-09-19; a promise with no mechanism behind it "
        "is a lie, even an accidental one): NEVER say 'I'll keep "
        "an eye on it', 'I'll track it', 'I'll let you know when...', or "
        "anything implying ongoing monitoring UNLESS you are actually "
        "setting that monitoring up right now via a real mechanism -- "
        "\"email_watch\" below for something arriving by email, a ClickUp "
        "task with a real due date for anything else worth checking back "
        "on. If nothing below can actually back the promise, say so "
        "honestly instead -- 'I can't actually watch for that yet' beats a "
        "reassuring sentence with nothing behind it, every time. SAME RULE "
        "for facts, not just promises: "
        "never state a specific fact -- "
        "hours, prices, addresses, availability -- as if verified unless "
        "you actually just checked it (WebSearch, a real fetch) THIS turn. "
        "If you're not sure, either go check it for real (see AGENCY) or "
        "say plainly that you don't know yet -- never fill the gap with "
        "something that sounds right.\n\n"
        + memory_block
        + capabilities_block
        + history_context
        + incoming
        + image_note
        + " " + tasks_context
        + f"IDEA CAPTURE: a new idea/thing {USER_NAME} wants to do or build {_THEM}self "
        "(not a request of YOU -- AGENCY above wins when it's a request), "
        "not a question, not chat, goes into \"new_tasks\" as a ClickUp "
        "task with a priority -- captured, not built inline; no spiralling "
        "into it just because it was said out loud. If it's "
        "specifically a startup/"
        "product idea (not personal/household), apply a sharp product "
        "lens first -- first user, painful current workaround, "
        "single biggest bottleneck, one or two sharp sentences, no praise "
        "-- as the task's description, and echo the sharp point in the "
        f"reply too. CLOSING TASKS: when {USER_NAME} says something is done/"
        "resolved/no longer needed, match it against the open-tasks list "
        "and put its real id in \"close_task_ids\" -- this is the only "
        "real resolution, a cooldown just delays re-asking. If unsure "
        "which task is meant, ask in the reply instead of guessing. HABIT "
        "LOGGING: anything relevant to physical/mental well-being -- "
        "habits, training done or skipped, sleep, mood, eating, "
        "grooming -- goes in \"habit_log\" every time, plainly, no "
        "judgment (be supportive, never moralize), even a mention in "
        "passing; logged quietly, no need to narrate it.\n\n"
        "CAPABILITIES: you keep a library of things you have already "
        "worked out how to do (shown above when relevant). If one fits, "
        "use it -- run its script with Bash from the project directory, "
        "or follow its steps -- and report how it went in "
        "\"capability_used\": [{\"slug\": \"...\", \"ok\": true|false, "
        "\"note\": \"<one line>\"}], honestly, ok=false if it failed. "
        "When you work out a repeatable way to do something FOR REAL this "
        "turn (a lookup, a small script, a multi-step how-to) and it "
        "actually worked, save it in \"learn\" so next time starts from "
        "the playbook, not from zero: {\"slug\": \"<kebab>\", \"title\": "
        "\"<what it does>\", \"when_to_use\": \"<when this would be asked "
        "for, in the user's own words>\", \"steps\": \"<numbered markdown "
        "steps>\", \"script\": {\"name\": \"run.sh\"|\"run.py\", "
        "\"content\": \"...\"} or null, \"declared_tier\": "
        "\"read|write|external\"}. Scripts are non-interactive, take "
        "arguments, print the result to stdout, read any credential from "
        "the environment or .env (never inline), call composio by name "
        "(`composio`, on PATH), and stay personal-"
        "life only. Only save what you ran and saw work this turn -- "
        "never a guess. Tiers: read = only looks things up; write = "
        f"changes {_THEIR} own things; external = reaches other people, spends "
        "money or cannot be undone. An external capability must NEVER run "
        f"without {_THEIR} explicit yes in this conversation, and the daemon "
        "re-checks your tier from the script itself, so label it "
        "honestly.\n\n"
        "MEMORY: you have a real, durable memory now -- what you put in "
        "\"remember\" is still there next week, in every future "
        "conversation. Save what a good household retainer would never "
        f"forget: people in {_THEIR} life (who they are to {_THEM}), places, "
        f"preferences and dislikes, decisions {USER_NAME} made and why, routines, "
        "and lessons from what worked or failed. NOT passing chatter, "
        "NOT what ClickUp already holds (tasks) or habit_log already "
        "records (habits/training/food), and NEVER credentials, card "
        "or payment details, OTPs or passwords. Use a stable, "
        "descriptive key (e.g. \"gym-trainer\") so a later correction "
        "REPLACES the old fact instead of sitting beside it; use "
        f"\"forget\" with the key when {USER_NAME} says something is no longer "
        "true or asks you to drop it. importance: 5 = never let this "
        "slip (hard rules, health facts), 3 = normal, 1 = trivia. "
        "Because this is real, \"I'll remember that\" is now an "
        "honest thing to say -- but only when you actually put it in "
        "\"remember\" this turn. "
        '"remember": [{"key": "<stable-slug>", "type": "person|place|'
        'preference|fact|event|routine|lesson", "text": "<one plain '
        'sentence>", "importance": <1-5>}]. "forget": ["<key>"]. '
        '"new_tasks": [{"name": "...", "priority": "urgent|high|normal|low", '
        '"description": "<pressure-test line for business ideas, else omit>", '
        '"due_in_days": <optional int>, '
        f'"list_id": "{CLICKUP_LIFE_OS_LIST_ID}"}}]. '
        '"close_task_ids": [<real id strings from the open-tasks list>]. '
        '"habit_log": [{"hq_key": "training|nutrition|habits|activity|'
        'appearance|sleep", "note": "<one plain factual line>"}]. "email_watch" '
        'is null, or {"query": "<a real Gmail search query -- e.g. '
        '\'from:shop.example.com\' or \'subject:shipped\'>", "description": '
        '"<what this is, for the eventual message -- e.g. \'the online '
        'order\'>"} when '
        f"{USER_NAME} has asked to be told about something "
        "arriving by email -- see HONESTY above, this is what actually "
        "backs that promise, checked periodically by a separate loop, not "
        "something you check yourself right now. Most replies need all "
        "arrays empty and email_watch null -- only populate what genuinely "
        "applies. Output the JSON object now."
    )


def build_clickup_check_prompt(clickup_tasks: list[dict]) -> str:
    return (
        _COMMON_CONTEXT_PREFIX
        + _VOICE_GUARDRAIL
        + "Here is a pre-fetched, already-filtered dump of Life OS tasks "
        "due within 3 days or overdue (fetched via Composio, not a tool call "
        "you need to make yourself, and ALREADY excludes anything surfaced "
        "in the last few hours -- that cooldown is enforced in code, you "
        "don't need to re-derive it): " + json.dumps(clickup_tasks) + ". "
        f"Judge which of these, if any, are worth surfacing to {USER_NAME} -- due, "
        "genuinely upcoming, or something whose staleness itself is "
        "notable. Do NOT manufacture urgency language tied to elapsed time "
        "('it's night now', 'two days now', 'already come and gone') -- if "
        "a task is stale, say so once, plainly, without narrating the clock. "
        "If a task describes a plan for a day that has already passed, treat "
        "it as moot, not urgent -- worth mentioning it should probably be "
        "closed, not worth escalating. If something qualifies, append ONE "
        "entry per "
        f"qualifying item to {PENDING_PATH} (read "
        "the file first, it is a JSON array -- append, don't overwrite "
        "existing entries), each shaped exactly like: "
        '{"id": "<clickup-YYYYMMDDThhmmssZ-xxxx>", "source": "clickup", '
        '"summary": "<one line, plain language, no task-management jargon>", '
        '"priority": "low|medium|high", "created_at": "<ISO8601 now>", '
        '"status": "new", "clickup_task_id": "<task id>"}. '
        "Do not add an entry that duplicates one already in pending.json "
        "with status \"new\" for the same task. If nothing qualifies, make "
        "no changes to pending.json and output nothing. This is a data-entry "
        f"step only -- nothing here is ever spoken to {USER_NAME} directly; the "
        "queue-drain step does that later."
    )


def build_calendar_check_prompt(calendar_events: list[dict]) -> str:
    return (
        _COMMON_CONTEXT_PREFIX
        + _VOICE_GUARDRAIL
        + "Here is a pre-fetched dump of calendar events in the next 30 "
        "hours (fetched via Composio, not a tool call you need to make "
        f"yourself; this is {_THEIR} work calendar): "
        + json.dumps(calendar_events) + ". "
        "Judge whether anything here is worth surfacing -- a genuine "
        "conflict, something close with no travel buffer accounted for, or "
        "a notable change since pending.json's existing calendar entries. "
        "If something qualifies, append ONE entry per qualifying item to "
        f"{PENDING_PATH} (read the file first, it "
        "is a JSON array -- append, don't overwrite existing entries), each "
        "shaped exactly like: "
        '{"id": "<calendar-YYYYMMDDThhmmssZ-xxxx>", "source": "calendar", '
        '"summary": "<one line, plain language>", "priority": '
        '"low|medium|high", "created_at": "<ISO8601 now>", "status": "new", '
        '"calendar_event_id": "<event id>"}. '
        "Do not add an entry that duplicates one already in pending.json "
        "with status \"new\" for the same event. If nothing qualifies, make "
        "no changes to pending.json and output nothing. This is a data-entry "
        f"step only -- nothing here is ever spoken to {USER_NAME} directly."
    )


def build_queue_drain_prompt(pending: list[dict], quiet_hours_active: bool) -> str:
    quiet_note = ""
    if quiet_hours_active:
        quiet_note = (
            f" IT IS CURRENTLY INSIDE {USER_NAME.upper()}'S QUIET HOURS. "
            "Nothing you output can "
            "send a message during quiet "
            "hours -- the daemon holds it, there is no override, so leave "
            "\"message\" null and let the entries wait. "
        )
    return (
        _COMMON_CONTEXT_PREFIX
        + _VOICE_GUARDRAIL
        + quiet_note
        + "Here is pending.json's current content, already read for you: "
        + json.dumps(pending) + ". Decide what to do with entries whose "
        "status is \"new\", per the voice rules above and in the instructions file -- "
        "especially the open-loop rule: AT MOST ONE narrated message "
        "(never a list, never more than one open loop folded in), choosing "
        "the single oldest-or-most-pressing entry if several qualify. This "
        "does NOT restate what's already in state.json's open_questions/"
        "queue -- those are separate and out of scope here. "
        "Do NOT call ./say.sh yourself and do NOT write pending.json "
        "yourself -- output your decision as JSON only, the daemon does the "
        "actual send and file write so the quiet-hours rule above is "
        "enforced in code, not left to instruction alone (rules that were "
        "prompt-only eventually got ignored at least once). "
        "Output ONLY a JSON object, no markdown fences, no prose outside "
        "it: {\"message\": <the exact text to send, or null if nothing "
        "should be sent right now>, \"processed_ids\": [<ids folded into "
        f"the message, if any>], \"urgency\": <\"chat\" | \"important\" | \"urgent\" -- how hard to chase {_THEM} if there's no answer. chat: nice to talk about, fine if it waits (most things). important: affects today and {USER_NAME} needs to decide or act today. Habit or health observations (a habit slip, a skipped workout, food) and anything about Ramu Kaka's own plumbing (tunnels, dashboards, links) are ALWAYS chat, never important. urgent: something happens within the next hour or so and {USER_NAME} must act (leave now, a meeting moved up, a deadline about to pass). Be honest -- urgent ends in a phone call.>, \"dismissed_ids\": [<ids you've decided are "
        "not worth ever surfacing -- stale, no longer relevant>], "
        "\"override_quiet_hours_reason\": \"<non-empty only for a genuine "
        "emergency exception during quiet hours, see above; omit or empty "
        "otherwise>\"}. Entries not in processed_ids or dismissed_ids stay "
        "\"new\" for next time. If nothing actually warrants a message "
        "right now, message should be null -- do not manufacture a reason "
        "to speak."
    )


# ---------------------------------------------------------------------------
# Loops
# ---------------------------------------------------------------------------


# Hardcoded, no claude -p call -- used ONLY when we genuinely cannot get a
# real reply out (lock contention exhausted its retry budget, or the model
# call itself timed out/errored). Never silence; see RELIABILITY FIXES
# (2026-09-14) in the module docstring. Deliberately plain and human, not an
# apology-bot line -- it still has to sound like Ramu Kaka, not a system.
_FALLBACK_REPLY = (
    "Got that, still with you -- caught up in something else for a second, "
    "give me a moment and I'll actually get back to it properly."
)


def _recall_memory(texts: list[str], history: list[dict], config: dict, tag: str) -> str:
    """Relevant durable memory for this exchange. The query is what the user
    just said plus the last couple of turns, so a short "yes, that one" still
    pulls in what "that one" refers to. Never raises -- memory is an
    enhancement, and a broken store must not cost the user a reply."""
    if not config.get("memory_enabled"):
        return ""
    try:
        recent = " ".join(h.get("text", "") for h in (history or [])[-2:])
        out = ramu_memory.recall(
            " ".join(texts) + " " + recent, MEMORY_STORE_DIR,
            budget_tokens=config["memory_recall_budget_tokens"],
        )
        if out:
            log(f"[{tag}] memory: recalled {out.count(chr(10)) + 1} item(s)")
        return out
    except Exception as e:  # noqa: BLE001
        log(f"[{tag}] memory recall failed (continuing without): {e}")
        return ""


def _apply_memory_writes(remember: list, forget: list, config: dict, tag: str, source: str = "telegram") -> None:
    if not config.get("memory_enabled") or not (remember or forget):
        return
    try:
        for r in ramu_memory.remember(remember, source=source, mem_dir=MEMORY_STORE_DIR,
                                      max_entries=config["memory_max_entries"]):
            log(f"[{tag}] memory: {r}")
        for r in ramu_memory.forget(forget, MEMORY_STORE_DIR):
            log(f"[{tag}] memory: {r}")
    except Exception as e:  # noqa: BLE001
        log(f"[{tag}] memory write failed: {e}")


def _recall_capabilities(texts: list[str], history: list[dict], config: dict, tag: str) -> str:
    """Capabilities relevant to this exchange (never raises)."""
    if not config.get("capabilities_enabled"):
        return ""
    try:
        recent = " ".join(h.get("text", "") for h in (history or [])[-2:])
        out = ramu_capabilities.recall(
            " ".join(texts) + " " + recent, CAPABILITIES_DIR,
            budget_tokens=config["capabilities_recall_budget_tokens"],
            max_items=config["capabilities_max_recall"],
        )
        if out:
            log(f"[{tag}] capabilities: {out.count(chr(10)) + 1} relevant")
        return out
    except Exception as e:  # noqa: BLE001
        log(f"[{tag}] capability recall failed (continuing without): {e}")
        return ""


def _apply_capabilities(learn: list, used: list, config: dict, tag: str) -> None:
    if not config.get("capabilities_enabled") or not (learn or used):
        return
    try:
        for r in ramu_capabilities.record_use(used, CAPABILITIES_DIR):
            log(f"[{tag}] capabilities: {r}")
        for r in ramu_capabilities.learn(learn, CAPABILITIES_DIR):
            log(f"[{tag}] capabilities: {r}")
    except Exception as e:  # noqa: BLE001
        log(f"[{tag}] capabilities write failed: {e}")


def _apply_blockers(blocked: list, config: dict, tag: str) -> None:
    if not config.get("blockers_enabled") or not blocked:
        return
    try:
        for r in ramu_blockers.add(blocked, BLOCKERS_PATH):
            log(f"[{tag}] blockers: {r}")
    except Exception as e:  # noqa: BLE001
        log(f"[{tag}] blockers write failed: {e}")


def _enqueue_synthetic(text: str) -> None:
    with _synthetic_lock:
        try:
            with open(SYNTHETIC_INBOX_PATH) as f:
                q = json.load(f)
            q = q if isinstance(q, list) else []
        except (OSError, json.JSONDecodeError):
            q = []
        q.append({"type": "message", "text": text})
        with open(SYNTHETIC_INBOX_PATH, "w") as f:
            json.dump(q, f, indent=2)


def _drain_synthetic_inbox(tag: str) -> list[dict]:
    """Automatic notes (a blocker resolved) that should go through the normal reply
    pipeline exactly like a Telegram message. Cleared only once read."""
    with _synthetic_lock:
        try:
            with open(SYNTHETIC_INBOX_PATH) as f:
                q = json.load(f)
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(q, list) or not q:
            return []
        try:
            os.remove(SYNTHETIC_INBOX_PATH)
        except OSError:
            pass
    log(f"[{tag}] {len(q)} automatic note(s) from the blocker watcher")
    return [m for m in q if isinstance(m, dict) and m.get("text")]


# Conversation-log role for the user's own turns.
USER_ROLE = "user"


def _turn_role(text: str) -> str:
    return "system" if text.startswith(ramu_blockers.AUTO_PREFIX) else USER_ROLE


def blockers_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    """Notice when something Ramu was waiting on is over and wake it. Zero cost
    while nothing is open: no blockers -> no composio call, no claude call."""
    tag = "blockers"
    interval = config["blockers_check_interval_seconds"]
    log(f"[{tag}] loop starting, interval={interval}s")
    while not stop_event.is_set():
        try:
            if config.get("blockers_enabled") and ramu_blockers.open_blockers(BLOCKERS_PATH):
                for b in ramu_blockers.check(BLOCKERS_PATH, expire_days=config["blockers_expire_days"]):
                    log(f"[{tag}] {b['toolkit']} is now live -> waking Ramu to resume: {b['goal'][:80]!r}")
                    _enqueue_synthetic(ramu_blockers.resume_message(b))
        except Exception as e:  # noqa: BLE001
            log(f"[{tag}] loop error (will retry): {e}")
        stop_event.wait(interval)
    log(f"[{tag}] loop stopped")


def _texts(items: list, *fields: str) -> str:
    """Flatten the human-readable fields of fetched items into a recall query."""
    parts = []
    for it in items or []:
        if isinstance(it, dict):
            parts.extend(str(it.get(f)) for f in fields if it.get(f))
        else:
            parts.append(str(it))
    return " ".join(parts)[:2000]


def _today_chat_block(now: datetime | None = None, max_turns: int = 60, max_chars: int = 240) -> str:
    """Today's actual conversation, for the background prompts. Without it they
    plan from stale state: suggesting something the user already said was done,
    reporting a stale location after the user said where they are, or raising
    an item again after saying it would be dropped."""
    now = now or datetime.now(_TZ)
    day = now.date()
    lines = []
    for t in _read_conversation_log():
        try:
            ts = datetime.fromisoformat(str(t.get("ts"))).astimezone(_TZ)
        except (TypeError, ValueError):
            continue
        text = str(t.get("text") or "")
        if ts.date() != day or t.get("chase") or t.get("health") or t.get("learning") or text.startswith("[auto]"):
            continue
        from_user = t.get("role") == USER_ROLE
        text = re.sub(r'^\[replying to: "([^"]{0,80})[^"]*"\]', r'[re: "\1…"]', text)
        lines.append((from_user, f"{ts:%H:%M} {_NAME_CAP if from_user else 'You'}: {' '.join(text.split())[:max_chars if from_user else max_chars // 2]}"))
    if not lines:
        return ""
    # Over budget: drop your own oldest lines first. The user's words are the facts.
    while len(lines) > max_turns:
        i = next((i for i, (from_user, _) in enumerate(lines) if not from_user), 0)
        lines.pop(i)
    lines = [l for _, l in lines]
    return (f"TODAY'S CONVERSATION so far (most recent last). What {USER_NAME} said here beats every plan, "
            f"task status, pattern and device feed: if {USER_NAME} says something is done, it's done; if "
            f"{USER_NAME} says where {PRONOUN_SUBJ} is, that's the place; if {USER_NAME} said something was handled, "
            f"or you said you'd drop it, do NOT raise it again today; never ask what {USER_NAME} already answered.\n"
            + "\n".join(lines) + "\n---\n")


def _prepend_memory(prompt: str, query: str, config: dict, tag: str) -> str:
    """Give a NON-conversational prompt (day-build, checks, queue, pattern) the
    same durable memory the Telegram reply gets, plus today's chat. Never raises;
    on any problem the prompt is returned unchanged."""
    try:
        prompt = _today_chat_block() + _orders_block() + prompt
    except Exception as e:  # noqa: BLE001
        log(f"[{tag}] today's chat unavailable (continuing without): {e}")
    if not config.get("memory_enabled"):
        return prompt
    try:
        ctx = ramu_memory.recall(query, MEMORY_STORE_DIR, budget_tokens=config["memory_recall_budget_tokens"])
    except Exception as e:  # noqa: BLE001
        log(f"[{tag}] memory recall failed (continuing without): {e}")
        return prompt
    if not ctx:
        return prompt
    log(f"[{tag}] memory: recalled {ctx.count(chr(10)) + 1} item(s)")
    return (
        f"What you already know about {USER_NAME} and {_THEIR} life (your own durable "
        "memory, most relevant first) -- let it sharpen this task and never "
        "contradict it:\n" + ctx + "\n---\n" + prompt
    )


def _log_conversation(turns: list[dict]) -> None:
    """Full append-only record, so the nightly reflection sees the whole day
    and not just the 16-turn window."""
    try:
        with open(CONVERSATION_LOG_PATH, "a") as f:
            for t in turns:
                f.write(json.dumps({"ts": _now_iso(), **t}) + "\n")
    except OSError as e:
        log(f"[telegram] WARNING: could not append conversation log: {e}")


def _read_conversation_log() -> list[dict]:
    out = []
    try:
        with open(CONVERSATION_LOG_PATH) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def _unsolicited_gate(config: dict, tag: str, kind: str = "nudge", urgency: str = "chat") -> tuple[bool, str]:
    """Code-level gate for anything the user did not just ask for. See ramu_pinggate."""
    return ramu_pinggate.evaluate(
        _read_conversation_log(),
        datetime.now(_TZ),
        quiet_start=config["quiet_hours_start_local"],
        quiet_end=config["quiet_hours_end_local"],
        kind=kind,
        urgency=urgency,
    )


def send_unsolicited(text: str, tag: str, config: dict, urgency: str = "chat") -> bool:
    """Send an unsolicited message and record it, so the next prompt can see
    what was already sent. Callers must have passed _unsolicited_gate first."""
    sent = send_telegram(text, tag)
    if sent:
        _append_conversation_turns(
            [{"role": "ramu_kaka", "text": text, "unsolicited": True, "urgency": urgency}],
            config["conversation_history_max_turns"],
        )
    return sent


# The ONLY number Ramu Kaka ever calls (profile: phone_number). Empty = no calls.
USER_PHONE_NUMBER = str(ramu_profile.PROFILE.get("phone_number") or "")
CALLS_STATE_PATH = os.path.join(PROJECT_DIR, "calls_state.json")


def call_user(tag: str, goal: str = "", allow_night: bool = False, config: dict | None = None) -> bool:
    """Ring the user through a voice-call service (an HTTP endpoint at
    config "call_service_url" that takes POST /call {client, to, goal} with an
    x-api-key header from CALL_SERVICE_API_KEY). Only ever the user's own
    number, capped per day, never in quiet hours unless they asked for it (a
    wake-up call). The call record comes back to /callrecord, so Ramu Kaka
    acts on what was said."""
    cfg = config or load_config()
    if not USER_PHONE_NUMBER:
        log(f"[{tag}] CALL wanted but no phone_number in the profile")
        return False
    key = os.environ.get("CALL_SERVICE_API_KEY", "")
    if not key:
        log(f"[{tag}] CALL wanted but CALL_SERVICE_API_KEY is not set")
        return False
    now = datetime.now(_TZ)
    if not allow_night and ramu_pinggate.in_quiet_hours(now, cfg["quiet_hours_start_local"], cfg["quiet_hours_end_local"]):
        log(f"[{tag}] CALL skipped: quiet hours")
        return False
    state = _read_json(CALLS_STATE_PATH, {})
    today = now.date().isoformat()
    made = [c for c in state.get("calls", []) if str(c.get("at", "")).startswith(today)]
    if len(made) >= cfg.get("max_calls_per_day", 4):
        log(f"[{tag}] CALL skipped: already {len(made)} call(s) today")
        return False
    import urllib.request
    body = json.dumps({"client": "ramu-kaka", "to": USER_PHONE_NUMBER,
                       "goal": (goal or f"Check in with {USER_NAME} about the message {PRONOUN_SUBJ} hasn't answered.")[:500]}).encode()
    req = urllib.request.Request(cfg.get("call_service_url", "http://127.0.0.1:8000") + "/call", data=body,
                                 headers={"x-api-key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            res = json.loads(r.read() or b"{}")
    except Exception as e:  # noqa: BLE001
        log(f"[{tag}] CALL failed: {e!r}")
        return False
    ok = int(res.get("status", 0)) in (200, 201, 202)
    state["calls"] = (state.get("calls") or [])[-50:] + [{"at": now.isoformat(timespec="seconds"),
                                                          "goal": goal[:200], "ok": ok}]
    _write_json(CALLS_STATE_PATH, state)
    log(f"[{tag}] CALL {'placed' if ok else 'REJECTED'}: {goal[:80]!r} -> {str(res)[:120]}")
    return ok


def chase_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    """When the user goes quiet on something it said, follow up the way a person
    would -- see ramu_pinggate.next_chase. No claude call; fixed lines."""
    tag = "chase"
    interval = config.get("chase_poll_interval_seconds", 60)
    log(f"[{tag}] loop starting, interval={interval}s")
    while not stop_event.is_set():
        try:
            step = ramu_pinggate.next_chase(
                _read_conversation_log(), datetime.now(_TZ),
                quiet_start=config["quiet_hours_start_local"],
                quiet_end=config["quiet_hours_end_local"],
            )
            if step:
                text = step["text"]
                anchor = next((t.get("text", "") for t in reversed(_read_conversation_log())
                               if t.get("unsolicited") and not t.get("chase")), "")
                goal = (f"You messaged {USER_NAME} on Telegram about something that matters and got no reply: "
                        f"\"{anchor[:300]}\". Say what it is in one line and ask what {PRONOUN_SUBJ} wants to do.")
                if step["action"] == "call" and not call_user(tag, goal, config=config):
                    text = "I'd be ringing your phone right now if I could. Please check Telegram."
                if send_telegram(text, tag):
                    _append_conversation_turns(
                        [{"role": "ramu_kaka", "text": text, "unsolicited": True,
                          "chase": step["step"], "urgency": step["urgency"]}],
                        config["conversation_history_max_turns"],
                    )
                    log(f"[{tag}] {step['urgency']} step {step['step']} ({step['action']}): {text!r}")
        except Exception as e:  # a bad cycle must never kill the loop
            log(f"[{tag}] ERROR in chase cycle: {e!r}")
        stop_event.wait(interval)
    log(f"[{tag}] loop stopped")


def _todays_turns(limit: int) -> list[dict]:
    today = _now_iso()[:10]
    out = []
    try:
        with open(CONVERSATION_LOG_PATH) as f:
            for line in f:
                try:
                    t = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(t.get("ts", "")).startswith(today):
                    out.append(t)
    except OSError:
        return []
    return out[-limit:]


def build_memory_reflect_prompt(turns: list[dict], existing: list[dict]) -> str:
    convo = "\n".join(f"{t.get('role')}: {str(t.get('text'))[:400]}" for t in turns)
    known = "\n".join(f"- {e['key']} [{e['type']}, imp {e['importance']}]: {e['text']}" for e in existing)[:4000]
    return (
        f"You are the memory curator for Ramu Kaka, {USER_NAME}'s personal life "
        "assistant. This is an internal pass, nobody reads your output but "
        f"code. Below is today's Telegram conversation between {USER_NAME} and "
        "Ramu Kaka, and what Ramu Kaka already remembers. Decide what is "
        "worth remembering long-term that the in-the-moment replies missed "
        f"or got wrong.\n\nKEEP: people in {_THEIR} life and who they are to {_THEM}; "
        "places; preferences and dislikes; decisions and the reason; "
        "routines; and LESSONS -- what actually worked or failed in how "
        f"Ramu Kaka helped {_THEM} (e.g. a reminder time that got ignored, a phrasing "
        "that landed), only when today's conversation is real evidence.\n"
        "SKIP: passing chatter, one-off logistics, anything ClickUp "
        "already tracks, habit logs, company/work details (personal "
        "life only), and NEVER credentials, card/payment details, OTPs, "
        "passwords.\nRULES: reuse an existing key to correct or extend a "
        "fact (that replaces it); put the key of anything now untrue or "
        "superseded in \"forget\"; at most 8 items; plain one-sentence "
        "text; importance 5 = never let slip, 3 = normal, 1 = trivia. If "
        "nothing qualifies, return empty arrays -- that is a fine outcome.\n\n"
        "Already remembered:\n" + (known or "(nothing yet)") + "\n\n"
        "Today's conversation:\n" + convo + "\n\n"
        "Output ONE JSON object and nothing else: "
        '{"remember": [{"key": "<stable-slug>", "type": "person|place|'
        'preference|fact|event|routine|lesson", "text": "<one sentence>", '
        '"importance": <1-5>}], "forget": ["<key>"]}'
    )


def memory_reflect_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    tag = "memory"
    interval = config["daybuild_check_interval_seconds"]
    trigger_hour = config["memory_reflect_trigger_hour_local"]
    log(f"[{tag}] reflect loop starting, interval={interval}s, trigger_hour={trigger_hour} local")
    while not stop_event.is_set():
        try:
            if config.get("memory_enabled"):
                today = _now_iso()[:10]
                state = {}
                try:
                    with open(MEMORY_REFLECT_STATE_PATH) as f:
                        state = json.load(f)
                except (OSError, json.JSONDecodeError):
                    pass
                if state.get("last_date") != today and datetime.now(_TZ).hour >= trigger_hour:
                    _reflect_once(config, lock, tag, today)
        except Exception as e:  # noqa: BLE001
            log(f"[{tag}] reflect loop error (will retry next cycle): {e}")
        stop_event.wait(interval)
    log(f"[{tag}] reflect loop stopped")


def _reflect_once(config: dict, lock: ClaudeLock, tag: str, today: str) -> None:
    def mark():
        try:
            with open(MEMORY_REFLECT_STATE_PATH, "w") as f:
                json.dump({"last_date": today}, f)
        except OSError as e:
            log(f"[{tag}] WARNING: could not write reflect state: {e}")

    turns = _todays_turns(config["memory_reflect_max_turns"])
    if sum(1 for t in turns if t.get("role") == USER_ROLE) < 2:
        log(f"[{tag}] reflect: fewer than 2 messages from the user today, nothing to learn from (0 claude calls)")
        mark()
        return
    if not lock.acquire(tag):
        return  # try again next cycle
    try:
        existing = sorted(ramu_memory.load_all(MEMORY_STORE_DIR),
                          key=lambda e: (e["importance"], e["updated"]), reverse=True)[:60]
        ok, out = run_claude(build_memory_reflect_prompt(turns, existing), config, tag)
        if not ok:
            log(f"[{tag}] reflect: claude call failed, will retry next cycle")
            return
        env = ramu_memory.extract_json_object(out)
        if env is None:
            log(f"[{tag}] reflect: no JSON in output, will retry next cycle")
            return
        _apply_memory_writes(env.get("remember") or [], env.get("forget") or [], config, tag, source="reflection")
        log(f"[{tag}] reflect: done ({len(turns)} turns reviewed)")
        mark()
    finally:
        lock.release()


def _maybe_consolidate_memory(config: dict, tag: str) -> None:
    """Once a day, no claude call: merge near-duplicates, prune stale trivia."""
    if not config.get("memory_enabled"):
        return
    today = _now_iso()[:10]
    try:
        with open(MEMORY_CONSOLIDATE_STAMP) as f:
            if f.read().strip() == today:
                return
    except OSError:
        pass
    try:
        if not os.path.isdir(MEMORY_STORE_DIR):
            return
        res = ramu_memory.consolidate(MEMORY_STORE_DIR)
        with open(MEMORY_CONSOLIDATE_STAMP, "w") as f:
            f.write(today)
        log(f"[{tag}] memory: consolidated {res}")
    except Exception as e:  # noqa: BLE001
        log(f"[{tag}] memory consolidate failed: {e}")


def _apply_envelope_effects(
    config: dict, tag: str, *, new_tasks: list, close_task_ids: list, habit_log: list,
    email_watch: dict | None, mem_remember: list, mem_forget: list,
    cap_learn: list, cap_used: list, cap_blocked: list, spawn_tasks: list | None = None,
    task_notes: list | None = None,
    promises: list | None = None, place_here: str | None = None,
    said_texts: list | None = None, goal_updates: list | None = None,
) -> None:
    """Everything a reply envelope asks for beyond the reply text itself.

    Extracted from telegram_loop 2026-09-21 so the RPC door (ramu_mcp.py ->
    rpc_loop) gets byte-identical behaviour rather than a second, drifting
    copy. A message asking Ramu Kaka to close a task must do the same thing
    whether it arrived from Telegram or ChatGPT.
    """
    for nt in new_tasks:
        name = nt.get("name")
        if not name:
            continue
        created = create_clickup_task(
            name, nt.get("priority", "normal"),
            nt.get("list_id", CLICKUP_LIFE_OS_LIST_ID), tag,
            description=nt.get("description", ""),
            due_in_days=nt.get("due_in_days"),
        )
        log(f"[{tag}] idea-capture: task {'created' if created else 'FAILED'}: {name!r}")
    for task_id in close_task_ids:
        closed = close_clickup_task(task_id, tag)
        log(f"[{tag}] close-task: {'closed' if closed else 'FAILED'}: {task_id!r}")
    for entry in habit_log:
        hq_key, note = entry.get("hq_key"), entry.get("note")
        if not hq_key or not note:
            continue
        logged = log_hq_comment(hq_key, note, config.get("hq_task_ids", {}), tag)
        log(f"[{tag}] habit-log: {'logged' if logged else 'FAILED'} [{hq_key}]: {note!r}")
    if email_watch and email_watch.get("query"):
        create_email_watch(
            email_watch["query"], email_watch.get("description", ""), tag,
        )
    _apply_memory_writes(mem_remember, mem_forget, config, tag)
    _apply_capabilities(cap_learn, cap_used, config, tag)
    _apply_blockers(cap_blocked, config, tag)
    for title, brief in ramu_tasks.parse_spawn(spawn_tasks):
        _spawn_task(title, brief, config, tag)
    for task_id, note in ramu_tasks.parse_notes(task_notes):
        with _tasks_io:
            t = ramu_tasks.add_note(TASKS_PATH, task_id, note, datetime.now(_TZ).isoformat())
            prev = None if t else ramu_tasks.get(TASKS_PATH, task_id)
        if t:
            log(f"[{tag}] note added to running task {task_id}: {note[:100]!r}")
        elif prev:  # it already finished: redo it with the correction
            _spawn_task(prev["title"], f"{prev['brief']}\n\nCORRECTION from {USER_NAME} (overrides the above): {note}\n"
                        f"Earlier result, now possibly wrong: {prev.get('summary', '')[:500]}", config, tag)
    if goal_updates:
        goals, changed = ramu_goals.apply_updates(_read_json(GOALS_PATH, {}).get("goals"), goal_updates, datetime.now(_TZ))
        if changed:
            _write_json(GOALS_PATH, {"goals": goals})
            log(f"[{tag}] goals updated: {changed}")
    if not place_here and said_texts:
        # Backstop: the model often skips place_here. If the user names where they are and
        # the phone has a fresh fix that isn't at a known place, pin it anyway.
        now = datetime.now(_TZ)
        presence = _read_json(PRESENCE_PATH, {})
        fix = ramu_ingest.fresh_fix(presence, now)
        if fix and not ramu_ingest.nearest_place(_read_json(PLACES_PATH, {}), fix["lat"], fix["lon"])[0]:
            place_here = next((n for t in said_texts if (n := ramu_ingest.place_from_text(t))), None)
    if place_here:
        with _presence_lock:
            places, ok = ramu_ingest.learn_place(_read_json(PLACES_PATH, {}), place_here, _read_json(PRESENCE_PATH, {}))
            if ok:
                _write_json(PLACES_PATH, places)
            # The user's word is the freshest presence signal there is, fix or no
            # fix -- it beats a stale or missing GPS reading.
            presence = _read_json(PRESENCE_PATH, {})
            presence.update({"place": place_here, "place_since": _now_iso(), "place_said": True})
            presence.pop("left", None)
            _write_json(PRESENCE_PATH, presence)
        log(f"[{tag}] place {'learned' if ok else 'NOT learned (no recent location fix)'}: {place_here!r}")
    if promises:
        for p in ramu_promises.add(PROMISES_PATH, ramu_promises.normalize(promises, datetime.now(_TZ))):
            log(f"[{tag}] promise scheduled for {p['at']}: {p['say']!r}")


# ---------------------------------------------------------------------------
# RPC door: a caller that wants the answer back (ramu_mcp.py / ChatGPT)
# ---------------------------------------------------------------------------


def enqueue_rpc(request_id: str, text: str, origin: str, max_queue: int = 20) -> bool:
    """Append a round-trip request. Called by ramu_mcp.py, not by the daemon."""
    with _rpc_lock:
        try:
            with open(RPC_INBOX_PATH) as f:
                q = json.load(f)
            q = q if isinstance(q, list) else []
        except (OSError, json.JSONDecodeError):
            q = []
        if len(q) >= max_queue:
            return False
        q.append({
            "request_id": request_id,
            "text": text,
            "origin": origin or "rpc",
            "ts": _now_iso(),
        })
        tmp = RPC_INBOX_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(q, f, indent=2)
        os.replace(tmp, RPC_INBOX_PATH)
    return True


def _drain_rpc_inbox(tag: str) -> list[dict]:
    with _rpc_lock:
        try:
            with open(RPC_INBOX_PATH) as f:
                q = json.load(f)
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(q, list) or not q:
            return []
        try:
            os.remove(RPC_INBOX_PATH)
        except OSError:
            pass
    items = [r for r in q if isinstance(r, dict) and r.get("request_id") and r.get("text")]
    if items:
        log(f"[{tag}] {len(items)} round-trip request(s)")
    return items


def write_rpc_response(request_id: str, payload: dict) -> None:
    os.makedirs(RPC_RESPONSES_DIR, exist_ok=True)
    path = os.path.join(RPC_RESPONSES_DIR, f"{request_id}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def _sweep_rpc_responses(ttl_seconds: int) -> None:
    """A caller that never collects its answer must not leak a file forever."""
    try:
        now = time.time()
        for name in os.listdir(RPC_RESPONSES_DIR):
            path = os.path.join(RPC_RESPONSES_DIR, name)
            try:
                if now - os.path.getmtime(path) > ttl_seconds:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def rpc_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    """Round-trip requests: same prompt, same memory, same conversation history
    as Telegram -- but the reply goes back to the caller instead of the user's phone.

    Deliberately NOT batched with Telegram messages: each request has someone
    waiting on its own answer, so merging them the way telegram_loop merges a
    burst of texts would hand the wrong reply to the wrong caller.
    """
    tag = "rpc"
    if not config.get("rpc_enabled", True):
        log(f"[{tag}] disabled by config, loop not starting")
        return
    interval = config["rpc_poll_interval_seconds"]
    ttl = config["rpc_response_ttl_seconds"]
    log(f"[{tag}] loop starting, interval={interval}s")
    last_sweep = 0.0
    while not stop_event.is_set():
        if time.time() - last_sweep > 300:
            _sweep_rpc_responses(ttl)
            last_sweep = time.time()
        for req in _drain_rpc_inbox(tag):
            if stop_event.is_set():
                # Don't strand the caller on shutdown -- it is polling for a
                # file that would otherwise never appear.
                write_rpc_response(req["request_id"], {
                    "ok": False,
                    "error": "Ramu Kaka is restarting. Try again in a moment.",
                })
                continue
            _serve_rpc(req, config, lock, tag)
        stop_event.wait(interval)
    log(f"[{tag}] loop stopped")


def _serve_rpc(req: dict, config: dict, lock: ClaudeLock, tag: str) -> None:
    request_id, text = req["request_id"], req["text"]
    origin = req.get("origin", "rpc")
    retry_budget = config["telegram_lock_retry_seconds"]
    retry_poll = config["telegram_lock_retry_poll_seconds"]

    got_lock, waited = False, 0
    while waited < retry_budget:
        if lock.acquire(tag):
            got_lock = True
            break
        time.sleep(retry_poll)
        waited += retry_poll
    if not got_lock:
        log(f"[{tag}] could not get lock after {waited}s for {request_id}")
        write_rpc_response(request_id, {
            "ok": False,
            "error": "Ramu Kaka is busy with something else. Try again shortly.",
        })
        return

    try:
        open_tasks = fetch_life_os_open_tasks(tag) or []
        history = _read_conversation_history()
        texts = [text]
        memory_context = _recall_memory(texts, history, config, tag)
        capabilities_context = _composio_apps_block() + _recall_capabilities(texts, history, config, tag)
        prompt = build_telegram_reply_prompt(
            texts, None, open_tasks or None, history or None,
            memory_context, capabilities_context,
        )
        prompt = (
            f"{prompt}\n\n[CHANNEL] This message reached you through {origin}, not "
            f"Telegram. {_NAME_CAP} is reading your reply there, so don't say to "
            "check the phone. Everything else about how you answer is unchanged."
        )
        ok, out = run_claude(prompt, config, tag)
        if not ok or not out:
            log(f"[{tag}] claude call failed/empty for {request_id}")
            write_rpc_response(request_id, {"ok": False, "error": _FALLBACK_REPLY})
            return

        envelope = ramu_memory.extract_json_object(out, "reply")
        if envelope is None:
            reply, envelope = out, {}
        else:
            reply = envelope.get("reply") or ""
        if not reply:
            log(f"[{tag}] no reply text for {request_id}, nothing returned")
            write_rpc_response(request_id, {"ok": False, "error": _FALLBACK_REPLY})
            return

        write_rpc_response(request_id, {"ok": True, "reply": reply})
        log(f"[{tag}] answered {request_id} ({len(reply)} chars) via {origin}")

        # Shared history: what the user says to ChatGPT and on Telegram
        # are one conversation, so the next Telegram message knows about this.
        _append_conversation_turns(
            [{"role": USER_ROLE, "text": text}, {"role": "ramu_kaka", "text": reply}],
            config["conversation_history_max_turns"],
        )
        _apply_envelope_effects(
            config, tag,
            new_tasks=envelope.get("new_tasks") or [],
            close_task_ids=envelope.get("close_task_ids") or [],
            habit_log=envelope.get("habit_log") or [],
            email_watch=envelope.get("email_watch"),
            mem_remember=envelope.get("remember") or [],
            mem_forget=envelope.get("forget") or [],
            cap_learn=envelope.get("learn") or [],
            cap_used=envelope.get("capability_used") or [],
            cap_blocked=envelope.get("blocked") or [],
            promises=envelope.get("promises") or [],
            place_here=envelope.get("place_here"),
            said_texts=[text],
            goal_updates=envelope.get("goals") or [],
        )
    except Exception as e:  # never strand a waiting caller on an unexpected error
        log(f"[{tag}] ERROR serving {request_id}: {e!r}")
        write_rpc_response(request_id, {"ok": False, "error": _FALLBACK_REPLY})
    finally:
        lock.release()


def _media_fetcher(tag: str):
    """fetch(file_id) -> local path for anything the user sends. Direct Bot API first,
    the old curl downloader as fallback. Raises with a plain reason on failure."""
    token = _tg_token()

    def fetch(file_id: str) -> str:
        if token:
            try:
                return ramu_telegram.download(token, file_id, ramu_media.MEDIA_DIR)
            except ramu_telegram.TelegramError as e:
                raise RuntimeError(str(e)) from None
        path = download_telegram_file(file_id, tag)
        if path:
            return path
        raise RuntimeError("no way to download files right now")
    return fetch


def _start_typing(tag: str, max_seconds: int = 180) -> threading.Event:
    """Shows "typing..." in the user's chat until the returned event is set (Telegram
    clears it after ~5s, so it is re-sent every 4s). Capped, so a crash before
    the caller stops it cannot leave it typing forever."""
    stop = threading.Event()
    token = _tg_token()

    def run():
        end = time.time() + max_seconds
        pulses = 0
        if not token:
            log(f"[{tag}] typing: no token, not showing typing")
        while token and not stop.is_set() and time.time() < end:
            try:
                ramu_telegram.send_chat_action(token, ramu_telegram.ALLOWED_CHAT_ID)
                pulses += 1
            except ramu_telegram.TelegramError as e:
                log(f"[{tag}] typing: stopped after {pulses} pulse(s), error: {e}")
                return
            stop.wait(4)
        log(f"[{tag}] typing: {pulses} pulse(s) sent")

    threading.Thread(target=run, daemon=True).start()
    return stop


# ---------------------------------------------------------------------------
# Background workers (ramu_tasks.py). The fast lane replies in
# seconds and never does tool work; anything real becomes a task here. The
# registry sits beside the session file, outside the synced project dir.
# ---------------------------------------------------------------------------
TASKS_PATH = os.path.join(MEMORY_DIR, ".ramu_tasks.json")
_tasks_io = threading.Lock()
_worker_sem: threading.BoundedSemaphore | None = None


def _now_line() -> str:
    return datetime.now(_TZ).strftime("Now: %A %d %B %Y, %H:%M %Z. ")


def _tasks_update(task_id: str, **fields):
    with _tasks_io:
        return ramu_tasks.update(TASKS_PATH, task_id, **fields)


def _spawn_task(title: str, brief: str, config: dict, tag: str, **extra) -> dict:
    with _tasks_io:
        t = ramu_tasks.add(TASKS_PATH, {**ramu_tasks.new_task(title, brief, datetime.now(_TZ).isoformat()), **extra})
    log(f"[{tag}] spawned background task {t['id']}: {t['title']!r}")
    threading.Thread(target=_run_worker, args=(t["id"], None, config), name=f"task-{t['id']}", daemon=True).start()
    return t


def _run_worker(task_id: str, resume_text: str | None, config: dict) -> None:
    global _worker_sem
    tag = f"task-{task_id}"
    if _worker_sem is None:
        _worker_sem = threading.BoundedSemaphore(config.get("worker_max_concurrent", 3))
    try:
        with _worker_sem:
            with _tasks_io:
                t = ramu_tasks.get(TASKS_PATH, task_id)
            if not t:
                return
            _tasks_update(task_id, status="running", started=t.get("started") or datetime.now(_TZ).isoformat())
            wcfg = {**config, "claude_timeout_seconds": config.get("worker_timeout_seconds", 900)}
            if resume_text:
                ok, out, sid = run_claude_session(resume_text, wcfg, tag, resume_id=t.get("session_id"))
            else:
                ok, out, sid = run_claude_session(ramu_tasks.worker_prompt(t, _now_line() + "\n" + _composio_apps_block()), wcfg, tag)
            if ok:
                result = ramu_tasks.parse_result(out)
                for _ in range(2):  # what the user added while it worked
                    with _tasks_io:
                        notes = ramu_tasks.take_notes(TASKS_PATH, task_id)
                    if not notes or not sid:
                        break
                    log(f"[{tag}] applying {len(notes)} note(s) from the user")
                    ok2, out2, sid2 = run_claude_session(ramu_tasks.note_prompt(notes), wcfg, tag, resume_id=sid)
                    if ok2:
                        result, sid = ramu_tasks.parse_result(out2), sid2 or sid
            else:
                result = {"status": "failed", "approval": None,
                          "summary": f"The run for '{t['title']}' crashed or timed out before it could report."}
            status = result["status"]
            _tasks_update(
                task_id, status=status, summary=str(result.get("summary") or "")[:1500],
                session_id=sid or t.get("session_id"), approval=result.get("approval"),
                finished=None if status == "needs_approval" else datetime.now(_TZ).isoformat(),
            )
            log(f"[{tag}] finished: {status}")
            _apply_envelope_effects(
                config, tag, new_tasks=result.get("new_tasks") or [], close_task_ids=[],
                habit_log=result.get("habit_log") or [], email_watch=None,
                mem_remember=result.get("remember") or [], mem_forget=[],
                cap_learn=[], cap_used=[], cap_blocked=[],
            )
    except Exception as e:  # a worker must never die silently
        log(f"[{tag}] ERROR in worker: {e!r}")
        _tasks_update(task_id, status="failed", summary="That task hit an internal error.", finished=datetime.now(_TZ).isoformat())
    # Delivery is separate: a problem telling the user must never rewrite what the task did.
    try:
        _deliver_task(task_id, config)
    except Exception as e:
        log(f"[{tag}] ERROR delivering the result: {e!r}")


def _deliver_task(task_id: str, config: dict) -> None:
    """Tell the user what came of it. They asked for this, so it is not gated; at
    night it goes out silently."""
    tag = f"task-{task_id}"
    with _tasks_io:
        t = ramu_tasks.get(TASKS_PATH, task_id)
    if not t or t.get("delivered_status") == t["status"]:
        return
    if t.get("unprompted") and not (t.get("summary") or "").strip() and t["status"] != "needs_approval":
        _tasks_update(task_id, delivered=True, delivered_status=t["status"])
        log(f"[{tag}] nothing to tell the user, stayed quiet")
        return
    buttons = None
    text = t.get("summary") or "Done."
    if t["status"] == "needs_approval" and (t.get("approval") or {}).get("action"):
        text = f"{text}\nNeed your OK: {t['approval']['action']}"
        buttons = ramu_tasks.approval_buttons(task_id)
    if send_telegram(text, tag, buttons=buttons, silent=_quiet_hours_active(config)):
        _append_conversation_turns([{"role": "ramu_kaka", "text": text, "task": task_id}],
                                   config["conversation_history_max_turns"])
        _tasks_update(task_id, delivered=True, delivered_status=t["status"])
        log(f"[{tag}] delivered ({t['status']})")


def _handle_approval_taps(taps: list, config: dict, tag: str) -> None:
    """Approve / Decline on a worker's request: log it, then resume that worker."""
    for m in taps:
        parsed = ramu_tasks.parse_approval(m.get("text"))
        if not parsed:
            continue
        task_id, yes = parsed
        with _tasks_io:
            t = ramu_tasks.get(TASKS_PATH, task_id)
        action = ((t or {}).get("approval") or {}).get("action") or "that step"
        if not t or t["status"] != "needs_approval":
            log(f"[{tag}] approval tap for {task_id} ignored (not waiting)")
            continue
        _append_conversation_turns(
            [{"role": USER_ROLE, "text": f"[tap] {'Approved' if yes else 'Declined'}: {action}"}],
            config["conversation_history_max_turns"])
        _tasks_update(task_id, status="running")
        log(f"[{tag}] {'approved' if yes else 'declined'} {task_id}, resuming the worker")
        threading.Thread(target=_run_worker, args=(task_id, ramu_tasks.resume_prompt(yes, action), config),
                         name=f"task-{task_id}", daemon=True).start()


def _recover_tasks() -> None:
    """A restart kills running workers. Mark them failed so nothing looks alive
    that is not; tasks waiting on an approval tap survive (their session resumes)."""
    with _tasks_io:
        tasks = ramu_tasks.load(TASKS_PATH)
        changed = False
        for t in tasks:
            if t["status"] in ("queued", "running"):
                t.update(status="failed", finished=datetime.now(_TZ).isoformat(), delivered=True,
                         summary=t.get("summary") or "Interrupted by a restart before it finished.")
                changed = True
        if changed:
            ramu_tasks.save(TASKS_PATH, tasks)
            log("[tasks] marked unfinished tasks as interrupted by the restart")


TELEGRAM_SESSION_PATH = os.path.join(MEMORY_DIR, ".telegram_session")


def _persona_fingerprint() -> str:
    return hashlib.sha1((_FAST_PREFIX + _VOICE_GUARDRAIL + _CAPTURE_RULE + ramu_tasks.FAST_LANE_RULE).encode()).hexdigest()[:10]


def _load_session(config: dict) -> dict | None:
    """The live Telegram Claude session, or None when it should start fresh:
    missing, too many turns, too old, or the persona prompt changed since."""
    try:
        with open(TELEGRAM_SESSION_PATH) as f:
            s = json.load(f)
        age = datetime.now(_TZ) - datetime.fromisoformat(s["started"])
        if (not s.get("id") or s.get("persona") != _persona_fingerprint()
                or s.get("turns", 0) >= config.get("telegram_session_max_turns", 20)
                or age > timedelta(hours=config.get("telegram_session_max_age_hours", 12))):
            return None
        return s
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_session(sid: str, turns: int, started: str) -> None:
    try:
        with open(TELEGRAM_SESSION_PATH, "w") as f:
            json.dump({"id": sid, "turns": turns, "started": started, "persona": _persona_fingerprint()}, f)
    except OSError as e:
        log(f"[telegram] WARNING: could not save session id: {e}")


_warming = False


def _ensure_warm_session(config: dict, tag: str) -> None:
    """Keep a live fast-lane session ready. The first turn of a session is slow (the
    full prompt, ~25s), so build the next one in the background whenever none is
    live (boot, expiry, a changed prompt) and the user's message lands on a warm one."""
    global _warming
    if _warming or _load_session(config):
        return
    _warming = True

    def run():
        global _warming
        try:
            prompt = build_telegram_reply_prompt(
                ["(session warm-up: nothing to answer, reply with an empty reply)"], None, None, None,
                "", "", tasks_block=ramu_tasks.awareness_block(ramu_tasks.load(TASKS_PATH), datetime.now(_TZ)),
                fast_lane=True)
            ok, out, sid = run_claude_session(prompt, config, tag, tools="Read,Skill")
            if ok and out and sid:
                _save_session(sid, 0, datetime.now(_TZ).isoformat())
                log(f"[{tag}] fast-lane session warmed up")
            else:
                log(f"[{tag}] fast-lane warm-up failed; the first message will start a session itself")
        except Exception as e:
            log(f"[{tag}] ERROR warming the fast-lane session: {e!r}")
        finally:
            _warming = False

    threading.Thread(target=run, name="warmup", daemon=True).start()


def _run_telegram_turn(texts, image_paths, open_tasks, history, memory_context, capabilities_context, config, tag):
    tasks_block = ramu_tasks.awareness_block(ramu_tasks.load(TASKS_PATH), datetime.now(_TZ))
    """One reply turn. Resumes the live session with a compact prompt when there
    is one (fast, cached); otherwise starts a fresh session with the full prompt.
    A failed resume falls back to a fresh session, never to silence."""
    sess = _load_session(config)
    if sess:
        prompt = build_telegram_reply_prompt(
            texts, image_paths or None, open_tasks or None, (history or [])[-6:] or None,
            memory_context, capabilities_context, compact=True, tasks_block=tasks_block, fast_lane=True)
        ok, out, sid = run_claude_session(prompt, config, tag, resume_id=sess["id"], tools="Read,Skill")
        if ok and out:
            _save_session(sid or sess["id"], sess.get("turns", 0) + 1, sess["started"])
            log(f"[{tag}] resumed session, turn {sess.get('turns', 0) + 1}")
            return True, out
        log(f"[{tag}] resume failed, starting a fresh session")
    prompt = build_telegram_reply_prompt(
        texts, image_paths or None, open_tasks or None, history or None, memory_context, capabilities_context,
        tasks_block=tasks_block, fast_lane=True)
    ok, out, sid = run_claude_session(prompt, config, tag, tools="Read,Skill")
    if ok and out and sid:
        _save_session(sid, 1, datetime.now(_TZ).isoformat())
        log(f"[{tag}] started a new session")
    return ok, out


def _normalize_buttons(raw) -> list | None:
    """Model-supplied tap options -> inline-keyboard rows, or None. Labels double
    as callback data, so a tap reads back to the model as a complete answer."""
    if not isinstance(raw, list):
        return None
    labels = [str(x).strip() for x in raw if isinstance(x, (str, int)) and str(x).strip()]
    labels = [l[:30] for l in labels][:4]
    if len(labels) < 2:
        return None
    return [[[l, l] for l in labels]]


def _orders_today(now: datetime) -> list[dict]:
    """Food and grocery orders today, from inbox signals and bank alerts."""
    try:
        sigs = [x for x in _read_json(FOOTPRINT_PATH, {}).get("signals") or [] if x.get("type") == "food"]
        sms = []
        with open(DEVICE_LOG_PATH) as f:
            for line in f.readlines()[-300:]:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("kind") == "sms":
                    sms.append(ev)
        return ramu_ingest.orders_today(sigs, sms, now)
    except (OSError, ValueError, AttributeError):
        return []


def _orders_block() -> str:
    orders = _orders_today(datetime.now(_TZ))
    if not orders:
        return ""
    return (f"Ordered today (from {_THEIR} inbox and card alerts; a meal order means {USER_NAME} ate, don't ask): "
            + "; ".join(f"{o['at']:%H:%M} {o['what']} ({o['kind']})" for o in orders) + ".\n---\n")


def _next_checkin_question(config: dict) -> str | None:
    now = datetime.now(_TZ)
    turns = _read_conversation_log()
    if ramu_checkins.recent_questions(turns, now) >= config.get("checkin_chain_max", 3):
        return None
    meals = [o["at"] for o in _orders_today(now) if o["kind"] == "meal"]
    todo = ramu_checkins.due(now, ramu_checkins.answered_today(turns, now, meals))
    return todo[0] if todo else None


def _handle_checkin_taps(taps: list, config: dict, tag: str) -> None:
    """Taps on a check-in question are handled in code, no model call: log the
    answer, then ask the next due question, or close out with one word."""
    max_turns = config["conversation_history_max_turns"]
    waved_off = False
    for m in taps:
        parsed = ramu_checkins.parse(m.get("text"))
        if not parsed:
            continue
        key, val = parsed
        line, note = ramu_checkins.describe(key, val)
        _append_conversation_turns([{"role": USER_ROLE, "text": f"[tap] {line}", "checkin": key}], max_turns)
        if note:
            hq = ramu_checkins.CHECKINS[key]["hq"]
            logged = log_hq_comment(hq, note, config.get("hq_task_ids", {}), tag)
            log(f"[{tag}] check-in tap {key}={val}: {'logged' if logged else 'FAILED to log'} [{hq}]")
        else:
            log(f"[{tag}] check-in tap {key}=skip")
        waved_off = val == ramu_checkins.SKIP[1]
    if waved_off:
        return  # the user waved it off: say nothing
    key = _next_checkin_question(config)
    if key:
        text, buttons = ramu_checkins.build(key)
        if send_telegram(text, tag, buttons=buttons):
            _append_conversation_turns([{"role": "ramu_kaka", "text": text, "checkin_q": key}], max_turns)
            log(f"[{tag}] check-in chain: asked {key}")
    else:
        send_telegram("Noted.", tag)


def _read_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


_presence_lock = threading.Lock()
DEVICE_NOTE_PREFIX = ramu_blockers.AUTO_PREFIX + " Signal"


def _enqueue_device_note(note: str) -> None:
    """One pending device note per kind of thing: a newer 'arrived at' replaces
    an older unread one, so a backlog behind the ping gate never becomes a wall."""
    text = (f"{DEVICE_NOTE_PREFIX} (not from {USER_NAME}, {datetime.now(_TZ).strftime('%H:%M')}): {note} "
            f"Decide whether this deserves a message to {_THEM} right now, using your skills "
            "(e.g. a travel skill when away from home, a nutrition skill for a meal, a money "
            "skill for a bank alert). Most signals need nothing: if so, set reply to "
            "an empty string and nothing is sent. Never mention the tracking itself.")
    key = note.split(" at ")[0][:40]
    with _synthetic_lock:
        q = _read_json(SYNTHETIC_INBOX_PATH, [])
        q = [m for m in (q if isinstance(q, list) else [])
             if not (str(m.get("text", "")).startswith(DEVICE_NOTE_PREFIX) and key in m.get("text", ""))]
        q.append({"type": "message", "text": text})
        _write_json(SYNTHETIC_INBOX_PATH, q)


def _ingest(events: list[dict], tag: str) -> None:
    now = datetime.now(_TZ)
    with _presence_lock:
        presence = _read_json(PRESENCE_PATH, {})
        places = _read_json(PLACES_PATH, {})
        with open(DEVICE_LOG_PATH, "a") as f:
            for ev in events:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                presence, note = ramu_ingest.apply(presence, ev, now, places)
                if note:
                    log(f"[{tag}] {ev['kind']}: {note}")
                    _enqueue_device_note(note)
        _write_json(PRESENCE_PATH, presence)


def ingest_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    """The private inbox for the user's devices. Bound only to the addresses in
    config "ingest_bind" (localhost and/or a Tailscale address, never 0.0.0.0),
    token required on every request."""
    import http.server
    tag = "ingest"
    # Entries are "host" (uses ingest_port) or "host:port".
    raw = config.get("ingest_bind") or []
    raw = [raw] if isinstance(raw, str) else list(raw)
    default_port = int(config.get("ingest_port", 8787))
    hosts = [(h.rsplit(":", 1)[0], int(h.rsplit(":", 1)[1])) if ":" in h else (h, default_port) for h in raw]
    token = os.environ.get("INGEST_TOKEN", "")
    if not hosts or not token:
        log(f"[{tag}] not starting: ingest_bind or INGEST_TOKEN missing")
        return

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep device traffic out of daemon.log
            pass

        def _reply(self, code, body=b"[]"):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            src = self.client_address[0]
            from urllib.parse import urlsplit, parse_qs
            qk = (parse_qs(urlsplit(self.path).query).get("k") or [""])[0]
            # ?k=<token> lets the user paste one URL into an app instead of typing a header.
            if not (ramu_ingest.authorized(self.headers, token) or ramu_ingest.authorized({"Authorization": f"Bearer {qk}"}, token)):
                auth = (self.headers.get("Authorization") or "")[:7] or "none"
                log(f"[{tag}] 401 from {src} on {self.path.split('?')[0]} (auth scheme: {auth!r})")
                return self._reply(401, b'{"error":"unauthorized"}')
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 64_000)
                raw = self.rfile.read(n) or b"{}"
                try:
                    payload = json.loads(raw)
                except ValueError:
                    # GPSLogger's default is a form body: kind=location&lat=%LAT&...
                    from urllib.parse import parse_qsl
                    payload = dict(parse_qsl(raw.decode("utf-8", "replace")))
                    if not payload:
                        raise
            except (ValueError, OSError):
                log(f"[{tag}] 400 bad json from {src} on {self.path.split('?')[0]} "
                    f"(content-type {self.headers.get('Content-Type')!r})")
                return self._reply(400, b'{"error":"bad json"}')
            now = datetime.now(_TZ)
            path = self.path.split("?")[0]
            if path == "/owntracks":
                events = ramu_ingest.parse_owntracks(payload, now)
            elif path == "/event":
                events = ramu_ingest.parse_generic(payload, now)
            elif path == "/sms":
                events = ramu_ingest.parse_sms_forwarder(payload, now)
            elif path == "/health":
                events = ramu_ingest.parse_health(payload, now)
            elif path == "/notify":
                events = ramu_ingest.parse_notification(payload, now)
            elif path == "/callrecord":
                events = ramu_ingest.parse_call_record(payload, now)
            else:
                return self._reply(404, b'{"error":"not found"}')
            if not events:
                log(f"[{tag}] {path} from {src}: nothing usable in payload (keys {sorted(payload)[:8] if isinstance(payload, dict) else type(payload).__name__})")
            if events:
                try:
                    _ingest(events, tag)
                except Exception as e:  # noqa: BLE001
                    log(f"[{tag}] ERROR ingesting: {e!r}")
            self._reply(200)  # OwnTracks expects a JSON array back

    servers = {}
    while not stop_event.is_set() and len(servers) < len(hosts):
        for host, port in hosts:
            if (host, port) in servers:
                continue
            try:
                srv = http.server.ThreadingHTTPServer((host, port), Handler)
            except OSError as e:  # Tailscale not up yet after a reboot
                log(f"[{tag}] can't bind {host}:{port} yet ({e}); retrying in 60s")
                continue
            servers[(host, port)] = srv
            threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 1},
                             name=f"ingest-{host}", daemon=True).start()
            log(f"[{tag}] listening on {host}:{port}")
        if len(servers) < len(hosts):
            stop_event.wait(60)
    stop_event.wait()
    for srv in servers.values():
        srv.shutdown()
        srv.server_close()
    log(f"[{tag}] loop stopped")


BANK_MAIL_STATE_PATH = os.path.join(PROJECT_DIR, "bank_mail_state.json")
# Senders of bank/card transaction alert emails (profile: bank_alert_senders).
BANK_ALERT_SENDERS = [str(x) for x in (ramu_profile.PROFILE.get("bank_alert_senders") or []) if x]
BANK_ALERT_QUERY = ("from:(" + " OR ".join(BANK_ALERT_SENDERS) + ") newer_than:2d") if BANK_ALERT_SENDERS else ""


def bank_mail_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    """The user's banks already email every transaction; read them as they land
    and feed them in like any other device signal. No app on the phone, OTPs
    never read. Does nothing unless the profile lists bank_alert_senders."""
    tag = "bankmail"
    interval = config.get("bank_mail_interval_seconds", 600)
    if not BANK_ALERT_QUERY or not GMAIL_PERSONAL_ACCOUNT:
        log(f"[{tag}] not starting: no bank_alert_senders / gmail_personal_account in the profile")
        return
    log(f"[{tag}] loop starting, interval={interval}s")
    while not stop_event.is_set():
        try:
            state = _read_json(BANK_MAIL_STATE_PATH, {})
            seen = set(state.get("seen") or [])
            ok, parsed = composio_execute("GMAIL_FETCH_EMAILS", {"query": BANK_ALERT_QUERY, "max_results": 25},
                                          tag, account=GMAIL_PERSONAL_ACCOUNT)
            if ok and parsed:
                _beat("bankmail")
                msgs = (parsed.get("data") or {}).get("messages") or []
                fresh = [m for m in msgs if (m.get("messageId") or m.get("id")) not in seen]
                first_run = not state.get("seen")
                now = datetime.now(_TZ)
                events = []
                for m in sorted(fresh, key=lambda m: m.get("messageTimestamp") or ""):
                    events += ramu_ingest.parse_bank_email(m.get("sender"), m.get("subject"),
                                                          m.get("messageText"), m.get("messageTimestamp"), now)
                if events and not first_run:  # first run: backfill silently, don't replay old spends as news
                    _ingest(events, tag)
                    for ev in events:  # a food/grocery charge is a meal log the user never has to type
                        kind = ramu_ingest.order_kind(f"{ev.get('merchant') or ''} {ev.get('text') or ''}")
                        if kind and ev.get("direction") == "debit":
                            when = (ev.get("txn_at") or ev["ts"])[11:16]
                            note = (f"{'Meal' if kind == 'meal' else 'Groceries'} ordered {when}: "
                                    f"{ev.get('merchant') or kind}, {ev['amount']:,.0f} (card alert).")
                            log_hq_comment("nutrition", note, config.get("hq_task_ids", {}), tag)
                elif events:
                    with open(DEVICE_LOG_PATH, "a") as f:
                        for ev in events:
                            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                ids = [m.get("messageId") or m.get("id") for m in msgs] + list(seen)
                _write_json(BANK_MAIL_STATE_PATH, {"seen": ids[:500]})
                if fresh:
                    log(f"[{tag}] {len(fresh)} new alert mail(s), {len(events)} money event(s){' (backfill)' if first_run else ''}")
        except Exception as e:  # noqa: BLE001
            log(f"[{tag}] ERROR: {e!r}")
        stop_event.wait(interval)
    log(f"[{tag}] loop stopped")


def footprint_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    """Bookings, orders, food orders, renewals and bills from the user's inbox, classified
    by one Sonnet call per batch of NEW mails. Zero claude calls when nothing's new."""
    tag = "footprint"
    interval = config.get("footprint_interval_seconds", 1800)
    if not GMAIL_PERSONAL_ACCOUNT:
        log(f"[{tag}] not starting: no gmail_personal_account in the profile")
        return
    log(f"[{tag}] loop starting, interval={interval}s")
    while not stop_event.is_set():
        wait = interval
        try:
            store = _read_json(FOOTPRINT_PATH, {})
            seen = set(store.get("seen") or [])
            ok, parsed = composio_execute("GMAIL_FETCH_EMAILS", {"query": ramu_footprint.QUERY, "max_results": 30},
                                          tag, account=GMAIL_PERSONAL_ACCOUNT)
            msgs = ((parsed or {}).get("data") or {}).get("messages") or [] if ok else []
            if ok:
                _beat("footprint")
            mails = ramu_footprint.compact(msgs, seen, ramu_ingest.email_text)
            if mails and not lock.acquire(tag):
                wait = 60  # someone else is using claude; try again soon, not in 30 min
            elif mails:
                try:
                    now = datetime.now(_TZ)
                    ok2, out = run_claude(ramu_footprint.prompt(mails, now), config, tag)
                finally:
                    lock.release()
                if ok2:
                    sigs = ramu_footprint.parse(out, mails)
                    first_run = not store.get("seen")
                    store = {"signals": ramu_footprint.merge(store.get("signals"), sigs, now),
                             "seen": ([m["id"] for m in mails] + list(seen))[:800]}
                    _write_json(FOOTPRINT_PATH, store)
                    log(f"[{tag}] {len(mails)} new mail(s) -> {len(sigs)} signal(s){' (backfill)' if first_run else ''}")
                    if not first_run:
                        for sg in sigs:
                            if sg["importance"] == "high" or sg["type"] == "food":
                                _enqueue_device_note(ramu_footprint.note(sg))
        except Exception as e:  # noqa: BLE001
            log(f"[{tag}] ERROR: {e!r}")
        stop_event.wait(wait)
    log(f"[{tag}] loop stopped")


def _goals_block() -> str:
    goals = _read_json(GOALS_PATH, {}).get("goals")
    if not goals:
        return ""
    import sqlite3
    try:
        db = sqlite3.connect(f"file:{os.path.join(PROJECT_DIR, 'life.db')}?mode=ro", uri=True)
    except sqlite3.Error:
        return ""
    try:
        ctx = {"db": db, "people": _read_json(PEOPLE_PATH, {}).get("people"),
               "presence": _read_json(PRESENCE_PATH, {})}
        return ramu_goals.block(goals, ctx, datetime.now(_TZ))
    finally:
        db.close()


def _feed_last_seen() -> dict:
    p = _read_json(PRESENCE_PATH, {})
    seen = dict(_heartbeats)
    seen["location"] = (p.get("last_location") or {}).get("ts")
    seen["whatsapp"] = p.get("last_whatsapp")
    seen["health"] = (p.get("health") or {}).get("ts")
    return {k: v for k, v in seen.items() if v}


def health_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    """Watches every feed and loop. A silent one gets ONE plain line to the user with
    the fix, a reminder at most every 12h, and a line when it's back. Held
    during quiet hours. No claude calls."""
    tag = "health"
    log(f"[{tag}] loop starting")
    stop_event.wait(120)  # let the other loops take their first beat
    while not stop_event.is_set():
        try:
            now = datetime.now(_TZ)
            seen = _feed_last_seen()
            problems = ramu_health.check(seen, now, _started_at or now)
            state = _read_json(HEALTH_STATE_PATH, {})
            state["current"] = problems
            quiet = ramu_pinggate.in_quiet_hours(now, config["quiet_hours_start_local"], config["quiet_hours_end_local"])
            if not quiet:
                msgs, told = ramu_health.transitions(problems, state.get("told"), now, last_seen=seen)
                for m in msgs:
                    if send_telegram(m, tag):
                        _append_conversation_turns([{"role": "ramu_kaka", "text": m, "health": True}],
                                                   config["conversation_history_max_turns"])
                        log(f"[{tag}] told the user: {m}")
                state["told"] = told
            _write_json(HEALTH_STATE_PATH, state)
        except Exception as e:  # noqa: BLE001
            log(f"[{tag}] ERROR: {e!r}")
        stop_event.wait(300)
    log(f"[{tag}] loop stopped")


def _fetch(url: str, timeout: int = 15) -> bytes | None:
    import ssl
    import urllib.request
    # A python.org Python on macOS can ship without CA certs; use certifi or macOS's bundle.
    try:
        import certifi
        cafile = certifi.where()
    except ImportError:
        cafile = "/etc/ssl/cert.pem"
    try:
        ctx = ssl.create_default_context(cafile=cafile)
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) RamuKaka/1.0 (personal digest)"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.read(2_000_000)
    except Exception:  # noqa: BLE001 - one dead feed must not stop the digest
        return None


def learning_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    """Once a day, once the user is up and active: one thing worth learning, with why
    and a tiny try-this. One Sonnet call a day."""
    tag = "learning"
    start_h, end_h = config.get("learning_window_local", [11, 21])
    log(f"[{tag}] loop starting, window {start_h}:00-{end_h}:00")
    while not stop_event.is_set():
        wait = 900
        try:
            now = datetime.now(_TZ)
            today = now.date().isoformat()
            state = _read_json(LEARNING_STATE_PATH, {})
            active = (str(_read_json(PRESENCE_PATH, {}).get("woke_at", "")).startswith(today)
                      or any(t.get("role") == USER_ROLE and str(t.get("ts", "")).startswith(today)
                             for t in _read_conversation_log()))
            if start_h <= now.hour < end_h and state.get("last_day") != today and active:
                cands = []
                hn = _fetch(ramu_learning.HN_URL)
                if hn:
                    try:
                        cands += ramu_learning.parse_hn(json.loads(hn))
                    except ValueError:
                        pass
                for name, url in ramu_learning.FEEDS.items():
                    raw = _fetch(url)
                    if raw:
                        cands += ramu_learning.parse_feed(name, raw)
                cands = ramu_learning.fresh(cands, state.get("sent"))
                if not cands:
                    log(f"[{tag}] no candidates fetched; retry later")
                elif not lock.acquire(tag):
                    wait = 60
                else:
                    try:
                        goals = [g["title"] for g in _read_json(GOALS_PATH, {}).get("goals") or []
                                 if g.get("status") == "active"]
                        ok, out = run_claude(ramu_learning.prompt(cands, state.get("topics", [])[-7:],
                                                                  "; ".join(goals)), config, tag)
                    finally:
                        lock.release()
                    pick = ramu_learning.parse_pick(out, cands) if ok else None
                    if pick and send_telegram(ramu_learning.format_message(pick), tag):
                        _append_conversation_turns([{"role": "ramu_kaka", "text": ramu_learning.format_message(pick),
                                                     "learning": True}], config["conversation_history_max_turns"])
                        state = {"last_day": today, "sent": (state.get("sent") or [])[-300:] + [pick["url"]],
                                 "topics": (state.get("topics") or [])[-30:] + [pick["topic"]]}
                        _write_json(LEARNING_STATE_PATH, state)
                        _beat("learning")
                        log(f"[{tag}] sent: {pick['topic']} ({pick['source']})")
                    else:
                        log(f"[{tag}] no usable pick ({'claude failed' if not ok else 'bad output'}); retry later")
        except Exception as e:  # noqa: BLE001
            log(f"[{tag}] ERROR: {e!r}")
        stop_event.wait(wait)
    log(f"[{tag}] loop stopped")


def _record_frictions(texts: list[str], history: list[dict] | None, tag: str) -> None:
    """The user's frustration is a bug report: log it with what Ramu Kaka said just before."""
    try:
        prior = next((h.get("text", "") for h in reversed(history or []) if h.get("role") == "ramu_kaka"), "")
        data = _read_json(FRICTIONS_PATH, {})
        frictions = data.get("frictions") or []
        now = datetime.now(_TZ).isoformat(timespec="seconds")
        for t in texts:
            if t.startswith(ramu_blockers.AUTO_PREFIX):
                continue
            cats = ramu_friction.detect(t)
            if cats:
                frictions, f = ramu_friction.record(frictions, t, cats, now, prior)
                if f:
                    log(f"[{tag}] friction {f['id']} {cats}: {t[:80]!r}")
        data["frictions"] = frictions
        _write_json(FRICTIONS_PATH, data)
    except Exception as e:  # noqa: BLE001 - never let this break a reply
        log(f"[{tag}] friction record failed: {e!r}")


def watchdog_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    """Pings an outside dead-man's switch (healthchecks.io; URL in .env as
    HEALTHCHECK_URL) every 5 min. If the pings stop, the host machine is down
    and healthchecks tells the user. If the daemon is up but can't reach
    Telegram, it pings /fail instead, so the user still hears about it through
    their channel."""
    tag = "watchdog"
    url = os.environ.get("HEALTHCHECK_URL", "").strip().rstrip("/")
    if not url:
        log(f"[{tag}] HEALTHCHECK_URL not set in .env; outside watchdog off")
        return
    log(f"[{tag}] loop starting")
    stop_event.wait(90)  # give telegram its first poll
    while not stop_event.is_set():
        now = datetime.now(_TZ)
        tg = ramu_health._parse(_heartbeats.get("telegram"), now.tzinfo)
        telegram_dead = tg is None or now - tg > timedelta(minutes=20)
        target = f"{url}/fail" if telegram_dead else url
        if _fetch(target, timeout=10) is None:
            log(f"[{tag}] ping failed ({'fail' if telegram_dead else 'ok'} ping)")
        elif telegram_dead:
            log(f"[{tag}] told healthchecks: telegram unreachable")
        stop_event.wait(300)
    log(f"[{tag}] loop stopped")


def goals_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    """Seeds goals.json once, and refreshes life.db from ClickUp/Toggl every few
    hours so goal progress (and the dashboard) reflect today, not last week."""
    tag = "goals"
    interval = config.get("goals_refresh_seconds", 10800)
    log(f"[{tag}] loop starting, refresh every {interval}s")
    while not stop_event.is_set():
        try:
            if not _read_json(GOALS_PATH, {}).get("goals"):
                _write_json(GOALS_PATH, {"goals": ramu_goals.seed(datetime.now(_TZ))})
                log(f"[{tag}] seeded {len(ramu_goals.SEED)} proposed goals")
            import ramu_dashboard
            html = ramu_dashboard.build()
            with open(os.path.join(PROJECT_DIR, "dashboard.html"), "w") as f:
                f.write(html)
            log(f"[{tag}] life.db + dashboard refreshed")
            _beat("goals")
        except Exception as e:  # noqa: BLE001
            log(f"[{tag}] ERROR: {e!r}")
        stop_event.wait(interval)
    log(f"[{tag}] loop stopped")


def radar_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    """Keeps last-contact dates current from chat and place visits (no claude
    calls), and every Friday afternoon surfaces who's drifting."""
    tag = "radar"
    day = config.get("radar_weekday", 4)  # Friday
    log(f"[{tag}] loop starting (weekday {day})")
    while not stop_event.is_set():
        try:
            now = datetime.now(_TZ)
            data = _read_json(PEOPLE_PATH, {})
            # Seeded as of now: the first time Ramu Kaka learns of these people.
            people = data.get("people") or ramu_radar.seed(now)
            events = []
            try:
                with open(DEVICE_LOG_PATH) as f:
                    events = [json.loads(line) for line in f if '"place"' in line or '"whatsapp"' in line]
            except (OSError, ValueError):
                pass
            people = ramu_radar.learn(people, turns=_read_conversation_log(), events=events)
            data["people"] = people
            y, w, _ = now.isocalendar()
            week = f"{y}-W{w:02d}"
            if now.weekday() == day and 16 <= now.hour < 21 and data.get("last_radar_week") != week:
                note = ramu_radar.radar_note(ramu_radar.overdue(people, now))
                data["last_radar_week"] = week
                if note:
                    _enqueue_device_note(note)
                    log(f"[{tag}] radar raised: {note[:120]}")
            _write_json(PEOPLE_PATH, data)
        except Exception as e:  # noqa: BLE001
            log(f"[{tag}] ERROR: {e!r}")
        stop_event.wait(1800)
    log(f"[{tag}] loop stopped")


WEEKLY_BRIEF = (
    "The weekly reset. Load and follow the weekly-reset skill (.claude/skills/weekly-reset). "
    "Pull the last 7 days yourself (life.db, conversation_log.jsonl, device_events.jsonl, "
    "footprint.json, promises.json, goals.json, frictions.json, ClickUp Life OS). Report each goal's week against its "
    "target in the numbers line. For this task only, the summary may be up to "
    "10 short lines, in the skill's format; end with its one question."
)


def weekly_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    """Once a week (Sunday by default), once the user is up: spawn the weekly reset."""
    tag = "weekly"
    day = config.get("weekly_reset_weekday", 6)  # Monday=0 ... Sunday=6
    log(f"[{tag}] loop starting (weekday {day})")
    while not stop_event.is_set():
        try:
            now = datetime.now(_TZ)
            y, w, _ = now.isocalendar()
            week = f"{y}-W{w:02d}"
            state = _read_json(WEEKLY_STATE_PATH, {})
            if now.weekday() == day and 11 <= now.hour < 21 and state.get("last_week") != week:
                today = now.date().isoformat()
                user_spoke = any(t.get("role") == USER_ROLE and str(t.get("ts", "")).startswith(today)
                               for t in _read_conversation_log())
                awake = str(_read_json(PRESENCE_PATH, {}).get("woke_at", "")).startswith(today)
                if user_spoke or awake or now.hour >= 15:
                    _write_json(WEEKLY_STATE_PATH, {"last_week": week, "started": now.isoformat()})
                    _spawn_task("Weekly reset", WEEKLY_BRIEF, config, tag)
        except Exception as e:  # noqa: BLE001
            log(f"[{tag}] ERROR: {e!r}")
        stop_event.wait(900)
    log(f"[{tag}] loop stopped")


OVERNIGHT_BRIEF = (
    f"The overnight shift. {_NAME_CAP} is asleep; work for {PRONOUN_OBJ} until morning.\n"
    f"1. Read what's open: {PRONOUN_POSS} Life OS ClickUp list ({CLICKUP_LIFE_OS_LIST_ID}), today's and tomorrow's "
    "personal calendar, the last day of conversation_log.jsonl, promises.json, "
    "presence.json, and the relevant .claude/skills playbooks.\n"
    f"Also goals.json: {PRONOUN_POSS} goals with live progress. Favour work that moves a goal that's behind.\n"
    "2. Pick the 2-4 items where reversible work moves things forward and DO it: chase an "
    "errand to closure, line up verified events for the coming days, prepare tomorrow's "
    "workout, refresh the money picture, draft (not send) messages that keep getting "
    "put off, research open questions. Use the matching skill when one exists. "
    "Log what you did as ClickUp comments on the right tasks.\n"
    f"Also frictions.json: moments {USER_NAME} was frustrated with Ramu Kaka. For each open friction with no "
    "diagnosis, write 'diagnosis' (what actually went wrong, one sentence) and 'fix' (the lesson in one "
    "line, and whether it needs a memory note, a skill edit, or a code change). Save a memory lesson "
    "where that alone fixes it. Don't edit code or skills yourself; mark those 'needs session'.\n"
    f"3. Never spend money, never message anyone as {PRONOUN_OBJ}, never touch company systems. "
    f"If one thing needs {PRONOUN_POSS} yes, return needs_approval with that single action.\n"
    "4. Summary: for this task only, up to 6 short lines, starting 'While you slept:' — "
    f"what's done, then at most 2 things that need {PRONOUN_OBJ}. {_NAME_CAP} reads it on waking."
)


def overnight_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    """Once a night, while the user is asleep, spawn a worker that advances their
    open items. Asleep = inside the window and nothing from them for a while."""
    tag = "overnight"
    start_h, end_h = config.get("overnight_window_local", [1, 6])
    quiet_min = config.get("overnight_min_silence_minutes", 60)
    log(f"[{tag}] loop starting, window {start_h}:00-{end_h}:00 local")
    while not stop_event.is_set():
        try:
            now = datetime.now(_TZ)
            night = (now - timedelta(hours=12)).date().isoformat()  # the night that began yesterday evening
            state = _read_json(OVERNIGHT_STATE_PATH, {})
            if start_h <= now.hour < end_h and state.get("last_night") != night:
                last_user_ts = max((t.get("ts", "") for t in _read_conversation_log()
                                if t.get("role") == USER_ROLE), default="")
                ts = ramu_ingest._parse(last_user_ts, now)
                if ts is None or now - ts >= timedelta(minutes=quiet_min):
                    _write_json(OVERNIGHT_STATE_PATH, {"last_night": night, "started": now.isoformat()})
                    _spawn_task("Overnight shift", OVERNIGHT_BRIEF, config, tag)
        except Exception as e:  # noqa: BLE001
            log(f"[{tag}] ERROR: {e!r}")
        stop_event.wait(600)
    log(f"[{tag}] loop stopped")


LOOKAHEAD_STATE_PATH = os.path.join(MEMORY_DIR, ".ramu_lookahead.json")  # outside the synced dir


def lookahead_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    """Preps the next 48h like a chief of staff: 10:00, 18:00, and when the
    user's personal calendar changes. See ramu_lookahead."""
    tag = "lookahead"
    log(f"[{tag}] loop starting")
    stop_event.wait(180)
    while not stop_event.is_set():
        try:
            now = datetime.now(_TZ)
            events = fetch_events_ahead(tag, 48, personal=True)
            if events is not None:
                state = _read_json(LOOKAHEAD_STATE_PATH, {})
                fp = ramu_lookahead.fingerprint(events)
                run, reason = ramu_lookahead.due(now, state, fp)
                running = any(t.get("lookahead") and t["status"] in ramu_tasks.LIVE for t in ramu_tasks.load(TASKS_PATH))
                if run and events and not running:
                    company = fetch_events_ahead(tag, 48, personal=False) or []
                    brief = (_today_chat_block(now) + _orders_block()
                             + ramu_lookahead.brief(events, company, now, PERSONAL_CALENDAR_ID, PERSONAL_CALENDAR_ACCOUNT))
                    _spawn_task("Lookahead: next 48h", brief, config, tag, unprompted=True, lookahead=True)
                    log(f"[{tag}] prepping {len(events)} event(s) ({reason})")
                if run or not state.get("fp"):
                    _write_json(LOOKAHEAD_STATE_PATH, ramu_lookahead.mark(state, now, fp, reason if run else ""))
        except Exception as e:  # noqa: BLE001
            log(f"[{tag}] ERROR: {e!r}")
        stop_event.wait(900)
    log(f"[{tag}] loop stopped")


def promise_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    """Sends what Ramu Kaka promised, when it promised it. The user asked for these,
    so they skip the unsolicited gate; they still get chased if ignored."""
    tag = "promise"
    interval = config.get("promise_poll_interval_seconds", 30)
    log(f"[{tag}] loop starting, interval={interval}s")
    while not stop_event.is_set():
        try:
            now = datetime.now(_TZ)
            send, expire = ramu_promises.due(ramu_promises.load(PROMISES_PATH), now)
            if expire:
                ramu_promises.mark(PROMISES_PATH, {p["id"] for p in expire}, "expired", now)
                log(f"[{tag}] expired {len(expire)} stale promise(s) (daemon was down past their time)")
            for p in send:
                if p.get("call"):
                    call_user(tag, f"You promised to call {USER_NAME} now. What to say: {p['say']}",
                               allow_night=True, config=config)
                if send_telegram(p["say"], tag):
                    _append_conversation_turns(
                        [{"role": "ramu_kaka", "text": p["say"], "unsolicited": True,
                          "urgency": p.get("urgency", "important"), "promise": p["id"]}],
                        config["conversation_history_max_turns"],
                    )
                    ramu_promises.mark(PROMISES_PATH, {p["id"]}, "sent", now)
                    log(f"[{tag}] kept promise {p['id']} due {p['at']}")
        except Exception as e:  # a bad cycle must never kill the loop
            log(f"[{tag}] ERROR in promise cycle: {e!r}")
        stop_event.wait(interval)
    log(f"[{tag}] loop stopped")


def checkin_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    """Opens a check-in: ONE silent question, through the ping gate, when
    something is due. Taps take it from there (see _handle_checkin_taps)."""
    tag = "checkin"
    interval = config.get("checkin_poll_interval_seconds", 600)
    log(f"[{tag}] loop starting, interval={interval}s")
    while not stop_event.is_set():
        try:
            allowed, _why = _unsolicited_gate(config, tag)
            if allowed and ramu_checkins.in_conversation(_read_conversation_log(), datetime.now(_TZ)):
                allowed = False
            key = _next_checkin_question(config) if allowed else None
            if key:
                text, buttons = ramu_checkins.build(key)
                if send_telegram(text, tag, buttons=buttons, silent=True):
                    _append_conversation_turns(
                        [{"role": "ramu_kaka", "text": text, "unsolicited": True, "checkin_q": key}],
                        config["conversation_history_max_turns"],
                    )
                    log(f"[{tag}] opened check-in: {key}")
        except Exception as e:  # a bad cycle must never kill the loop
            log(f"[{tag}] ERROR in check-in cycle: {e!r}")
        stop_event.wait(interval)
    log(f"[{tag}] loop stopped")


def telegram_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    tag = "telegram"
    interval = config["telegram_poll_interval_seconds"]
    retry_budget = config["telegram_lock_retry_seconds"]
    retry_poll = config["telegram_lock_retry_poll_seconds"]
    log(f"[{tag}] loop starting, interval={interval}s")
    while not stop_event.is_set():
        _maybe_consolidate_memory(config, tag)
        _ensure_warm_session(config, tag)
        _t0 = time.time()
        messages = poll_telegram(tag, config.get("telegram_long_poll_seconds", 0))
        if config.get("telegram_long_poll_seconds") and not messages and time.time() - _t0 < 3:
            # A real long-poll holds for many seconds; returning at once means the
            # call failed. Back off so an outage doesn't hammer Composio every second.
            stop_event.wait(10)
        actionable = [
            m
            for m in messages
            if m.get("type") in ("message", "callback_query") and m.get("text")
        ]
        # The blocker watcher's synthetic notes turn into a reply the user did not
        # ask for, so they go through the same gate. When the user is actually talking to
        # it, they fold into that reply as before.
        taps = [m for m in actionable
                if m.get("type") == "callback_query" and ramu_checkins.parse(m.get("text"))]
        if taps:
            actionable = [m for m in actionable if m not in taps]
            _handle_checkin_taps(taps, config, tag)
        approvals = [m for m in actionable
                     if m.get("type") == "callback_query" and ramu_tasks.parse_approval(m.get("text"))]
        if approvals:
            actionable = [m for m in actionable if m not in approvals]
            _handle_approval_taps(approvals, config, tag)
        talking = bool(taps) or bool(approvals) or bool(actionable)
        synthetic = []
        if talking or _unsolicited_gate(config, tag)[0]:
            synthetic = _drain_synthetic_inbox(tag)
        unsolicited_reply = bool(synthetic) and not talking
        actionable += synthetic
        if messages and not actionable and not taps and not approvals:
            log(f"[{tag}] {len(messages)} update(s), none actionable (unhandled type)")
        if actionable:
            # Batched: a burst of N messages in one poll cycle becomes ONE
            # claude -p call and ONE reply, not N of each (2026-09-13) --
            # a person answers a burst of thoughts once, not piecemeal.
            # Fixed 2026-09-22: listen.sh now surfaces "reply_to_text" when
            # the user taps reply on an earlier message (Telegram's own
            # reply_to_message field) -- previously dropped entirely, so a
            # reply lost all context about which message it was answering.
            texts = [
                (f'[replying to: "{m["reply_to_text"]}"] {m["text"]}' if m.get("reply_to_text") else m["text"])
                for m in actionable
            ]
            # Fixed 2026-09-14: photos were detected (listen.sh already did
            # that part right) but never actually downloaded or read --
            # replies were based on the caption alone, or nothing. Only
            # "photo" for now (the common case); other media kinds (video,
            # voice, documents) still degrade to caption/placeholder text.
            typing = _start_typing(tag)
            image_paths = []
            if any(m.get("media_kind") for m in actionable):
                # Everything the user sends is a reading about their life: photos, voice
                # notes, video, documents, locations. Convert it before the model
                # sees it (transcripts, frames, file paths).
                ramu_media.cleanup()
                fetch = _media_fetcher(tag)
                for i, m in enumerate(actionable):
                    res = ramu_media.ingest(m, fetch)
                    if res["note"]:
                        texts[i] = f"{texts[i]}\n{res['note']}"
                    image_paths += res["paths"]
                    log(f"[{tag}] media {m.get('media_kind')}: {len(res['paths'])} file(s), note={len(res['note'])} chars")
            log(f"[{tag}] {len(texts)} new message(s) ({len(image_paths)} attachment(s)), replying once")

            # These messages are ALREADY consumed from Telegram's offset
            # (listen.sh's own invariant) -- from here on, giving up means
            # permanent silence, not "try again next cycle". So retry the
            # lock instead of a single attempt. (RELIABILITY FIX 2026-09-14.)
            got_lock = False
            waited = 0
            while waited < retry_budget and not stop_event.is_set():
                if lock.acquire(tag):
                    got_lock = True
                    break
                stop_event.wait(retry_poll)
                waited += retry_poll

            if not got_lock:
                typing.set()
                log(f"[{tag}] could not get lock after {waited}s, sending fallback reply instead of silence")
                sent = send_telegram(_FALLBACK_REPLY, tag)
                log(f"[{tag}] fallback sent={sent}")
                stop_event.wait(interval)
                continue

            try:
                # 2026-09-15: fetch open tasks so the model has real ids to
                # match "that's done" against and can actually close them.
                open_tasks = fetch_life_os_open_tasks(tag) or []
                # 2026-09-16: every claude -p call is a brand-new process
                # with zero memory of any prior exchange, so carry a short
                # rolling window, not a full archive.
                history = _read_conversation_history()
                memory_context = _recall_memory(texts, history, config, tag)
                capabilities_context = _composio_apps_block() + _recall_capabilities(texts, history, config, tag)
                ok, out = _run_telegram_turn(texts, image_paths, open_tasks, history, memory_context, capabilities_context, config, tag)
                if ok and out:
                    reply, new_tasks, close_task_ids, habit_log, email_watch = None, [], [], [], None
                    reply_buttons = None
                    spawn_tasks = []
                    task_notes = []
                    promises = []
                    place_here = None
                    goal_updates = []
                    mem_remember, mem_forget = [], []
                    cap_learn, cap_used, cap_blocked = [], [], []
                    # Tolerant: the model sometimes wraps the JSON in a
                    # sentence or a code fence. Pull the object out so its
                    # fields (incl. remember) still count and the user doesn't
                    # get raw JSON as the reply.
                    envelope = ramu_memory.extract_json_object(out, "reply")
                    if envelope is not None:
                        reply = envelope.get("reply")
                        reply_buttons = _normalize_buttons(envelope.get("buttons"))
                        spawn_tasks = envelope.get("spawn_tasks") or []
                        task_notes = envelope.get("task_notes") or []
                        promises = envelope.get("promises") or []
                        place_here = envelope.get("place_here")
                        goal_updates = envelope.get("goals") or []
                        new_tasks = envelope.get("new_tasks") or []
                        close_task_ids = envelope.get("close_task_ids") or []
                        habit_log = envelope.get("habit_log") or []
                        email_watch = envelope.get("email_watch")
                        mem_remember = envelope.get("remember") or []
                        mem_forget = envelope.get("forget") or []
                        cap_learn = envelope.get("learn") or []
                        cap_used = envelope.get("capability_used") or []
                        cap_blocked = envelope.get("blocked") or []
                    else:
                        # Fallback for a non-JSON reply (model didn't follow
                        # the envelope contract) -- treat raw output as the
                        # reply text rather than dropping it entirely.
                        reply = out
                    if reply:
                        sent = send_telegram(reply, tag, buttons=reply_buttons)
                        log(f"[{tag}] replied ({len(reply)} chars) to {len(texts)} message(s), sent={sent}, buttons={len(reply_buttons[0]) if reply_buttons else 0}")
                        _record_frictions(texts, history, tag)
                        turns = [{"role": _turn_role(t), "text": t} for t in texts]
                        turns.append({"role": "ramu_kaka", "text": reply, **({"unsolicited": True} if unsolicited_reply else {})})
                        _append_conversation_turns(turns, config["conversation_history_max_turns"])
                    else:
                        log(f"[{tag}] claude returned no reply text, nothing sent")
                    _apply_envelope_effects(
                        config, tag,
                        new_tasks=new_tasks, close_task_ids=close_task_ids,
                        habit_log=habit_log, email_watch=email_watch,
                        mem_remember=mem_remember, mem_forget=mem_forget,
                        cap_learn=cap_learn, cap_used=cap_used, cap_blocked=cap_blocked,
                        spawn_tasks=spawn_tasks, task_notes=task_notes, promises=promises, place_here=place_here,
                        said_texts=[t for t in texts if not t.startswith(ramu_blockers.AUTO_PREFIX)],
                        goal_updates=goal_updates,
                    )
                elif ok and not out:
                    log(f"[{tag}] claude returned empty output, nothing sent")
                else:
                    # run_claude already logged the specific error (timeout,
                    # non-zero exit, etc). Never total silence from here.
                    log(f"[{tag}] claude call failed, sending fallback reply instead of silence")
                    sent = send_telegram(_FALLBACK_REPLY, tag)
                    log(f"[{tag}] fallback sent={sent}")
            finally:
                typing.set()
                lock.release()
        stop_event.wait(interval)
    log(f"[{tag}] loop stopped")


def clickup_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    tag = "clickup"
    interval = config["clickup_check_interval_seconds"]
    log(f"[{tag}] loop starting, interval={interval}s")
    while not stop_event.is_set():
        tasks = fetch_clickup_due_soon(tag, config["resurface_cooldown_hours"])
        if tasks is None:
            # Composio fetch itself failed -- log only, no claude -p call
            # (nothing to judge, and per the voice rules, this is never
            # something to tell the user about). Retried next cycle.
            log(f"[{tag}] fetch failed, skipping this cycle (no claude call)")
        elif not tasks:
            log(f"[{tag}] fetch ok, 0 tasks due/overdue, skipping claude call")
        elif lock.acquire(tag):
            try:
                ok, _out = run_claude(_prepend_memory(build_clickup_check_prompt(tasks),
                    _texts(tasks, "name"), config, tag), config, tag)
                log(f"[{tag}] check {'done' if ok else 'failed'} ({len(tasks)} candidate task(s))")
            finally:
                lock.release()
        stop_event.wait(interval)
    log(f"[{tag}] loop stopped")


def calendar_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    tag = "calendar"
    interval = config["calendar_check_interval_seconds"]
    log(f"[{tag}] loop starting, interval={interval}s")
    while not stop_event.is_set():
        events = fetch_calendar_next_30h(tag, config["resurface_cooldown_hours"])
        if events is None:
            log(f"[{tag}] fetch failed, skipping this cycle (no claude call)")
        elif not events:
            log(f"[{tag}] fetch ok, 0 events in next 30h, skipping claude call")
        elif lock.acquire(tag):
            try:
                ok, _out = run_claude(_prepend_memory(build_calendar_check_prompt(events),
                    _texts(events, "summary"), config, tag), config, tag)
                log(f"[{tag}] check {'done' if ok else 'failed'} ({len(events)} candidate event(s))")
            finally:
                lock.release()
        stop_event.wait(interval)
    log(f"[{tag}] loop stopped")


def _quiet_hours_active(config: dict) -> bool:
    start, end = config["quiet_hours_start_local"], config["quiet_hours_end_local"]
    hour = datetime.now(_TZ).hour
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end  # window wraps past midnight


def queue_drain_loop(config: dict, lock: ClaudeLock, stop_event: threading.Event):
    tag = "queue"
    interval = config["queue_drain_interval_seconds"]
    log(f"[{tag}] loop starting, interval={interval}s")
    while not stop_event.is_set():
        if pending_is_empty():
            log(f"[{tag}] pending.json empty, nothing to do (0 claude calls)")
        elif lock.acquire(tag):
            try:
                pending = _read_pending()
                fresh = [e for e in pending if e.get("status", "new") == "new"]
                fresh_ids = {e.get("id") for e in fresh}
                quiet = _quiet_hours_active(config)
                ok, out = run_claude(_prepend_memory(build_queue_drain_prompt(fresh, quiet),
                    _texts(fresh, "summary", "text", "name"), config, tag), config, tag)
                if not ok:
                    log(f"[{tag}] drain failed, pending.json untouched, will retry next cycle")
                else:
                    try:
                        envelope = json.loads(out) if out else {}
                    except (json.JSONDecodeError, AttributeError):
                        envelope = {}
                    message = envelope.get("message")
                    urgency = envelope.get("urgency")
                    if urgency not in ramu_pinggate.URGENCY:
                        urgency = "chat"
                    processed_ids = set(envelope.get("processed_ids") or []) & fresh_ids
                    dismissed_ids = set(envelope.get("dismissed_ids") or []) & fresh_ids
                    if message and not processed_ids:
                        log(f"[{tag}] dropped: message not tied to any new entry: {message[:150]!r}")
                        message = None
                    override_reason = (envelope.get("override_quiet_hours_reason") or "").strip()

                    # CODE-LEVEL quiet-hours enforcement, added 2026-09-15 --
                    # not just the prompt instruction above. If it's quiet
                    # hours and the model didn't give a real, specific
                    # override reason, the message is held: nothing sends,
                    # and whatever it wanted to mark "processed" stays "new"
                    # instead (it wasn't actually delivered) so it's picked
                    # up again once quiet hours end, not lost.
                    # 2026-09-22: the override is gone -- the model could talk
                    # itself past it.
                    # The gate (quiet hours, one unanswered at a time) is code and nothing the model outputs changes it.
                    if override_reason:
                        log(f"[{tag}] ignoring model's override request (none exists): {override_reason!r}")
                    allowed, why = _unsolicited_gate(config, tag, urgency=urgency)
                    held = bool(message) and not allowed
                    if held:
                        log(f"[{tag}] held ({why}): {message[:150]!r}")
                        processed_ids = set()  # nothing was actually delivered

                    sent = None
                    if message and not held:
                        sent = send_unsolicited(message, tag, config, urgency=urgency)
                        log(f"[{tag}] drain sent {urgency} message ({len(message)} chars), delivered={sent}")
                    elif not message:
                        log(f"[{tag}] drain: nothing to say this cycle")

                    if processed_ids or dismissed_ids:
                        for entry in pending:
                            if entry.get("id") in processed_ids:
                                entry["status"] = "processed"
                            elif entry.get("id") in dismissed_ids:
                                entry["status"] = "dismissed"
                        try:
                            with open(PENDING_PATH, "w") as f:
                                json.dump(pending, f, indent=2)
                        except OSError as e:
                            log(f"[{tag}] WARNING: could not write pending.json: {e}")
            finally:
                lock.release()
        stop_event.wait(interval)
    log(f"[{tag}] loop stopped")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main():
    load_env_file(ENV_PATH)
    config = load_config()
    lock = ClaudeLock(LOCK_PATH, config["lock_stale_seconds"])
    stop_event = threading.Event()

    def handle_signal(signum, _frame):
        log(f"received signal {signum}, shutting down")
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    log("=" * 60)
    log(f"daemon.py starting, pid={os.getpid()}, project_dir={PROJECT_DIR}")
    log(f"config: {json.dumps({k: v for k, v in config.items()})}")

    if config.get("mail_enabled") or config.get("whatsapp_enabled"):
        log("WARNING: mail_enabled/whatsapp_enabled set True, but no loop "
            "exists for either yet -- neither Mail nor WhatsApp is connected. "
            "TODO once connected.")
    # TODO(mail): no Gmail-polling loop exists yet. Not connected for Ramu
    # Kaka's purposes beyond what the interactive/scheduled-task path already
    # uses; add a source_fetch_loop(...) here once there's a concrete need.
    # TODO(whatsapp): Composio's WhatsApp toolkit is business-only and needs
    # its own connection before a loop for it makes sense.

    _recover_tasks()
    threads = [
        threading.Thread(
            target=telegram_loop, args=(config, lock, stop_event), name="telegram", daemon=True
        ),
        threading.Thread(
            target=clickup_loop, args=(config, lock, stop_event), name="clickup", daemon=True
        ),
        threading.Thread(
            target=calendar_loop, args=(config, lock, stop_event), name="calendar", daemon=True
        ),
        threading.Thread(
            target=daybuild_loop, args=(config, lock, stop_event), name="daybuild", daemon=True
        ),
        threading.Thread(
            target=pattern_loop, args=(config, lock, stop_event), name="pattern", daemon=True
        ),
        threading.Thread(
            target=email_watch_loop, args=(config, lock, stop_event), name="email", daemon=True
        ),
        threading.Thread(
            target=memory_reflect_loop, args=(config, lock, stop_event), name="memory", daemon=True
        ),
        threading.Thread(
            target=blockers_loop, args=(config, lock, stop_event), name="blockers", daemon=True
        ),
        threading.Thread(
            target=queue_drain_loop, args=(config, lock, stop_event), name="queue", daemon=True
        ),
        threading.Thread(
            target=rpc_loop, args=(config, lock, stop_event), name="rpc", daemon=True
        ),
        threading.Thread(
            target=checkin_loop, args=(config, lock, stop_event), name="checkin", daemon=True
        ),
        threading.Thread(
            target=chase_loop, args=(config, lock, stop_event), name="chase", daemon=True
        ),
        threading.Thread(
            target=promise_loop, args=(config, lock, stop_event), name="promise", daemon=True
        ),
        threading.Thread(
            target=ingest_loop, args=(config, lock, stop_event), name="ingest", daemon=True
        ),
        threading.Thread(
            target=overnight_loop, args=(config, lock, stop_event), name="overnight", daemon=True
        ),
        threading.Thread(
            target=lookahead_loop, args=(config, lock, stop_event), name="lookahead", daemon=True
        ),
        threading.Thread(
            target=bank_mail_loop, args=(config, lock, stop_event), name="bankmail", daemon=True
        ),
        threading.Thread(
            target=footprint_loop, args=(config, lock, stop_event), name="footprint", daemon=True
        ),
        threading.Thread(
            target=weekly_loop, args=(config, lock, stop_event), name="weekly", daemon=True
        ),
        threading.Thread(
            target=radar_loop, args=(config, lock, stop_event), name="radar", daemon=True
        ),
        threading.Thread(
            target=goals_loop, args=(config, lock, stop_event), name="goals", daemon=True
        ),
        threading.Thread(
            target=health_loop, args=(config, lock, stop_event), name="health", daemon=True
        ),
        threading.Thread(
            target=learning_loop, args=(config, lock, stop_event), name="learning", daemon=True
        ),
        threading.Thread(
            target=watchdog_loop, args=(config, lock, stop_event), name="watchdog", daemon=True
        ),
    ]
    global _started_at
    _started_at = datetime.now(_TZ)
    for t in threads:
        t.start()

    # Main thread just waits for a stop signal; the four loops do the work.
    try:
        while not stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        stop_event.set()

    # RELIABILITY FIX (2026-09-14): this used to be timeout=5, which could
    # kill a thread mid-`claude -p`-call -- after the subprocess had already
    # returned a real reply but before send_telegram() ran -- silently
    # dropping a message the user was actively waiting on. Give threads a real
    # chance to finish (longer than claude_timeout_seconds) before the
    # process dies out from under them.
    grace = config.get("shutdown_grace_seconds", 200)
    log(f"waiting up to {grace}s for in-flight work to finish before exiting")
    for t in threads:
        t.join(timeout=grace)
    log("daemon.py exiting cleanly")


if __name__ == "__main__":
    main()
