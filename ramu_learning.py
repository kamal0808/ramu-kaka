"""Daily learning digest: one thing worth learning today, not a news feed.

A news feed narrowed to ONE pick a day, with a why and a tiny try-this.
Sources and interests come from the profile (`learning_feeds`, `interests`).

Pure functions: parsing sources, building the pick prompt, reading the pick.
daemon.learning_loop fetches, calls Sonnet once, and sends.
"""
import json
import re
import xml.etree.ElementTree as ET

import ramu_profile

HN_URL = "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30"
FEEDS = ramu_profile.PROFILE.get("learning_feeds") or {
    "Wired (tech)": "https://www.wired.com/feed/rss",
    "ScienceDaily (science)": "https://www.sciencedaily.com/rss/top/science.xml",
}
INTERESTS = ramu_profile.PROFILE.get("interests") or ""


def parse_hn(data):
    out = []
    for h in (data or {}).get("hits") or []:
        title, url = h.get("title"), h.get("url") or (
            f"https://news.ycombinator.com/item?id={h.get('objectID')}" if h.get("objectID") else None)
        if title and url:
            out.append({"source": "Hacker News", "title": title[:160], "url": url,
                        "points": h.get("points") or 0})
    return out


def _text(el, *names):
    for n in names:
        found = el.find(n)
        if found is not None:
            if found.text and found.text.strip():
                return found.text.strip()
            if found.get("href"):
                return found.get("href")
    return ""


def parse_feed(source, xml_bytes, limit=8):
    """RSS 2.0 or Atom -> [{source, title, url, summary}]; [] on junk."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    atom = "{http://www.w3.org/2005/Atom}"
    items = root.findall(".//item") or root.findall(f".//{atom}entry")
    out = []
    for it in items[:limit]:
        title = _text(it, "title", f"{atom}title")
        url = _text(it, "link", f"{atom}link")
        summary = re.sub(r"<[^>]+>", " ", _text(it, "description", f"{atom}summary", f"{atom}content"))
        if title and url:
            out.append({"source": source, "title": title[:160], "url": url,
                        "summary": re.sub(r"\s+", " ", summary).strip()[:240]})
    return out


def prompt(candidates, recent_topics, goals_line=""):
    name = ramu_profile.NAME
    work = ramu_profile.PROFILE.get("work_context") or ""
    return (
        f"You pick ONE thing for {name} to learn today, from the candidates below. "
        f"Interests: {INTERESTS or 'not set; pick anything broadly useful'}\n"
        + (f"About {name}'s work (skip anything that is just their day job, not learning): {work}\n" if work else "")
        + (f"{name}'s current goals: {goals_line}\n" if goals_line else "")
        + f"Topics {name} got recently (pick something different): {', '.join(recent_topics) or 'none'}.\n"
        "Prefer something with a durable idea over breaking news; skip company drama, funding "
        "announcements and politics. Vary: not tech every day.\n"
        "Write the message in plain, warm, short English (no jargon, no hype):\n"
        "line 1: what it is, in one sentence;\n"
        f"line 2: why it matters to {name} (their interests, goals, life), one sentence;\n"
        f"line 3: 'Try:' one tiny action {name} can do today in under 10 minutes.\n"
        "Output ONLY JSON: {\"url\": \"<the candidate's url, exactly>\", \"topic\": \"<2-4 words>\", "
        "\"message\": \"<the 3 lines, separated by \\n>\"}\n\n"
        "Candidates:\n" + json.dumps(candidates, ensure_ascii=False)
    )


def parse_pick(out, candidates):
    """The model's pick, only if it points at a real candidate."""
    try:
        data = json.loads(out[out.index("{"):out.rindex("}") + 1])
    except (ValueError, AttributeError):
        return None
    urls = {c["url"]: c for c in candidates}
    if data.get("url") not in urls or not str(data.get("message") or "").strip():
        return None
    return {"url": data["url"], "topic": str(data.get("topic") or "")[:40],
            "message": str(data["message"]).strip()[:600], "source": urls[data["url"]]["source"]}


def fresh(candidates, sent_urls):
    seen, out = set(sent_urls or []), []
    for c in candidates:
        if c["url"] not in seen:
            seen.add(c["url"])
            out.append(c)
    return out


def format_message(pick):
    return f"📚 Today's one thing\n{pick['message']}\n{pick['url']}"
