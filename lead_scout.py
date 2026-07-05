#!/usr/bin/env python3
"""Find people who need software built and send leads to Telegram.

Daily: search X (config lead_keywords), Reddit (config lead_subreddits), and
Hacker News (Algolia) for client-need posts, drop spam and already-seen leads
(leads.jsonl), draft a short pitch for each, and send them to Telegram.
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
    '"looking for an ai engineer"',
    '"need help building an ai"',
    '"hire an ai engineer"',
    '"need an ai developer"',
    '"build me an ai agent"',
]

SPAM_MARKERS = ("dm me", "apply here", "we're hiring", "job alert", "#hiring",
                "open to work", "job hunting", "freelancer", "portfolio",
                "[for hire]", "for hire]")

DEFAULT_SUBREDDITS = ["forhire", "startups", "SaaS", "Entrepreneur", "nocode"]

REDDIT_NEED_MARKERS = ("[hiring]", "looking for", "need a dev", "need someone",
                       "need help build", "who can build", "build my",
                       "technical cofounder", "technical co-founder", "mvp")

HN_QUERIES = ('"looking for a developer"', '"need a developer"',
              '"need someone to build"', '"technical cofounder"')

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


def search_reddit(cfg: dict) -> list[dict]:
    leads = []
    for i, sub in enumerate(cfg.get("lead_subreddits") or DEFAULT_SUBREDDITS):
        if i:
            time.sleep(10)  # reddit rate-limits unauthenticated clients hard
        root = None
        for attempt in range(3):
            try:
                resp = requests.get(
                    f"https://www.reddit.com/r/{sub}/new.rss?limit=25",
                    headers=UA, timeout=30,
                )
                if resp.status_code == 429:
                    time.sleep(15 * (attempt + 1))
                    continue
                resp.raise_for_status()
                root = ET.fromstring(resp.content)
                break
            except (requests.RequestException, ET.ParseError) as e:
                print(f"leads: reddit r/{sub} failed: {e}")
                break
        if root is None:
            print(f"leads: reddit r/{sub} unavailable (rate limited)")
            continue
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("a:entry", ns):
            title = entry.findtext("a:title", "", ns)
            link = entry.find("a:link", ns)
            url = link.get("href") if link is not None else ""
            eid = entry.findtext("a:id", url, ns)
            body = html.unescape(re.sub(r"<[^>]+>", " ", entry.findtext("a:content", "", ns)))
            body = re.sub(r"\s+", " ", body).replace("[link] [comments]", "").strip()
            if not any(m in title.lower() for m in REDDIT_NEED_MARKERS):
                continue
            leads.append({
                "id": f"reddit:{eid}",
                "source": f"reddit r/{sub}",
                "url": url,
                "text": f"{title}\n{body[:400]}",
                "author": entry.findtext("a:author/a:name", "", ns),
            })
    return leads


def search_hn(cfg: dict) -> list[dict]:
    since = int(time.time()) - 2 * 86400
    leads = []
    for q in HN_QUERIES:
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
    candidates = search_x(cfg) + search_reddit(cfg) + search_hn(cfg)
    skip, skip_authors = seen_ids()
    leads, seen_now, seen_authors = [], set(), set()
    for c in candidates:
        text = c["text"].lower()
        if c["id"] in skip or c["id"] in seen_now or any(m in text for m in SPAM_MARKERS):
            continue
        author = c.get("author", "")
        if author and (author in skip_authors or author in seen_authors):
            continue
        if not is_client_lead(cfg, c["text"]):
            continue
        seen_now.add(c["id"])
        if author:
            seen_authors.add(author)
        leads.append(c)
        if len(leads) >= limit:
            break
    return leads


def is_client_lead(cfg: dict, text: str) -> bool:
    """True if the author is looking to get something built (a potential client),
    not a developer advertising their own services."""
    verdict = llm(
        cfg,
        "Someone posted this online:\n"
        f"{text}\n\n"
        "Does the author want someone to build software FOR THEM (a potential "
        "client for a freelance developer)? Answer yes if they are asking for a "
        "developer, an app, an MVP, or a technical cofounder — even informally or "
        "offering barter. Answer no ONLY if the author is themselves a "
        "developer/agency advertising their services, sharing an opinion, or a "
        "recruiter posting a company job ad.\n"
        "Answer with ONLY yes or no.",
        max_tokens=5,
        temperature=0.0,
    )
    return verdict.strip().lower().startswith("y")


def draft_pitch(cfg: dict, lead_text: str) -> str:
    product = cfg.get("lead_pitch_context", "an independent AI engineer who ships fast")
    prompt = (
        f"Persona: {cfg['persona']}\n"
        f"You are {product}.\n\n"
        f"Someone posted online that they need software built:\n{lead_text}\n\n"
        f"Draft a SHORT reply (under 200 characters) they'd want to answer: "
        f"address their specific need, offer one concrete first step or "
        f"question, no generic sales talk, no links, no hashtags, no emojis. "
        f"NEVER invent past projects or numbers.\n"
        f"Reply with ONLY the reply text."
    )
    return clean_text(llm(cfg, prompt, max_tokens=150, temperature=0.8,
                          model=cfg.get("writer_model") or cfg["model"]))


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
        print(f"  pitch: {pitch!r}")
        if args.dry_run:
            continue
        send_lead(token, chat_id, lead, pitch)
        log_lead(lead, pitch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
