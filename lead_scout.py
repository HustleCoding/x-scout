#!/usr/bin/env python3
"""Find people on X who need AI engineering help and send leads to Telegram.

Daily: search recent posts for client-need phrases (config lead_keywords),
drop spam and already-seen leads (leads.jsonl), draft a short pitch for each,
and send them to Telegram. Nothing is ever posted by the agent — you reply
from your own phone.

    python lead_scout.py --dry-run    # search + draft, print, no telegram
    python lead_scout.py              # full flow
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from scout import X_API, clean_text, llm, load_config
from reply_scout import app_bearer, traction
from tg_approve import api

ROOT = Path(__file__).resolve().parent
LEADS_PATH = ROOT / "leads.jsonl"
MAX_LEADS = 5

DEFAULT_KEYWORDS = [
    '"looking for an ai engineer"',
    '"need help building an ai"',
    '"hire an ai engineer"',
    '"need an ai developer"',
    '"build me an ai agent"',
]

SPAM_MARKERS = ("dm me", "apply here", "we're hiring", "job alert", "#hiring",
                "open to work", "job hunting", "freelancer", "portfolio")


def seen_ids() -> set[str]:
    if not LEADS_PATH.exists():
        return set()
    ids = set()
    for line in LEADS_PATH.read_text().strip().splitlines():
        try:
            ids.add(json.loads(line)["id"])
        except (json.JSONDecodeError, KeyError):
            continue
    return ids


def log_lead(lead: dict, pitch: str) -> None:
    entry = {
        "date": datetime.now(timezone.utc).isoformat(),
        "id": lead["id"],
        "author_id": lead.get("author_id", ""),
        "text": lead["text"],
        "pitch": pitch,
    }
    with LEADS_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def search_leads(cfg: dict, limit: int = MAX_LEADS) -> list[dict]:
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
        raise SystemExit(f"lead search failed ({resp.status_code}): {resp.text}")
    tweets = resp.json().get("data", [])
    skip = seen_ids()
    leads = []
    for t in sorted(tweets, key=lambda t: traction(t.get("public_metrics", {})), reverse=True):
        text = t.get("text", "").lower()
        if t["id"] in skip or any(m in text for m in SPAM_MARKERS):
            continue
        if not is_client_lead(cfg, t["text"]):
            continue
        leads.append(t)
        if len(leads) >= limit:
            break
    return leads


def is_client_lead(cfg: dict, text: str) -> bool:
    """True if the author is looking to get something built (a potential client),
    not a developer advertising their own services."""
    verdict = llm(
        cfg,
        "Someone posted this on X:\n"
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
        f"Someone posted on X that they need software built:\n{lead_text}\n\n"
        f"Draft a SHORT reply (under 200 characters) they'd want to answer: "
        f"address their specific need, offer one concrete first step or "
        f"question, no generic sales talk, no links, no hashtags, no emojis. "
        f"NEVER invent past projects or numbers.\n"
        f"Reply with ONLY the reply text."
    )
    return clean_text(llm(cfg, prompt, max_tokens=150, temperature=0.8,
                          model=cfg.get("writer_model") or cfg["model"]))


def send_lead(token: str, chat_id: str, lead: dict, pitch: str) -> None:
    m = lead.get("public_metrics", {})
    text = (
        f"lead ({m.get('like_count', 0)} likes, {m.get('reply_count', 0)} replies):\n\n"
        f"{lead['text']}\n"
        f"https://x.com/i/status/{lead['id']}\n\n"
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
        print(f"lead https://x.com/i/status/{lead['id']}: {lead['text'][:80]!r}")
        print(f"  pitch: {pitch!r}")
        if args.dry_run:
            continue
        send_lead(token, chat_id, lead, pitch)
        log_lead(lead, pitch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
