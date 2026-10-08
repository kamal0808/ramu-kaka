"""Jarvis Protocol for Ramu Kaka: the user must never wait on the conversation.

The fast lane (the Telegram reply) only talks: it has no tools, so anything that
needs real work becomes a background task. Workers run in parallel as their own
Claude processes and report back honestly with a terminal status.

Pure functions plus a small JSON registry, no daemon imports, so it is testable.
The registry lives outside the synced project dir (see daemon.TASKS_PATH).
"""
import json
import os
import tempfile
import uuid
from datetime import datetime

import ramu_profile

_NAME = ramu_profile.NAME

STATUSES = ("queued", "running", "completed", "needs_review", "blocked",
            "failed", "needs_approval", "declined")
TERMINAL = ("completed", "needs_review", "blocked", "failed", "declined")
APPROVAL_PREFIX = "ap:"
MAX_KEEP = 30
MAX_SPAWN_PER_TURN = 3


# ---- registry ---------------------------------------------------------------

def load(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save(path, tasks):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-tasks-")
    with os.fdopen(fd, "w") as f:
        json.dump(tasks[-MAX_KEEP:], f, indent=2)
    os.replace(tmp, path)


def new_task(title, brief, now_iso):
    return {"id": uuid.uuid4().hex[:8], "title": str(title).strip()[:80] or "task",
            "brief": str(brief).strip(), "status": "queued", "created": now_iso,
            "started": None, "finished": None, "summary": "", "session_id": None,
            "approval": None, "delivered": False}


def add(path, task):
    tasks = load(path)
    tasks.append(task)
    save(path, tasks)
    return task


def update(path, task_id, **fields):
    tasks = load(path)
    for t in tasks:
        if t["id"] == task_id:
            t.update(fields)
            save(path, tasks)
            return t
    return None


LIVE = ("queued", "running")


def add_note(path, task_id, note, now_iso):
    """The user added to or corrected a task that is still working. Returns the
    task, or None if it is gone or already finished (then the caller spawns a
    follow-up). Without this, a correction like "use the other address" never
    reaches the running task, which comes back with the wrong answer."""
    note = str(note or "").strip()
    tasks = load(path)
    for t in tasks:
        if t["id"] == task_id and note and t["status"] in LIVE:
            t.setdefault("notes", []).append({"text": note[:1000], "at": now_iso, "applied": False})
            save(path, tasks)
            return t
    return None


def take_notes(path, task_id):
    """Unapplied notes for a task, marked applied."""
    tasks = load(path)
    out = []
    for t in tasks:
        if t["id"] == task_id:
            for n in t.get("notes") or []:
                if not n.get("applied"):
                    n["applied"] = True
                    out.append(n["text"])
    if out:
        save(path, tasks)
    return out


def parse_notes(raw):
    """The fast lane's `task_notes` field -> [(task_id, note)]."""
    if not isinstance(raw, list):
        return []
    return [(str(n["id"]), str(n["note"])) for n in raw[:MAX_SPAWN_PER_TURN]
            if isinstance(n, dict) and n.get("id") and str(n.get("note") or "").strip()]


def note_prompt(notes):
    return (f"While you were working, {_NAME} added this:\n- " + "\n- ".join(notes)
            + "\nIt overrides anything in the brief it contradicts. Redo whatever it changes, "
            "verify, then report with the same JSON object; your summary replaces the earlier one.")


def get(path, task_id):
    return next((t for t in load(path) if t["id"] == task_id), None)


def parse_spawn(raw):
    """The fast lane's `spawn_tasks` field -> clean [(title, brief)], capped."""
    out = []
    if not isinstance(raw, list):
        return out
    for item in raw[:MAX_SPAWN_PER_TURN]:
        if isinstance(item, dict) and str(item.get("brief") or "").strip():
            out.append((str(item.get("title") or item["brief"])[:80], str(item["brief"])))
    return out


# ---- prompts ----------------------------------------------------------------

FAST_LANE_RULE = (
    f"JARVIS PROTOCOL: you are the fast lane and {_NAME} must never wait on you. You have "
    f"NO tools except reading files {_NAME} sends. Answer from what you already know in "
    "one to three sentences. Anything that needs research, the web, ClickUp, "
    "calendar or email work, building something, or more than a moment of effort "
    "goes in \"spawn_tasks\" as [{\"title\": <short>, \"brief\": <complete, "
    f"self-contained instructions: what {_NAME} asked, the relevant facts, and what done "
    f"looks like; never tell the worker to message {_NAME}, delivery is automatic>}}] "
    "(at most 3), and your reply is one line acknowledging it, like "
    "\"On it, I'll ping you when it's done.\" Do not spawn a duplicate of a task "
    f"listed as running. If {_NAME} adds to or corrects one that is running (\"make it the blue one\", "
    "\"use the other address\"), put it in \"task_notes\" as [{\"id\": <that task's id>, \"note\": "
    f"<{_NAME}'s correction, self-contained>}}]: the worker applies it before reporting. Never claim you "
    "passed something on without a task_notes entry. "
    f"PLAN CHANGES: when what {_NAME} says changes something already planned (\"the meeting moved to 3\", "
    "\"the trip is now Friday\", \"dinner's cancelled\", \"I'm done for today\"), don't just say noted: spawn a task that "
    f"fixes the plan itself ({_NAME}'s personal calendar, ClickUp, anything planned around it) and reports "
    "what it changed. Never say work is done unless a finished task below says so. "
)

_WORK = ramu_profile.PROFILE.get("work_context") or ""
_PCAL = ramu_profile.PROFILE.get("personal_calendar_id") or ""
_PCAL_ACCT = ramu_profile.PROFILE.get("personal_calendar_account") or ""
_PCAL_REF = f" ({_PCAL}, account {_PCAL_ACCT})" if _PCAL or _PCAL_ACCT else ""

WORKER_CONTRACT = (
    f"You are a background worker for Ramu Kaka, {_NAME}'s personal life assistant. {_NAME} is "
    "not waiting on you, so finish the job properly: do the whole task, retry on errors, "
    "and verify the outcome before you claim it. You have full tools (Bash, the composio "
    "CLI, web search, files). Personal life only: "
    f"never touch {_NAME}'s work or company systems"
    + (f" ({_WORK})" if _WORK else "") + f". Do NOT message {_NAME} yourself "
    "(no Telegram, no say.sh): the system delivers your summary, and two senders would "
    f"double-message {_NAME}.\n\n"
    f"APPROVAL RULE: do not send anything as {_NAME} to another person, spend money, or do "
    f"anything you cannot undo without {_NAME}'s approval. Everything reversible, free and "
    f"internal you just do: editing {_NAME}'s PERSONAL calendar{_PCAL_REF} "
    f"and {_NAME}'s ClickUp counts, so fix plans there yourself and say what "
    "you changed. Never edit the work calendar. If you reach a step that needs approval, stop there and "
    "return status needs_approval with the exact action.\n\n"
    "Report honestly. completed only if the outcome was achieved and verified; "
    "needs_review if there was real progress but it is not verified; blocked only for a "
    "concrete external blocker (missing input, access, or an unavailable system); "
    "failed if it did not work.\n\n"
    "Output ONLY one JSON object, no fences: {\"status\": \"completed|needs_review|"
    "blocked|failed|needs_approval\", \"summary\": \"<what you did or found, 1 to 3 "
    "short plain sentences in a calm, crisp Jarvis voice, no tool talk; name what it "
    f"was about, {_NAME} may have moved on. It goes to {_NAME} VERBATIM as a Telegram message, so "
    f"speak TO {_NAME}: 'you', never third person or '{_NAME}', and never a note about what to "
    f"tell {_NAME}>\", "
    "\"approval\": null or {\"action\": \"<the exact thing you would do>\"}, "
    "\"new_tasks\": [], \"habit_log\": [], \"remember\": []}."
)


def worker_prompt(task, now_str=""):
    return (WORKER_CONTRACT + ("\n\n" + now_str if now_str else "")
            + "\n\nTASK: " + task["title"] + "\n\nBRIEF:\n" + task["brief"])


def resume_prompt(approved, action):
    if approved:
        return (f"{_NAME} approved: {action}. Go ahead and do it, verify it worked, then report "
                "with the same JSON object as before.")
    return (f"{_NAME} declined: {action}. Do not do it. Stop, and report with the same JSON "
            "object (status completed or blocked, summary of where things stand).")


def parse_result(text):
    """Tolerant: the worker may wrap the JSON. Falls back to needs_review with the
    raw text so nothing it found is thrown away."""
    text = (text or "").strip()
    dec = json.JSONDecoder()
    i = text.find("{")
    while i != -1:
        try:
            obj, _ = dec.raw_decode(text[i:])
            if isinstance(obj, dict) and obj.get("status") in STATUSES:
                obj.setdefault("summary", "")
                return obj
        except ValueError:
            pass
        i = text.find("{", i + 1)
    return {"status": "needs_review", "summary": text[:600] or "It finished without a report.",
            "approval": None}


# ---- what the fast lane can see ----------------------------------------------

def awareness_block(tasks, now):
    """Running and recent background tasks, so 'how's that going?' is answered
    instantly from context and duplicates are not spawned."""
    if not tasks:
        return ""

    def age(iso):
        try:
            secs = int((now - datetime.fromisoformat(iso)).total_seconds())
        except (TypeError, ValueError):
            return "?"
        return f"{secs // 60}m" if secs >= 120 else f"{secs}s"

    live = [t for t in tasks if t["status"] in ("queued", "running", "needs_approval")]
    done = [t for t in tasks if t["status"] in TERMINAL][-5:]
    lines = []
    for t in live:
        lines.append(f"- RUNNING [{t['status']}] id={t['id']} {t['title']} (started {age(t['created'])} ago)")
    for t in done:
        lines.append(f"- {t['status'].upper()} {t['title']}: {t['summary'][:160]}")
    if not lines:
        return ""
    return "Your background tasks (you spawned these; do not duplicate the running ones):\n" + "\n".join(lines) + "\n---\n"


# ---- approvals ----------------------------------------------------------------

def approval_buttons(task_id):
    return [[["Approve", f"{APPROVAL_PREFIX}{task_id}:y"], ["Decline", f"{APPROVAL_PREFIX}{task_id}:n"]]]


def parse_approval(text):
    if not isinstance(text, str) or not text.startswith(APPROVAL_PREFIX):
        return None
    parts = text[len(APPROVAL_PREFIX):].split(":")
    if len(parts) != 2 or parts[1] not in ("y", "n") or not parts[0]:
        return None
    return parts[0], parts[1] == "y"
