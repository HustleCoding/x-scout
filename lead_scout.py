#!/usr/bin/env python3
"""Find operators with stuck workflows and send leads to Telegram.

Daily: search configured sources for operators with manual or AI-heavy
workflows, drop spam and already-seen leads (leads.jsonl), draft a diagnostic
reply for each, and send them to Telegram.
Nothing is ever posted by the agent — you reply from your own account.

    python lead_scout.py --dry-run    # search + draft, print, no telegram
    python lead_scout.py              # full flow
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests

from scout import X_API, clean_text, llm, load_config
from reply_scout import app_bearer, traction
from tg_approve import api

ROOT = Path(__file__).resolve().parent
LEADS_PATH = ROOT / "leads.jsonl"
MAX_LEADS = 5
AUTHOR_COOLDOWN_DAYS = 30

DEFAULT_KEYWORDS = [
    '"we do this manually"',
    '"manual process" automation',
    '"want to add an AI feature"',
    '"workflow bottleneck"',
    '"stuck" "AI" workflow',
]

SPAM_MARKERS = ("dm me", "apply here", "we're hiring", "job alert", "#hiring",
                "open to work", "job hunting", "freelancer", "portfolio",
                "[for hire]", "for hire]")

DEFAULT_SUBREDDITS = ["SaaS", "microsaas", "startups", "Entrepreneur", "smallbusiness", "automate"]

DEFAULT_LEAD_SOURCES = ("x", "reddit", "hn")

DEFAULT_REDDIT_NEED_MARKERS = (
    "manual", "manually", "automation", "automate", "ai feature",
    "ai workflow", "repetitive", "bottleneck", "stuck", "looking for help",
    "need help", "need someone",
)

DEFAULT_HN_QUERIES = (
    '"we do this manually"', '"manual process"', '"how do you automate"',
    '"want to add an AI feature"', '"workflow automation"', '"internal tool"',
)

DEFAULT_BUYER_CONTEXT = (
    "The author already has a live product, business, users, customers, "
    "revenue, or an existing internal process. They have a manual, repetitive, "
    "AI-heavy, or automation opportunity inside that live operation, and the "
    "problem is costly, slow, or becoming urgent. Do not qualify idea-stage "
    "projects with nothing built, people shopping for the cheapest MVP, "
    "developers or agencies advertising services, recruiters posting jobs, "
    "equity-only or barter offers, or general opinion posts."
)

FORBIDDEN_DRAFT_PATTERNS = (
    "i can build", "we can build", "happy to build", "let me build",
    "hire me", "dm me", "book a call", "scope day", "founding sprint",
    "http://", "https://", "www.",
)

UA = {"User-Agent": "linux:x-scout-leads:v1.0 (by u/hustlecoding)"}


def seen_ids() -> tuple[set[str], set[str]]:
    """Return (lead ids ever pitched, authors pitched within the cooldown)."""
    if not LEADS_PATH.exists():
        return set(), set()
    cutoff = datetime.now(timezone.utc).timestamp() - AUTHOR_COOLDOWN_DAYS * 86400
    ids, authors = set(), set()
    for line in LEADS_PATH.read_text().strip().splitlines():
        try:
            entry = json.loads(line)
            lid = entry["id"]
        except (json.JSONDecodeError, KeyError):
            continue
        ids.add(lid)
        if lid.isdigit():  # legacy x-only entries lacked the source prefix
            ids.add(f"x:{lid}")
        author = entry.get("author") or entry.get("author_id") or ""
        try:
            recent = datetime.fromisoformat(entry["date"]).timestamp() >= cutoff
        except (KeyError, ValueError):
            recent = False
        if author and recent:
            authors.add(author)
    return ids, authors


def log_lead(lead: dict, pitch: str) -> None:
    entry = {
        "date": datetime.now(timezone.utc).isoformat(),
        "id": lead["id"],
        "source": lead["source"],
        "url": lead["url"],
        "author": lead.get("author", ""),
        "text": lead["text"],
        "pitch": pitch,
    }
    with LEADS_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def search_x(cfg: dict) -> list[dict]:
    try:
        keywords = cfg.get("lead_keywords") or DEFAULT_KEYWORDS
        query = "(" + " OR ".join(keywords) + ") -is:retweet -is:reply -is:quote lang:en"
        resp = requests.get(
            f"{X_API}/tweets/search/recent",
            params={
                "query": query,
                "max_results": 25,
                "tweet.fields": "public_metrics,author_id,created_at",
            },
            headers={"Authorization": f"Bearer {app_bearer()}"},
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"leads: x search failed ({resp.status_code}): {resp.text}")
            return []
        tweets = resp.json().get("data", [])
        tweets.sort(key=lambda t: traction(t.get("public_metrics", {})), reverse=True)
        return [
            {
                "id": f"x:{t['id']}",
                "source": "x",
                "url": f"https://x.com/i/status/{t['id']}",
                "text": t["text"],
                "author": t.get("author_id", ""),
            }
            for t in tweets
        ]
    except (Exception, SystemExit) as e:
        print(f"leads: x search unavailable ({e}); continuing without x")
        return []


def search_reddit(cfg: dict) -> list[dict]:
    leads = []
    markers = tuple(
        marker.lower()
        for marker in (cfg.get("reddit_need_markers") or DEFAULT_REDDIT_NEED_MARKERS)
    )
    for i, sub in enumerate(cfg.get("lead_subreddits") or DEFAULT_SUBREDDITS):
        if i:
            time.sleep(10)  # reddit rate-limits unauthenticated clients hard
        root = None
        error = ""
        for attempt in range(3):
            try:
                resp = requests.get(
                    f"https://www.reddit.com/r/{sub}/new.rss?limit=25",
                    headers=UA, timeout=30,
                )
                if resp.status_code == 429:
                    error = "HTTP 429"
                    time.sleep(15 * (attempt + 1))
                    continue
                resp.raise_for_status()
                root = ET.fromstring(resp.content)
                break
            except (requests.RequestException, ET.ParseError) as e:
                error = str(e)
                print(f"leads: reddit r/{sub} failed: {e}")
                break
        if root is None:
            print(f"leads: reddit r/{sub} unavailable ({error or 'unknown error'})")
            continue
        ns = {"a": "http://www.w3.org/2005/Atom"}
        posts_seen = 0
        matches = 0
        for entry in root.findall("a:entry", ns):
            posts_seen += 1
            title = entry.findtext("a:title", "", ns)
            link = entry.find("a:link", ns)
            url = link.get("href") if link is not None else ""
            eid = entry.findtext("a:id", url, ns)
            body = html.unescape(re.sub(r"<[^>]+>", " ", entry.findtext("a:content", "", ns)))
            body = re.sub(r"\s+", " ", body).replace("[link] [comments]", "").strip()
            if not any(marker in title.lower() for marker in markers):
                continue
            matches += 1
            leads.append({
                "id": f"reddit:{eid}",
                "source": f"reddit r/{sub}",
                "url": url,
                "text": f"{title}\n{body[:400]}",
                "author": entry.findtext("a:author/a:name", "", ns),
            })
        print(f"leads: reddit r/{sub}: {posts_seen} posts, {matches} title matches")
    return leads


def search_hn(cfg: dict) -> list[dict]:
    since = int(time.time()) - 2 * 86400
    leads = []
    queries = cfg.get("hn_queries") or DEFAULT_HN_QUERIES
    for q in queries:
        try:
            resp = requests.get(
                "https://hn.algolia.com/api/v1/search_by_date",
                params={"query": q, "tags": "(story,comment)",
                        "numericFilters": f"created_at_i>{since}"},
                timeout=30,
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", [])
        except (requests.RequestException, ValueError) as e:
            print(f"leads: hn search failed: {e}")
            continue
        print(f"leads: hn query {q!r}: {len(hits)} hits")
        for h in hits:
            text = h.get("title") or ""
            comment = re.sub(r"<[^>]+>", " ", h.get("comment_text") or "")
            text = html.unescape(f"{text}\n{comment}".strip())[:500]
            hid = h["objectID"]
            leads.append({
                "id": f"hn:{hid}",
                "source": "hn",
                "url": f"https://news.ycombinator.com/item?id={hid}",
                "text": text,
                "author": h.get("author", ""),
            })
    return leads


def search_leads(cfg: dict, limit: int = MAX_LEADS) -> list[dict]:
    sources = [str(source).lower() for source in
               (cfg.get("lead_sources") or DEFAULT_LEAD_SOURCES)]
    candidates = []
    source_counts = {}
    for source in sources:
        if source == "x":
            found = search_x(cfg)
        elif source == "reddit":
            found = search_reddit(cfg)
        elif source in ("hn", "hackernews", "hacker-news"):
            found = search_hn(cfg)
        else:
            print(f"leads: unknown source {source!r}, skipping")
            continue
        source_counts[source] = len(found)
        candidates.extend(found)
    summary = ", ".join(f"{source}={count}" for source, count in source_counts.items())
    print(f"leads: sources returned candidates after title/query pre-filter: {summary or 'none'}")
    print(f"leads: {len(candidates)} candidates reached the qualifier")
    skip, skip_authors = seen_ids()
    leads, seen_now, seen_authors = [], set(), set()
    for c in candidates:
        text = c["text"].lower()
        if c["id"] in skip or c["id"] in seen_now or any(m in text for m in SPAM_MARKERS):
            continue
        author = c.get("author", "")
        if author and (author in skip_authors or author in seen_authors):
            continue
        qualified = is_client_lead(cfg, c["text"])
        print(f"leads: qualifier {'yes' if qualified else 'no'} [{c['source']}] {c['url']}")
        if not qualified:
            continue
        seen_now.add(c["id"])
        if author:
            seen_authors.add(author)
        leads.append(c)
        if len(leads) >= limit:
            break
    return leads


def is_client_lead(cfg: dict, text: str) -> bool:
    """Return whether a post matches the configured live-product buyer."""
    buyer_context = cfg.get("lead_buyer_context") or DEFAULT_BUYER_CONTEXT
    verdict = llm(
        cfg,
        "Someone posted this online:\n"
        f"{text}\n\n"
        f"Buyer qualification context:\n{buyer_context}\n\n"
        "Answer yes only when the author is plausibly this buyer and the post "
        "describes a concrete workflow, automation, or AI feature problem. "
        "Answer no when the evidence is missing or the post is mainly a job ad, "
        "service advertisement, or general discussion.\n"
        "Answer with ONLY yes or no.",
        max_tokens=5,
        temperature=0.0,
    )
    return verdict.strip().lower().startswith("y")


def draft_pitch(cfg: dict, lead_text: str) -> str:
    product = cfg.get(
        "lead_pitch_context",
        "an independent engineer who ships manual or AI-heavy workflows inside live products",
    )
    prompt = (
        f"Persona: {cfg['persona']}\n"
        f"Background context (do not turn this into a pitch): {product}\n\n"
        f"Someone posted online about an existing workflow:\n{lead_text}\n\n"
        "Draft a lowercase reply of up to 280 characters with exactly two "
        "sentences: first, one specific observation that names or paraphrases "
        "a detail from their post, never generic advice; second, one diagnostic "
        "question about why this workflow matters now (what changed, the urgency, "
        "the volume, or the consequence). Make the question answerable and "
        "concrete, not 'want help?'.\n"
        "This is a diagnosis, not a pitch. Do not offer to build anything, "
        "mention prices or an engagement, suggest a call or DM, include links, "
        "hashtags, emojis, or generic sales language. Do not mention your "
        "background or proof unless the post explicitly asks. NEVER invent past "
        "projects or numbers. Reply with ONLY the reply text."
    )
    for attempt in range(2):
        draft = clean_text(llm(
            cfg, prompt, max_tokens=180, temperature=0.8,
            model=cfg.get("writer_model") or cfg["model"],
        ))
        lower = draft.lower()
        punctuation = re.findall(r"[.!?]", draft)
        has_forbidden = (
            any(pattern in lower for pattern in FORBIDDEN_DRAFT_PATTERNS)
            or "#" in draft
            or re.search(r"[$€£]\s*\d|\b(?:\d{3,}|five hundred|three thousand)\b", lower)
        )
        asks_why_now = (
            "why" in lower
            or "what changed" in lower
            or any(term in lower for term in ("today", "now", "urgent", "volume", "cost"))
        )
        if (
            draft.endswith("?")
            and punctuation == ["?"]
            and asks_why_now
            and not has_forbidden
        ):
            return draft
        prompt += (
            "\nYour previous draft broke a rule. Rewrite it as exactly two "
            "sentences: a specific observation from the post, then one question "
            "about why this workflow matters now. Use one final question mark "
            "only, with no offer, price, link, hashtag, emoji, or sales language."
        )
    return draft


def send_lead(token: str, chat_id: str, lead: dict, pitch: str) -> None:
    text = (
        f"lead ({lead['source']}):\n\n"
        f"{lead['text']}\n"
        f"{lead['url']}\n\n"
        f"suggested reply (paste/adapt from your phone):\n{pitch}"
    )
    api(token, "sendMessage", chat_id=chat_id, text=text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="search and draft only, no telegram")
    args = parser.parse_args(argv)

    cfg = load_config()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not args.dry_run and (not token or not chat_id):
        print("leads: telegram not configured, skipping")
        return 0

    leads = search_leads(cfg)
    if not leads:
        print("leads: none found today")
        return 0

    for lead in leads:
        pitch = draft_pitch(cfg, lead["text"])
        print(f"lead [{lead['source']}] {lead['url']}: {lead['text'][:80]!r}")
        print(f"  drafted reply: {pitch!r}")
        if args.dry_run:
            continue
        send_lead(token, chat_id, lead, pitch)
        log_lead(lead, pitch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
