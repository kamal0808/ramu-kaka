#!/usr/bin/env bash
# Read the person's Telegram replies via the bot connected in Composio.
# Tracks the highest update_id in .last_update so messages are never
# reprocessed. Only ONE poller may run per bot.
#
# INVARIANT (do not break this): every update_id we consume MUST produce
# exactly one JSON line on stdout before we advance the offset past it.
# We've hit this bug twice already:
#   - callback_query taps with no "text" were silently swallowed
#   - non-text messages (photos, documents, forwards, etc.) had no `.text`
#     so `if txt:` silently dropped them while still advancing the offset,
#     losing those messages for good
# If you add a new Telegram update/message shape, you MUST emit a line for
# it (a real line, or the catch-all "unhandled update type" line below)
# before returning from that branch. Never `continue`/fall through to the
# offset write without having printed something for that update.
#
# Everything is emitted as one JSON line with a "text" field so existing
# consumers (which only look at .text) keep working unchanged:
#   - plain typed messages            -> {"text": "...", "type": "message"}
#   - inline-keyboard button taps      -> {"text": "<button label>", "type": "callback_query"}
#     (from ask.sh). A tapped button is auto-acknowledged via
#     TELEGRAM_ANSWER_CALLBACK_QUERY so it stops spinning on their phone.
#   - photo/document/video/etc WITH a caption -> {"text": "<caption>", "type": "message", "media_kind": "..."}
#   - photo/document/video/etc with NO caption -> {"text": "[photo, no caption]" or
#     "[document: name.pdf]", "type": "message", "media_kind": "...", "media": {...file_id etc...}}
#   - forwarded messages              -> same as above plus "forwarded": true
#   - a reply to an earlier message    -> same as above plus "reply_to_text": "<text/caption of the message being replied to>"
#   - anything else unrecognized      -> {"text": "[unhandled update type: [...]]", "type": "unhandled", "raw_keys": [...]}
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OFF_FILE="$DIR/.last_update"

# Photos, documents, voice notes, forwards, etc. all arrive as ordinary
# "message" updates (Telegram only uses separate update types for things
# like channel posts, which we don't get in a private DM), so no need to
# widen this beyond message/edited_message/callback_query.
ALLOWED='"allowed_updates": ["message", "edited_message", "callback_query"]'
# LISTEN_TIMEOUT (seconds, set by the daemon only): long-poll, so Telegram holds the
# call open and returns the moment a message lands instead of on the next sleep.
# Unset = the old instant short poll, so manual runs behave exactly as before.
LT="${LISTEN_TIMEOUT:-0}"
ARGS="{\"timeout\": $LT, $ALLOWED}"
if [ -f "$OFF_FILE" ]; then
  OFFSET=$(cat "$OFF_FILE")
  [ -n "$OFFSET" ] && ARGS="{\"offset\": $((OFFSET + 1)), \"timeout\": $LT, $ALLOWED}"
fi

composio execute TELEGRAM_GET_UPDATES -d "$ARGS" 2>&1 \
  | OFF_FILE="$OFF_FILE" python3 -c '
import sys, json, os, subprocess

raw = sys.stdin.read(); i = raw.find("{")
if i < 0:
    print("no json from composio:", raw[:400], file=sys.stderr); sys.exit(1)
d = json.loads(raw[i:])
if not d.get("successful"):
    print("getUpdates failed:", json.dumps(d)[:400], file=sys.stderr); sys.exit(1)
res = (d.get("data") or {}).get("result") or []
hi = None
to_ack = []

# kind -> (telegram field name, extra fields to lift into "media")
MEDIA_KINDS = ["photo", "document", "video", "voice", "audio", "sticker", "location", "contact"]

def media_info_for(kind, m):
    if kind == "photo":
        photos = m.get("photo") or []
        if not photos:
            return None
        largest = photos[-1]
        return {"file_id": largest.get("file_id")}
    if kind == "document":
        doc = m.get("document") or {}
        return {"file_id": doc.get("file_id"), "file_name": doc.get("file_name"), "mime_type": doc.get("mime_type")}
    if kind == "video":
        v = m.get("video") or {}
        return {"file_id": v.get("file_id"), "file_name": v.get("file_name"), "mime_type": v.get("mime_type")}
    if kind == "voice":
        v = m.get("voice") or {}
        return {"file_id": v.get("file_id"), "mime_type": v.get("mime_type")}
    if kind == "audio":
        a = m.get("audio") or {}
        return {"file_id": a.get("file_id"), "file_name": a.get("file_name"), "mime_type": a.get("mime_type")}
    if kind == "sticker":
        s = m.get("sticker") or {}
        return {"file_id": s.get("file_id"), "emoji": s.get("emoji")}
    if kind == "location":
        loc = m.get("location") or {}
        return {"latitude": loc.get("latitude"), "longitude": loc.get("longitude")}
    if kind == "contact":
        c = m.get("contact") or {}
        return {"phone_number": c.get("phone_number"), "first_name": c.get("first_name")}
    return None

def placeholder_for(kind, info):
    if kind == "document":
        name = (info or {}).get("file_name") or "unnamed file"
        return f"[document: {name}]"
    if kind == "location":
        return "[location]"
    if kind == "contact":
        name = (info or {}).get("first_name") or "contact"
        return f"[contact: {name}]"
    return f"[{kind}, no caption]"

def reply_to_text(m):
    r = m.get("reply_to_message")
    if not r:
        return None
    t = (r.get("text") or r.get("caption") or "").strip()
    return t or None

def is_forwarded(m):
    return bool(
        m.get("forward_date")
        or m.get("forward_from")
        or m.get("forward_from_chat")
        or m.get("forward_sender_name")
        or m.get("forward_origin")
    )

for u in res:
    hi = u["update_id"]
    cq = u.get("callback_query")
    if cq:
        data = (cq.get("data") or "").strip()
        msg = cq.get("message") or {}
        if data:
            print(json.dumps({
                "update_id": u["update_id"],
                "date": msg.get("date"),
                "text": data,
                "type": "callback_query",
            }))
        else:
            # A callback_query with no data would previously vanish
            # silently. Never drop it -- surface it via the catch-all shape.
            print(json.dumps({
                "update_id": u["update_id"],
                "date": msg.get("date"),
                "text": "[callback_query with no data]",
                "type": "unhandled",
                "raw_keys": sorted(cq.keys()),
            }))
        if cq.get("id"):
            to_ack.append(cq["id"])
        continue

    m = u.get("message") or u.get("edited_message")
    if m is None:
        # Some other update type entirely (e.g. channel_post, poll,
        # my_chat_member, chat_member...) that we did not anticipate.
        keys = sorted(u.keys())
        print(json.dumps({
            "update_id": u["update_id"],
            "text": f"[unhandled update type: {keys}]",
            "type": "unhandled",
            "raw_keys": keys,
        }))
        continue

    forwarded = is_forwarded(m)
    txt = (m.get("text") or "").strip()
    caption = (m.get("caption") or "").strip()

    media_kind = None
    for k in MEDIA_KINDS:
        if m.get(k):
            media_kind = k
            break

    out = None
    if txt:
        out = {
            "update_id": u["update_id"],
            "date": m.get("date"),
            "text": txt,
            "type": "message",
        }
    elif media_kind:
        info = media_info_for(media_kind, m)
        if caption:
            out = {
                "update_id": u["update_id"],
                "date": m.get("date"),
                "text": caption,
                "type": "message",
                "media_kind": media_kind,
            }
        else:
            out = {
                "update_id": u["update_id"],
                "date": m.get("date"),
                "text": placeholder_for(media_kind, info),
                "type": "message",
                "media_kind": media_kind,
            }
        if info:
            out["media"] = info
    elif caption:
        # A caption with no media field we recognized -- still surface it
        # rather than dropping it.
        out = {
            "update_id": u["update_id"],
            "date": m.get("date"),
            "text": caption,
            "type": "message",
        }
    else:
        # Message subtype we did not anticipate (poll, venue, dice,
        # new_chat_members, pinned_message, ...). Never drop it silently.
        keys = sorted(m.keys())
        out = {
            "update_id": u["update_id"],
            "date": m.get("date"),
            "text": f"[unhandled update type: {keys}]",
            "type": "unhandled",
            "raw_keys": keys,
        }

    if forwarded:
        out["forwarded"] = True

    rtext = reply_to_text(m)
    if rtext:
        out["reply_to_text"] = rtext

    print(json.dumps(out))

if hi is not None:
    with open(os.environ["OFF_FILE"], "w") as f:
        f.write(str(hi))
# Stop the button spinner on their phone. Best-effort: never let an ack
# failure block emitting the tap itself (already printed above).
for cqid in to_ack:
    try:
        subprocess.run(
            ["composio", "execute", "TELEGRAM_ANSWER_CALLBACK_QUERY",
             "-d", json.dumps({"callback_query_id": cqid})],
            capture_output=True, timeout=15, check=False,
        )
    except Exception as e:
        print(f"warning: failed to ack callback {cqid}: {e}", file=sys.stderr)
'
