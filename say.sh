#!/usr/bin/env bash
# Ramu Kaka -> its person on Telegram, via the bot connected in Composio.
# No tokens here: Composio holds the credentials.
# Fails LOUDLY: exits 1 with the real error if the send fails.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHAT_ID="${RAMU_CHAT_ID:-$(cd "$DIR" && python3 -c 'import ramu_profile; print(ramu_profile.PROFILE["telegram_chat_id"] or "")')}"
[ -n "$CHAT_ID" ] || { echo "no Telegram chat id: set RAMU_CHAT_ID or telegram_chat_id in profile.json" >&2; exit 1; }
MSG="${*:-}"
[ -n "$MSG" ] || { echo "usage: say.sh <message>" >&2; exit 1; }

# Optional, all via env so plain `say.sh "text"` is unchanged:
#   RAMU_BUTTONS   JSON rows of [label, callback_data] pairs -> inline keyboard;
#                  a tap comes back through listen.sh as text = callback_data
#   RAMU_SILENT=1  send without a notification sound (check-ins never buzz them)
#   RAMU_DRY_RUN=1 validate the payload with Composio and print it, send nothing
PAYLOAD=$(python3 -c '
import json, os, sys
d = {"chat_id": sys.argv[1], "text": sys.argv[2]}
btn = os.environ.get("RAMU_BUTTONS", "").strip()
if btn:
    rows = json.loads(btn)
    d["reply_markup"] = json.dumps({"inline_keyboard": [
        [{"text": b[0], "callback_data": b[1]} for b in row] for row in rows]})
if os.environ.get("RAMU_SILENT") == "1":
    d["disable_notification"] = True
print(json.dumps(d))' "$CHAT_ID" "$MSG")

if [ "${RAMU_DRY_RUN:-}" = "1" ]; then
  composio execute TELEGRAM_SEND_MESSAGE --dry-run -d "$PAYLOAD" 2>&1
  exit $?
fi

OUT=$(composio execute TELEGRAM_SEND_MESSAGE -d "$PAYLOAD" 2>&1)

if echo "$OUT" | grep -q '"successful": true'; then
  echo "delivered"
else
  echo "TELEGRAM SEND FAILED:" >&2
  echo "$OUT" | tail -20 >&2
  exit 1
fi
