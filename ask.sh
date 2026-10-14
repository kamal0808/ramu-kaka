#!/usr/bin/env bash
# Ramu Kaka -> its person on Telegram, WITH TAPPABLE BUTTONS (inline keyboard).
# Same contract as say.sh: prints "delivered" on success, exits 1 with the
# REAL error on failure. No tokens here: Composio holds the credentials.
#
# Taps come back through listen.sh as an ordinary JSON line whose "text"
# field is the button's label (listen.sh auto-acknowledges the tap via
# TELEGRAM_ANSWER_CALLBACK_QUERY so the button stops spinning on their phone).
#
# Usage:
#   ./ask.sh "Which part of town do you live in?" "North" "South" "Central" "Other"
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHAT_ID="${RAMU_CHAT_ID:-$(cd "$DIR" && python3 -c 'import ramu_profile; print(ramu_profile.PROFILE["telegram_chat_id"] or "")')}"
[ -n "$CHAT_ID" ] || { echo "no Telegram chat id: set RAMU_CHAT_ID or telegram_chat_id in profile.json" >&2; exit 1; }

MSG="${1:-}"
[ -n "$MSG" ] || { echo "usage: ask.sh <message> <option1> [option2] ..." >&2; exit 1; }
shift
[ "$#" -ge 1 ] || { echo "usage: ask.sh <message> <option1> [option2] ..." >&2; exit 1; }

PAYLOAD=$(python3 -c '
import json, sys
chat_id = sys.argv[1]
msg = sys.argv[2]
opts = sys.argv[3:]
keyboard = {"inline_keyboard": [[{"text": o, "callback_data": o[:64]}] for o in opts]}
print(json.dumps({
    "chat_id": chat_id,
    "text": msg,
    "reply_markup": json.dumps(keyboard),
}))
' "$CHAT_ID" "$MSG" "$@")

OUT=$(composio execute TELEGRAM_SEND_MESSAGE -d "$PAYLOAD" 2>&1)

if echo "$OUT" | grep -q '"successful": true'; then
  echo "delivered"
else
  echo "TELEGRAM SEND FAILED:" >&2
  echo "$OUT" | tail -20 >&2
  exit 1
fi
