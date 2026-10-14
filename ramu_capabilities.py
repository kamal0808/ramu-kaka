"""Capability library for Ramu Kaka: things it has worked out how to do.

WHY. Ramu Kaka's chat brain already has Bash / Write / WebSearch /
Composio when it runs, but nothing it works out survives the call: "hit a
wall, investigate" was re-done from scratch every time, and a lookup that took
six tool calls today took six again tomorrow. This is the durable half -- once
it has done something for real, it can save HOW, and next time it starts from
the playbook instead of from zero.

Shape, on purpose plain:
  capabilities/<slug>/CAPABILITY.md   frontmatter + numbered playbook steps
  capabilities/<slug>/run.sh|run.py   optional, non-interactive, args in / stdout out
  capabilities/LOG.jsonl              append-only audit: every learn/use/reject

The model never writes these files directly. It proposes a capability in its
reply envelope (`learn`) and the DAEMON validates, classifies and writes it --
so the guards below cannot be skipped by a model that thinks it knows better.

TIERS are computed here by a static scan of the script text, not taken on the
model's word (a declared tier can only RAISE the computed one):
  read      only looks things up
  write     changes the user's OWN things (ClickUp, calendar, files, messages to them)
  external  touches other people, money, or anything permanent
            -> status needs_approval: needs the user's explicit yes before every run

HONEST LIMIT: this is a scan, not a sandbox. `claude -p` runs with whatever
permissions the machine's settings grant; nothing here can stop a determined
script. It catches the honest mistakes and makes the risky ones visible, which
is what it is for.

Reuses the memory module's tokenising, synonyms and secret guard. No dependency
on daemon.py.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from datetime import datetime

import ramu_memory as rm
import ramu_profile

TIERS = ("read", "write", "external")
_TIER_RANK = {t: i for i, t in enumerate(TIERS)}
SCRIPT_NAMES = ("run.sh", "run.py")
MAX_SCRIPT_CHARS = 20000
MAX_STEPS_CHARS = 6000
MAX_TITLE_CHARS = 120
MAX_WHEN_CHARS = 300
BROKEN_AFTER_CONSECUTIVE_FAILURES = 3
LOG_NAME = "LOG.jsonl"

# --- guards -----------------------------------------------------------------

# Personal life only: nothing that reaches the user's work systems. The terms
# that mark work systems (employer, products, internal tools) come from the
# profile's `work_scope_block` list; an empty list blocks nothing.
_SCOPE_TERMS = [str(t).strip() for t in ramu_profile.PROFILE.get("work_scope_block", []) or [] if str(t).strip()]
_SCOPE_BLOCK = re.compile(
    r"(?i)\b(" + "|".join(re.escape(t) for t in _SCOPE_TERMS) + r")\b" if _SCOPE_TERMS else r"(?!x)x"
)
_DANGEROUS = re.compile(r"(?i)(\bsudo\b|\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r|\bmkfs\b|\bdd\s+if=|:\(\)\s*\{)")

_COMPOSIO = re.compile(r"composio\s+execute\s+([A-Za-z0-9_]+)")
_EXTERNAL_VERBS = re.compile(
    r"(?i)(SEND|MAKE_CALL|_CALL_|SMS|POST|PUBLISH|TWEET|INVITE|DELETE|REMOVE|PAY|PAYMENT|FORWARD|REPLY|SHARE|TRANSFER|SUBSCRIBE|CHARGE|ORDER|PURCHASE)"
)
_READ_VERBS = re.compile(r"(?i)_(GET|LIST|SEARCH|FETCH|FIND|READ|QUERY|RETRIEVE|CHECK|LOOKUP)(_|$)")
_CURL_MUTATING = re.compile(r"(?i)\bcurl\b[^\n|;]*(-X\s*(POST|PUT|DELETE|PATCH)|--request\s*(POST|PUT|DELETE|PATCH)|\s-d\s|--data)")
_PY_MUTATING = re.compile(r"(?i)\brequests\.(post|put|delete|patch)\b|\bhttpx\.(post|put|delete|patch)\b|method\s*=\s*['\"](POST|PUT|DELETE|PATCH)")
_FILE_WRITE = re.compile(r"(?<![0-9&\-=])>>?(?![=&])\s*(?!/dev/null)\S|\btee\b|open\([^)]*['\"][wa]\+?b?['\"]|\.write_text\(|\.write\(")
_SAY_TO_USER = re.compile(r"\bsay\.sh\b")


def classify_tier(text: str, prose: bool = False) -> str:
    """Static tier for a script/playbook text. Conservative on purpose.

    `prose=True` (playbook steps) skips the file-redirect check: placeholders
    like `from:<sender>` in prose are not shell redirects."""
    rank = 0
    for slug in _COMPOSIO.findall(text):
        if _EXTERNAL_VERBS.search(slug):
            rank = max(rank, 2)
        elif _READ_VERBS.search(slug):
            rank = max(rank, 0)
        else:
            rank = max(rank, 1)  # unknown composio action: assume it changes something
    if _CURL_MUTATING.search(text) or _PY_MUTATING.search(text):
        rank = max(rank, 2)
    if (not prose and _FILE_WRITE.search(text)) or _SAY_TO_USER.search(text):
        rank = max(rank, 1)
    return TIERS[rank]


# --- storage ----------------------------------------------------------------

_FIELDS = ("slug", "title", "when_to_use", "tier", "status", "revision", "created", "updated",
           "last_used", "uses", "successes", "failures", "consecutive_failures", "script", "last_note")
_INT_FIELDS = ("revision", "uses", "successes", "failures", "consecutive_failures")


def _dir(cap_dir: str, slug: str) -> str:
    return os.path.join(cap_dir, slug)


def _one_line(s: str, limit: int) -> str:
    return " ".join(str(s).split())[:limit]


def _serialise(c: dict) -> str:
    head = "\n".join(f"{f}: {c.get(f, '')}" for f in _FIELDS)
    return f"---\n{head}\n---\n{c.get('steps', '').strip()}\n"


def _read(cap_dir: str, slug: str) -> dict | None:
    try:
        with open(os.path.join(_dir(cap_dir, slug), "CAPABILITY.md"), "r", encoding="utf-8") as f:
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
    if not meta.get("slug"):
        return None
    for f in _INT_FIELDS:
        try:
            meta[f] = int(meta.get(f, 0))
        except ValueError:
            meta[f] = 0
    meta["tier"] = meta.get("tier") if meta.get("tier") in TIERS else "write"
    meta["steps"] = m.group(2).strip()
    return meta


def _atomic_write(path: str, content: str, mode: int | None = None) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _write(cap_dir: str, c: dict) -> None:
    _atomic_write(os.path.join(_dir(cap_dir, c["slug"]), "CAPABILITY.md"), _serialise(c))


def _log(cap_dir: str, event: str, slug: str, **extra) -> None:
    try:
        os.makedirs(cap_dir, exist_ok=True)
        with open(os.path.join(cap_dir, LOG_NAME), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": rm._iso(rm._now()), "event": event, "slug": slug, **extra}) + "\n")
    except OSError:
        pass


def load_all(cap_dir: str) -> list[dict]:
    try:
        names = sorted(os.listdir(cap_dir))
    except OSError:
        return []
    out = []
    for n in names:
        if n.startswith(".") or not os.path.isdir(_dir(cap_dir, n)):
            continue
        c = _read(cap_dir, n)
        if c:
            out.append(c)
    return out


# --- learn ------------------------------------------------------------------


def learn(items: list, cap_dir: str, source: str = "telegram") -> list[str]:
    """Validate, classify and save proposed capabilities. One result string per item.

    item: {"slug","title","when_to_use","steps","script":{"name","content"}|null,"declared_tier"?}
    """
    results: list[str] = []
    now = rm._iso(rm._now())
    for item in items or []:
        if not isinstance(item, dict):
            results.append("skipped (not an object)")
            continue
        slug = rm.slugify(item.get("slug") or item.get("title") or "")
        title = _one_line(item.get("title") or "", MAX_TITLE_CHARS)
        when = _one_line(item.get("when_to_use") or "", MAX_WHEN_CHARS)
        steps = str(item.get("steps") or "").strip()
        script = item.get("script") if isinstance(item.get("script"), dict) else None
        s_name = str(script.get("name") or "") if script else ""
        s_body = str(script.get("content") or "") if script else ""

        if not slug or not title or not when:
            results.append(f"skipped (needs slug, title and when_to_use): {slug or '?'}")
            continue
        if not steps and not s_body:
            results.append(f"skipped (no steps and no script): {slug}")
            continue
        if len(steps) > MAX_STEPS_CHARS or len(s_body) > MAX_SCRIPT_CHARS:
            results.append(f"skipped (too large): {slug}")
            continue
        if s_body and s_name not in SCRIPT_NAMES:
            results.append(f"skipped (script name must be one of {SCRIPT_NAMES}): {slug}")
            continue
        blob = "\n".join([title, when, steps, s_body])
        if _SCOPE_BLOCK.search(blob):
            _log(cap_dir, "rejected", slug, reason="scope: touches work/company systems")
            results.append(f"skipped (touches work/company systems, personal life only): {slug}")
            continue
        if rm.looks_secret(blob):
            _log(cap_dir, "rejected", slug, reason="secret")
            results.append(f"skipped (looks like it contains a secret; read credentials from env/.env instead): {slug}")
            continue
        if _DANGEROUS.search(blob):
            _log(cap_dir, "rejected", slug, reason="dangerous command")
            results.append(f"skipped (dangerous command): {slug}")
            continue

        computed = TIERS[max(_TIER_RANK[classify_tier(s_body)], _TIER_RANK[classify_tier(f'{title}\n{when}\n{steps}', prose=True)])]
        declared = item.get("declared_tier") if item.get("declared_tier") in TIERS else "read"
        tier = TIERS[max(_TIER_RANK[computed], _TIER_RANK[declared])]
        status = "needs_approval" if tier == "external" else "active"

        prior = _read(cap_dir, slug)
        cap = {
            "slug": slug, "title": title, "when_to_use": when, "tier": tier, "status": status,
            "revision": (prior["revision"] + 1) if prior else 1,
            "created": prior["created"] if prior else now, "updated": now,
            "last_used": prior["last_used"] if prior else "",
            "uses": prior["uses"] if prior else 0, "successes": prior["successes"] if prior else 0,
            "failures": prior["failures"] if prior else 0, "consecutive_failures": 0,
            "script": s_name if s_body else "", "last_note": "", "steps": steps,
        }
        try:
            if s_body:
                sp = os.path.join(_dir(cap_dir, slug), s_name)
                if os.path.exists(sp):  # one revision of rollback
                    with open(sp, "r", encoding="utf-8") as f:
                        _atomic_write(sp + ".prev", f.read(), 0o644)
                _atomic_write(sp, s_body if s_body.endswith("\n") else s_body + "\n", 0o755)
            _write(cap_dir, cap)
        except OSError as e:
            results.append(f"skipped (write failed: {e}): {slug}")
            continue
        _log(cap_dir, "updated" if prior else "learned", slug, tier=tier, revision=cap["revision"],
             source=source, sha=hashlib.sha256(s_body.encode()).hexdigest()[:12] if s_body else "")
        note = f" (tier raised {declared}->{tier} by scan)" if tier != declared else ""
        results.append(f"{'updated' if prior else 'learned'}: {slug} [{tier}]{note}")
    return results


# --- use tracking -----------------------------------------------------------


def record_use(used: list, cap_dir: str) -> list[str]:
    results = []
    now = rm._iso(rm._now())
    for u in used or []:
        if not isinstance(u, dict):
            continue
        slug = rm.slugify(u.get("slug") or "")
        c = _read(cap_dir, slug) if slug else None
        if not c:
            results.append(f"unknown capability: {slug or '?'}")
            continue
        ok = bool(u.get("ok"))
        c["uses"] += 1
        c["last_used"] = now
        c["last_note"] = _one_line(u.get("note") or "", 200)
        if ok:
            c["successes"] += 1
            c["consecutive_failures"] = 0
            if c["status"] == "broken":
                c["status"] = "needs_approval" if c["tier"] == "external" else "active"
        else:
            c["failures"] += 1
            c["consecutive_failures"] += 1
            if c["consecutive_failures"] >= BROKEN_AFTER_CONSECUTIVE_FAILURES and c["status"] != "retired":
                c["status"] = "broken"
        _write(cap_dir, c)
        _log(cap_dir, "used", slug, ok=ok, note=c["last_note"], status=c["status"])
        results.append(f"used: {slug} ok={ok} -> {c['status']}")
    return results


def retire(slug: str, cap_dir: str) -> str:
    slug = rm.slugify(slug)
    c = _read(cap_dir, slug)
    if not c:
        return f"not found: {slug}"
    c["status"] = "retired"
    _write(cap_dir, c)
    _log(cap_dir, "retired", slug)
    return f"retired: {slug}"


# --- recall -----------------------------------------------------------------


def recall(query: str, cap_dir: str, budget_tokens: int = 500, max_items: int = 4, rel_prefix: str = "capabilities") -> str:
    """The capabilities relevant to `query`, as short prompt lines. No pinned set:
    if nothing overlaps, nothing is shown."""
    caps = [c for c in load_all(cap_dir) if c["status"] != "retired"]
    if not caps:
        return ""
    qc = rm._canon(rm.tokens(query))
    if not qc:
        return ""
    scored = []
    for c in caps:
        ct = rm._canon(rm.tokens(c["slug"] + " " + c["title"] + " " + c["when_to_use"]))
        overlap = len(qc & ct)
        if not overlap:
            continue
        reliability = (c["successes"] + 1) / (c["uses"] + 2)  # Laplace: new ones start at 0.5
        penalty = 0.3 if c["status"] == "broken" else 1.0
        scored.append((overlap / math.sqrt(len(qc)) * (0.5 + reliability) * penalty, c))
    scored.sort(key=lambda t: t[0], reverse=True)

    lines, used = [], 0
    for _, c in scored[:max_items]:
        base = f"{rel_prefix}/{c['slug']}"
        how = (f"run `bash {base}/{c['script']}` (or python3 for .py; details in {base}/CAPABILITY.md)"
               if c["script"] else f"follow the steps in {base}/CAPABILITY.md")
        rec = f", worked {c['successes']}/{c['uses']}" if c["uses"] else ", untried"
        line = f"- {c['slug']} [{c['tier']}{rec}]: {c['title']}. Use when: {c['when_to_use']} How: {how}."
        if c["status"] == "needs_approval":
            line += f" NEEDS {ramu_profile.NAME.upper()}'S EXPLICIT YES in this conversation before every run -- describe it and wait."
        elif c["status"] == "broken":
            line += f" BROKEN (last note: {c['last_note'] or 'none'}) -- fix it, then learn it again."
        cost = rm.estimate_tokens(line)
        if used + cost > budget_tokens:
            continue
        lines.append(line)
        used += cost
    return "\n".join(lines)


def stats(cap_dir: str) -> dict:
    caps = load_all(cap_dir)
    by: dict[str, int] = {}
    for c in caps:
        by[c["status"]] = by.get(c["status"], 0) + 1
    return {"total": len(caps), "by_status": by}
