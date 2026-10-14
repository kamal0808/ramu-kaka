"""Durable memory for Ramu Kaka.

WHY THIS EXISTS. Every `claude -p` call the daemon makes is a
cold process. Until now the only carry-over was a 16-turn Telegram window
(conversation_history.json) plus the curated files under MEMORY_DIR that the
prompt tells the model to read *whole* every time (~25 KB) -- and the daemon
never wrote anything back. So a person, a preference or a decision the user
mentioned on Telegram was gone within eight exchanges.

Design, on purpose boring:
  * One markdown file per memory under <project>/memory/ -- readable, greppable,
    editable by hand or from an interactive session. No database, no
    embeddings (flat markdown until it stops scaling).
  * Recall is chosen, not dumped: keyword overlap x importance x recency,
    packed into a token budget. Importance-5 entries are always included.
  * Writes are upserts by key, so a corrected fact REPLACES the old one
    instead of sitting beside it as a contradiction.
  * A guard refuses anything that looks like a credential, card number or OTP.
  * consolidate() is deterministic (no LLM): merges near-duplicates and prunes
    stale, low-importance entries.

This module has no dependency on daemon.py. It never raises to the caller for
ordinary bad input; it returns a short human-readable result per item so the
daemon can log it.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from datetime import datetime

import ramu_profile

MEMORY_TYPES = ("person", "place", "preference", "fact", "event", "routine", "lesson")

_TZ = ramu_profile.TZ

MAX_TEXT_CHARS = 500
DEFAULT_MAX_ENTRIES = 400
DECAY_HALF_LIFE_DAYS = 45.0
DECAY_FLOOR = 0.4
DUPLICATE_JACCARD = 0.6  # on write: same-type entry this similar is the same fact
CONSOLIDATE_JACCARD = 0.7
STALE_DAYS = 90  # importance<=1 and untouched this long -> pruned

_STOPWORDS = frozenset(
    """a about after all also am an and any are as at be because been before but by
    can could did do does doing done for from get got had has have he her him his how i
    if in into is it its just me more most my no not now of on one or our out over she
    so some than that the their them then there these they this to too up us was we
    were what when where which who why will with would you your kaka ramu""".split()
)

# Concept groups used ONLY when scoring recall, so "friends" finds a memory that
# says "meetup". Deliberately small and personal-life shaped; extend as real
# misses turn up. (Not used for duplicate detection -- that stays exact.)
_SYNONYM_GROUPS = [
    {"social", "friends", "friend", "meet", "meetup", "hangout", "people"},
    {"eat", "eating", "dinner", "lunch", "breakfast", "food", "meal", "snack", "hungry", "cook", "cooking"},
    {"gym", "workout", "training", "trainer", "lift", "lifting", "session", "dumbbell"},
    {"sleep", "sleeping", "tired", "rest", "wake", "woke", "nap", "insomnia", "bedtime"},
    {"money", "income", "earn", "earning", "salary", "spend", "budget", "revenue", "expense", "expenses"},
    {"anxious", "anxiety", "stress", "stressed", "worried", "panic", "mood", "low", "sad", "down"},
    {"habit", "habits", "routine", "routines", "streak", "daily"},
    {"parent", "parents", "mom", "dad", "mother", "father", "family"},
    {"partner", "spouse", "girlfriend", "boyfriend", "relationship", "dating", "marriage"},
]
_SYN_INDEX = {w: i for i, grp in enumerate(_SYNONYM_GROUPS) for w in grp}


def _canon(tok_set: set[str]) -> set[str]:
    return {f"~g{_SYN_INDEX[t]}" if t in _SYN_INDEX else t for t in tok_set}


_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b\d{9,10}:[A-Za-z0-9_\-]{30,}"),  # telegram bot token
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),  # card-number-like run of digits
    re.compile(r"(?i)\b(password|passcode|passwd|cvv|otp|pin code|api[ _-]?key|secret key|private key)\b\s*(is|:|=)"),
]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(_TZ)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=_TZ)


def slugify(key: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(key).lower()).strip("-")
    return slug[:60].strip("-")


def tokens(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9]+", str(text).lower()):
        if len(w) < 3 or w in _STOPWORDS:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def looks_secret(text: str) -> bool:
    return any(p.search(text) for p in _SECRET_PATTERNS)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


# ---------------------------------------------------------------------------
# storage: one markdown file per entry, tiny frontmatter
# ---------------------------------------------------------------------------

_FIELDS = ("key", "type", "importance", "created", "updated", "last_used", "uses", "source")


def _path(mem_dir: str, slug: str) -> str:
    return os.path.join(mem_dir, f"{slug}.md")


def _serialise(e: dict) -> str:
    head = "\n".join(f"{f}: {e.get(f, '')}" for f in _FIELDS)
    return f"---\n{head}\n---\n{e['text'].strip()}\n"


def _read_file(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None
    m = re.match(r"---\n(.*?)\n---\n(.*)", raw, re.S)
    if not m:
        return None
    meta = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip()
    if not meta.get("key"):
        return None
    try:
        importance = min(5, max(1, int(meta.get("importance", 3))))
    except ValueError:
        importance = 3
    try:
        uses = int(meta.get("uses", 0))
    except ValueError:
        uses = 0
    return {
        "key": meta["key"],
        "slug": os.path.basename(path)[:-3],
        "type": meta.get("type") if meta.get("type") in MEMORY_TYPES else "fact",
        "importance": importance,
        "created": meta.get("created", ""),
        "updated": meta.get("updated", ""),
        "last_used": meta.get("last_used", ""),
        "uses": uses,
        "source": meta.get("source", ""),
        "text": m.group(2).strip(),
    }


def _write_file(mem_dir: str, e: dict) -> None:
    os.makedirs(mem_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=mem_dir, prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_serialise(e))
        os.replace(tmp, _path(mem_dir, e["slug"]))
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def load_all(mem_dir: str) -> list[dict]:
    try:
        names = sorted(os.listdir(mem_dir))
    except OSError:
        return []
    entries = []
    for n in names:
        if n.endswith(".md") and not n.startswith("."):
            e = _read_file(os.path.join(mem_dir, n))
            if e:
                entries.append(e)
    return entries


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def _age_decay(e: dict, now: datetime) -> float:
    stamps = [d for d in (_parse_iso(e.get("updated")), _parse_iso(e.get("last_used"))) if d]
    if not stamps:
        return DECAY_FLOOR
    age_days = max(0.0, (now - max(stamps)).total_seconds() / 86400.0)
    return max(DECAY_FLOOR, 0.5 ** (age_days / DECAY_HALF_LIFE_DAYS))


def _score(e: dict, q: set[str], now: datetime) -> float:
    if not q:
        return 0.0
    qc = _canon(q)
    overlap = len(qc & _canon(tokens(e["key"]) | tokens(e["text"])))
    if overlap == 0:
        return 0.0
    relevance = overlap / math.sqrt(len(qc))
    return relevance * (e["importance"] / 3.0) * _age_decay(e, now)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def remember(items: list, source: str = "telegram", mem_dir: str = "", max_entries: int = DEFAULT_MAX_ENTRIES) -> list[str]:
    """Upsert memories. Each item: {"key", "text", "type"?, "importance"?}.

    Returns one short result string per item ("saved: x", "updated: x",
    "merged: x", "skipped (...): x") -- for the daemon log, never for the user.
    """
    results: list[str] = []
    if not mem_dir:
        return ["skipped: no memory dir"]
    now = _now()
    existing = {e["slug"]: e for e in load_all(mem_dir)}

    for item in items or []:
        if not isinstance(item, dict):
            results.append("skipped (not an object)")
            continue
        key = str(item.get("key") or "").strip()
        text = " ".join(str(item.get("text") or "").split())
        slug = slugify(key)
        if not slug or not text:
            results.append(f"skipped (empty key/text): {key!r}")
            continue
        if looks_secret(key + " " + text):
            results.append(f"skipped (looks like a secret/credential): {slug}")
            continue
        text = text[:MAX_TEXT_CHARS]
        mtype = item.get("type") if item.get("type") in MEMORY_TYPES else "fact"
        try:
            importance = min(5, max(1, int(item.get("importance", 3))))
        except (TypeError, ValueError):
            importance = 3

        target = existing.get(slug)
        verb = "updated"
        if target is None:
            qt = tokens(key + " " + text)
            best, best_sim = None, 0.0
            for other in existing.values():
                if other["type"] != mtype:
                    continue
                sim = _jaccard(qt, tokens(other["key"] + " " + other["text"]))
                if sim > best_sim:
                    best, best_sim = other, sim
            if best is not None and best_sim >= DUPLICATE_JACCARD:
                target, verb = best, "merged"

        if target is None:
            entry = {
                "key": key, "slug": slug, "type": mtype, "importance": importance,
                "created": _iso(now), "updated": _iso(now), "last_used": "", "uses": 0,
                "source": source, "text": text,
            }
            verb = "saved"
        else:
            entry = dict(target)
            entry.update(text=text, updated=_iso(now), source=source,
                         importance=max(target["importance"], importance))
        try:
            _write_file(mem_dir, entry)
        except OSError as e:
            results.append(f"skipped (write failed: {e}): {slug}")
            continue
        existing[entry["slug"]] = entry
        results.append(f"{verb}: {entry['slug']}")

    if len(existing) > max_entries:
        results.append(f"pruned {_prune_to(mem_dir, existing, max_entries, now)} over cap")
    return results


def forget(keys: list, mem_dir: str) -> list[str]:
    results = []
    for k in keys or []:
        slug = slugify(k)
        if not slug:
            continue
        try:
            os.remove(_path(mem_dir, slug))
            results.append(f"forgot: {slug}")
        except FileNotFoundError:
            results.append(f"not found: {slug}")
        except OSError as e:
            results.append(f"skipped (remove failed: {e}): {slug}")
    return results


def recall(query: str, mem_dir: str, budget_tokens: int = 700, max_items: int = 12, touch: bool = True) -> str:
    """The most relevant memories for `query`, packed into a token budget.

    Importance-5 entries are always candidates (the user's non-negotiables) even
    with zero keyword overlap; they rank after relevant matches.
    """
    entries = load_all(mem_dir)
    if not entries:
        return ""
    now = _now()
    q = tokens(query)
    scored = []
    for e in entries:
        s = _score(e, q, now)
        if s > 0:
            scored.append((s + 10.0, e))  # relevant hits first
        elif e["importance"] >= 5:
            scored.append((e["importance"] * _age_decay(e, now), e))
    scored.sort(key=lambda t: t[0], reverse=True)

    lines, used, chosen = [], 0, []
    for _, e in scored[:max_items * 2]:
        day = (e["updated"] or e["created"])[:10]
        line = f"- [{e['type']}] {e['key']}: {e['text']} (as of {day})"
        cost = estimate_tokens(line)
        if used + cost > budget_tokens:
            continue
        lines.append(line)
        chosen.append(e)
        used += cost
        if len(chosen) >= max_items:
            break

    if touch:
        for e in chosen:
            e["last_used"] = _iso(now)
            e["uses"] += 1
            try:
                _write_file(mem_dir, e)
            except OSError:
                pass
    return "\n".join(lines)


def consolidate(mem_dir: str) -> dict:
    """Deterministic tidy-up. Merges near-duplicates within a type (newer text
    wins, higher importance wins) and prunes stale importance-1 entries."""
    now = _now()
    entries = sorted(load_all(mem_dir), key=lambda e: e["updated"] or e["created"], reverse=True)
    merged = pruned = 0
    kept: list[dict] = []
    for e in entries:  # newest first, so the first of a duplicate pair is the keeper
        et = tokens(e["key"] + " " + e["text"])
        dup = next((k for k in kept if k["type"] == e["type"]
                    and _jaccard(et, tokens(k["key"] + " " + k["text"])) >= CONSOLIDATE_JACCARD), None)
        if dup is not None:
            dup["importance"] = max(dup["importance"], e["importance"])
            dup["uses"] += e["uses"]
            _write_file(mem_dir, dup)
            try:
                os.remove(_path(mem_dir, e["slug"]))
            except OSError:
                pass
            merged += 1
            continue
        touched = max((d for d in (_parse_iso(e["updated"]), _parse_iso(e["last_used"])) if d), default=None)
        if e["importance"] <= 1 and touched and (now - touched).days >= STALE_DAYS:
            try:
                os.remove(_path(mem_dir, e["slug"]))
            except OSError:
                pass
            pruned += 1
            continue
        kept.append(e)
    return {"merged": merged, "pruned": pruned, "kept": len(kept)}


def _prune_to(mem_dir: str, existing: dict, cap: int, now: datetime) -> int:
    """Drop the lowest-value entries until `cap` remain. Value = importance x decay."""
    ranked = sorted(existing.values(), key=lambda e: e["importance"] * _age_decay(e, now))
    n = 0
    for e in ranked[: max(0, len(existing) - cap)]:
        if e["importance"] >= 5:
            continue
        try:
            os.remove(_path(mem_dir, e["slug"]))
            existing.pop(e["slug"], None)
            n += 1
        except OSError:
            pass
    return n


def stats(mem_dir: str) -> dict:
    entries = load_all(mem_dir)
    by_type: dict[str, int] = {}
    for e in entries:
        by_type[e["type"]] = by_type.get(e["type"], 0) + 1
    return {"total": len(entries), "by_type": by_type}


def extract_json_object(text: str, required_key: str | None = None) -> dict | None:
    """Pull the first JSON object out of model output that may be wrapped in
    prose or a ```json fence. The daemon asks for bare JSON, but under load
    the model sometimes adds a sentence around it; before this existed the
    whole blob was sent to the user as the reply and any `remember` was lost."""
    if not text:
        return None
    dec = json.JSONDecoder()
    i = text.find("{")
    while i != -1:
        try:
            obj, _ = dec.raw_decode(text[i:])
        except ValueError:
            obj = None
        if isinstance(obj, dict) and (required_key is None or required_key in obj):
            return obj
        i = text.find("{", i + 1)
    return None
