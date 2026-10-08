"""Who this Ramu Kaka looks after, and which of their accounts it may use.

Everything personal lives in profile.json next to this file (gitignored).
profile.example.json is the committed template: copy it to profile.json and
fill it in. Code never hardcodes a name, number, email, ID or place; it reads
them from here.

Missing keys fall back to the defaults below, so a partial profile.json works
and every integration is optional: an empty ID means "not connected", and the
code that needs it skips quietly.

Optional keys some modules read with their own fallback when absent (leave
them out rather than setting them empty, or the fallback is lost):
  work_scope_block   terms the agent must never act on (its person's work systems)
  people             relationship radar seed: [{"name", "relation", ...}]
  tracked_habit      {"keyword", "cap", "cap_start", "cap_end"}: a daily limit for one habit
                     (e.g. coffee), shown on the habit tile
  income             {"hourly_project_id", "hourly_rate", "withholding",
                      "other_monthly", "target_monthly", "currency_symbol"}
  hq_task_ids        ClickUp task ids per life area, e.g. {"sleep": "...",
                     "habits": "..."}; the areas are examples, use your own
  toggl_account      Composio account for Toggl
  footprint_senders  inbox senders that count as life signals
  meal_merchants, grocery_merchants   merchant names for meal/grocery detection
  learning_feeds     {"label": "rss url"}; interests: list of topics
  speech_hint        vocabulary hint for voice-note transcription
"""

import json
import os
from zoneinfo import ZoneInfo

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_PATH = os.environ.get("RAMU_PROFILE", os.path.join(PROJECT_DIR, "profile.json"))

DEFAULTS = {
    # The person. `name` is how prompts refer to them; `pronoun` is "he",
    # "she" or "they" and is used in third-person prompt text.
    "name": "the user",
    "pronoun": "they",
    "timezone": "UTC",
    "home_city": "",
    # Free text the agent gets as context about their work and what's in or
    # out of scope (e.g. "Runs a small bakery; never act on the bakery's
    # accounts"). Empty is fine.
    "work_context": "",
    # The agent's standing instructions, relative to the project dir.
    "instructions_file": "CLAUDE.md",
    # Telegram: only messages from this chat id are ever read.
    "telegram_chat_id": None,
    # Phone number (digits, with country code) for voice calls. Empty = no calls.
    "phone_number": "",
    # ClickUp: the list that holds their life tasks, and their user id.
    "clickup_list_id": "",
    "clickup_user_id": None,
    # Composio connected-account ids and the matching calendar ids.
    "gmail_personal_account": "",
    "gmail_work_account": "",
    "personal_calendar_account": "",
    "personal_calendar_id": "",
    "work_calendar_account": "",
    "work_calendar_id": "",
    "slack_account": "",
    "slack_team_id": "",
    # Senders whose emails are bank/card transaction alerts.
    "bank_alert_senders": [],
}


def load(path: str = PROFILE_PATH) -> dict:
    profile = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        profile.update({k: v for k, v in data.items() if not k.startswith("_")})
    except FileNotFoundError:
        pass
    return profile


PROFILE = load()
NAME = PROFILE["name"]
PRONOUN = PROFILE["pronoun"]
TZ = ZoneInfo(PROFILE["timezone"])
