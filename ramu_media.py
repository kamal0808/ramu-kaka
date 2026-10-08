"""Turn anything the user sends on Telegram into something the model can use.

Telegram is Ramu Kaka's sensor bus: a meal photo, a voice note, a screenshot of
a step count, a shared location, a forwarded bank SMS are all readings about
the user's life. Until now only photos were fetched; voice, video, documents and locations
reached the model as a bare placeholder like "[voice, no caption]".

ingest() returns {"note": text to append to the user's message, "paths": local files
the model should Read (images, PDFs)}. It never raises: anything that cannot be
fetched becomes an honest note so the model can say so instead of pretending.

Speech runs locally (ffmpeg + whisper.cpp on the same machine, absolute paths
because the daemon's PATH may not include Homebrew). Files live in a temp dir and
are deleted after a few days: these are personal photos and voice notes.
"""
import os
import subprocess
import tempfile
import time

import ramu_profile

FFMPEG = "/opt/homebrew/bin/ffmpeg"
WHISPER = "/opt/homebrew/bin/whisper-cli"
MODEL = os.path.expanduser("~/.cache/whisper/ggml-medium.bin")
MEDIA_DIR = os.path.join(tempfile.gettempdir(), "ramu_media")
KEEP_DAYS = 3
MAX_TEXT = 6000
# Steers whisper toward the user's vocabulary (accent, foods, names, places) so
# uncommon words are transcribed right. Set `speech_hint` in the profile.
SPEECH_HINT = ramu_profile.PROFILE.get("speech_hint", "")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"}
TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".log"}
SPEECH_KINDS = {"voice", "audio"}
VIDEO_KINDS = {"video", "video_note", "animation"}


def cleanup(directory=MEDIA_DIR, max_age_days=KEEP_DAYS):
    cutoff = time.time() - max_age_days * 86400
    try:
        for name in os.listdir(directory):
            p = os.path.join(directory, name)
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
    except OSError:
        pass


def transcribe(path, run=subprocess.run):
    """Local speech to text. Returns the transcript, or "" if it cannot."""
    wav = path + ".wav"
    try:
        run([FFMPEG, "-y", "-i", path, "-ar", "16000", "-ac", "1", wav],
            capture_output=True, timeout=120)
        hint = ["--prompt", SPEECH_HINT] if SPEECH_HINT else []
        out = run([WHISPER, "-m", MODEL, "-f", wav, "-nt", "-np", "-l", "auto", *hint],
                  capture_output=True, text=True, timeout=300)
        return " ".join((out.stdout or "").split())[:MAX_TEXT]
    except (subprocess.TimeoutExpired, OSError):
        return ""
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


def frames(path, run=subprocess.run, count=3):
    """A few still frames from a video, so the model can look at it."""
    stem = path + "_frame"
    try:
        run([FFMPEG, "-y", "-i", path, "-vf", "fps=1/3", "-frames:v", str(count), f"{stem}_%d.jpg"],
            capture_output=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        return []
    return [p for p in (f"{stem}_{i}.jpg" for i in range(1, count + 1)) if os.path.exists(p)]


def ingest(msg, fetch, run=subprocess.run):
    """`fetch(file_id)` downloads and returns a local path (raises on failure)."""
    kind = msg.get("media_kind")
    info = msg.get("media") or {}
    none = {"note": "", "paths": []}
    if not kind:
        return none

    if kind == "location":
        lat, lon = info.get("latitude"), info.get("longitude")
        return {"note": f"[{ramu_profile.NAME} shared their location: {lat}, {lon} (https://maps.google.com/?q={lat},{lon})]", "paths": []}
    if kind == "contact":
        return {"note": f"[contact card: {info.get('first_name') or ''} {info.get('phone_number') or ''}]".replace("  ", " "), "paths": []}
    if kind == "sticker":
        return {"note": f"[sticker {info.get('emoji') or ''}]".replace(" ]", "]"), "paths": []}
    if not info.get("file_id"):
        return none

    try:
        path = fetch(info["file_id"])
    except Exception as e:
        return {"note": f"[{ramu_profile.NAME} sent a {kind} but it could not be fetched: {e}]", "paths": []}

    ext = os.path.splitext(path)[1].lower()
    if kind == "photo":
        return {"note": "", "paths": [path]}
    if kind == "document":
        name = info.get("file_name") or os.path.basename(path)
        if ext in IMAGE_EXT or ext == ".pdf":
            return {"note": f"[document: {name}]", "paths": [path]}
        if ext in TEXT_EXT:
            try:
                with open(path, errors="replace") as f:
                    body = f.read(MAX_TEXT)
            except OSError:
                body = ""
            return {"note": f"[document {name}, contents:]\n{body}", "paths": []}
        return {"note": f"[document {name} saved at {path}, type {info.get('mime_type') or ext or 'unknown'}]", "paths": []}
    if kind in SPEECH_KINDS or kind in VIDEO_KINDS:
        said = transcribe(path, run)
        imgs = frames(path, run) if kind in VIDEO_KINDS else []
        label = "voice note" if kind == "voice" else kind.replace("_", " ")
        note = f'[{label}, {ramu_profile.NAME} said: "{said}"]' if said else f"[{label}, nothing intelligible could be transcribed]"
        return {"note": note, "paths": imgs}
    return none
