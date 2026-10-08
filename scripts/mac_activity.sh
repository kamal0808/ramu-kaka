#!/bin/bash
# Reports this Mac's activity to Ramu Kaka's device inbox every run (launchd, 5 min):
# seconds since last keyboard/mouse input and the frontmost app's name. Nothing
# else: no window titles, no keystrokes, no screenshots. Needs no macOS permissions.
#
# Token: $RAMU_INGEST_TOKEN, else ~/.ramu_ingest_token, else the project .env.
# URL:   $RAMU_INGEST_URL, else localhost (set it to the daemon machine's address).
set -u
TOKEN="${RAMU_INGEST_TOKEN:-}"
[ -z "$TOKEN" ] && [ -f "$HOME/.ramu_ingest_token" ] && TOKEN=$(cat "$HOME/.ramu_ingest_token")
[ -z "$TOKEN" ] && TOKEN=$(grep -s '^INGEST_TOKEN=' "$(cd "$(dirname "$0")/.." && pwd)/.env" | cut -d= -f2)
[ -z "$TOKEN" ] && exit 0
URL="${RAMU_INGEST_URL:-http://127.0.0.1:8787/event}"

idle=$(ioreg -c IOHIDSystem | awk '/HIDIdleTime/ {print int($NF/1000000000); exit}')
app=$(lsappinfo info -only name "$(lsappinfo front)" 2>/dev/null | sed -E 's/.*="(.*)"/\1/' | tr -d '"\\' | head -c 40)
host=$(scutil --get LocalHostName 2>/dev/null || hostname -s)

curl -s -m 10 -o /dev/null -X POST "$URL" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"kind\":\"mac\",\"host\":\"$host\",\"idle_s\":${idle:-0},\"app\":\"$app\",\"interval_s\":300}" || true
