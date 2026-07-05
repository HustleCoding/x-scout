#!/usr/bin/env python3
"""Find high-traction posts on X in our topics and quote them with approval.

Daily: search recent posts for the configured keywords, rank by traction,
draft a quote post for the top targets, ask on Telegram per quote, publish
the approved ones. Every quote is logged to replied.jsonl and an author is
never quoted twice within a week. (Quote posts, not replies: X's API blocks
replying to conversations the account hasn't been engaged in.)

    python reply_scout.py --dry-run    # search + draft, print, no telegram/publish
    python reply_scout.py              # full flow
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from scout import MAX_CHARS, X_API, clean_text, env, llm, load_config, load_unslop, x_auth
from tg_approve import api

ROOT = Path(__file__).resolve().parent
REPLIED_PATH = ROOT / "replied.jsonl"
MAX_TARGETS = 2
AUTHOR_COOLDOWN_DAYS = 7
POLL_SECONDS = 25
DEFAULT_TIMEOUT_MINUTES = 45

DEFAULT_KEYWORDS = [
    "ai agents",
    "building in public",
    "indie hacker",
    "llm coding",
]


def recent_authors(days: int = AUTHOR_COOLDOWN_DAYS) -> set[str]:
    if not REPLIED_PATH.exists():
        return set()
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    authors = set()
    for line in REPLIED_PATH.read_text().strip().splitlines():
        try:
            entry = json.loads(line)
            ts = datetime.fromisoformat(entry["date"]).timestamp()
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        if ts >= cutoff:
            authors.add(entry.get("author_id", ""))
    return authors


def log_quote(target: dict, quote_text: str, quote_id: str) -> None:
    entry = {
        "date": datetime.now(timezone.utc).isoformat(),
        "target_id": target["id"],
        "author_id": target["author_id"],
        "target_text": target["text"],
        "quote": quote_text,
        "quote_id": quote_id,
    }
    with REPLIED_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def traction(m: dict) -> float:
    return m.get("like_count", 0) + 2 * m.get("retweet_count", 0) + 3 * m.get("reply_count", 0)


def app_bearer() -> str:
    """App-only bearer token; the search endpoint rejects OAuth1 user context."""
    resp = requests.post(
        "https://api.x.com/oauth2/token",
        data={"grant_type": "client_credentials"},
        auth=(env("X_API_KEY"), env("X_API_SECRET")),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def search_targets(cfg: dict, limit: int = MAX_TARGETS) -> list[dict]:
    keywords = cfg.get("reply_keywords") or DEFAULT_KEYWORDS
    query = "(" + " OR ".join(f'"{k}"' for k in keywords) + ") -is:retweet -is:reply -is:quote lang:en"
    resp = requests.get(
        f"{X_API}/tweets/search/recent",
        params={
            "query": query,
            "max_results": 25,
            "tweet.fields": "public_metrics,author_id,created_at,reply_settings",
        },
        headers={"Authorization": f"Bearer {app_bearer()}"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise SystemExit(f"search failed ({resp.status_code}): {resp.text}")
    tweets = resp.json().get("data", [])
    skip_authors = recent_authors()
    me = requests.get(f"{X_API}/users/me", auth=x_auth(), timeout=30).json().get("data", {}).get("id", "")
    seen_authors = set()
    ranked = sorted(tweets, key=lambda t: traction(t.get("public_metrics", {})), reverse=True)
    targets = []
    for t in ranked:
        author = t.get("author_id", "")
        if author in skip_authors or author in seen_authors or author == me:
            continue
        if traction(t.get("public_metrics", {})) < 5:
            continue
        if t.get("reply_settings", "everyone") != "everyone":
            continue
        seen_authors.add(author)
        targets.append(t)
        if len(targets) >= limit:
            break
    return targets


def draft_quote(cfg: dict, target_text: str) -> str:
    unslop = load_unslop()
    style = f"\n\nStyle guide:\n{unslop}\n" if unslop else ""
    prompt = (
        f"You write quote posts on X (twitter): your commentary shown above "
        f"someone else's post. Persona: {cfg['persona']}\n\n"
        f"Someone posted:\n{target_text}\n\n"
        f"Write ONE quote-post comment under 260 characters that adds something real: a "
        f"specific experience, a sharp question, or a genuinely different "
        f"angle. Never flatter, never summarize their post back, never pitch "
        f"anything. NEVER invent specific incidents, numbers, or events that "
        f"did not happen; opinions and questions are safer than fake stories. "
        f"If you have nothing real to add, still give your best "
        f"attempt. Do not address the author directly; you're commenting to "
        f"your own audience. No hashtags, no emojis.{style}\n"
        f"Reply with ONLY the comment text, no quotation marks."
    )
    return clean_text(llm(cfg, prompt, max_tokens=200, temperature=0.9,
                          model=cfg.get("writer_model") or cfg["model"]))


def publish_quote(text: str, quoted_id: str) -> str | None:
    resp = requests.post(
        f"{X_API}/tweets",
        json={"text": text, "quote_tweet_id": quoted_id},
        auth=x_auth(),
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        print(f"  quote failed ({resp.status_code}): {resp.text}")
        return None
    return resp.json()["data"]["id"]


def approve_on_telegram(token: str, chat_id: str, target: dict, quote_text: str,
                        timeout_minutes: int) -> bool:
    link = f"https://x.com/i/status/{target['id']}"
    m = target.get("public_metrics", {})
    text = (
        f"quote opportunity ({m.get('like_count', 0)} likes, "
        f"{m.get('reply_count', 0)} replies):\n\n"
        f"{target['text']}\n{link}\n\n"
        f"drafted quote post:\n{quote_text}"
    )
    sent = api(token, "sendMessage", chat_id=chat_id, text=text,
               reply_markup={"inline_keyboard": [[
                   {"text": "post quote", "callback_data": "reply:yes"},
                   {"text": "skip", "callback_data": "reply:no"},
               ]]})["result"]
    message_id = sent["message_id"]
    deadline = time.time() + timeout_minutes * 60
    offset = None
    while time.time() < deadline:
        params = {"timeout": POLL_SECONDS, "allowed_updates": ["callback_query"]}
        if offset is not None:
            params["offset"] = offset
        for u in api(token, "getUpdates", **params)["result"]:
            offset = u["update_id"] + 1
            cq = u.get("callback_query")
            if not cq or cq.get("message", {}).get("message_id") != message_id:
                continue
            api(token, "answerCallbackQuery", callback_query_id=cq["id"])
            approved = cq.get("data") == "reply:yes"
            api(token, "editMessageText", chat_id=chat_id, message_id=message_id,
                text=text + ("\n\n-> posting" if approved else "\n\n-> skipped"))
            return approved
    api(token, "editMessageText", chat_id=chat_id, message_id=message_id,
        text=text + "\n\n-> timed out, skipped")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="search and draft only, no telegram or publish")
    parser.add_argument("--timeout-minutes", type=int, default=DEFAULT_TIMEOUT_MINUTES)
    args = parser.parse_args(argv)

    cfg = load_config()
    targets = search_targets(cfg)
    if not targets:
        print("quotes: no suitable targets today")
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not args.dry_run and (not token or not chat_id):
        print("quotes: telegram not configured, skipping (quotes always need approval)")
        return 0

    for target in targets:
        quote_text = draft_quote(cfg, target["text"])[:MAX_CHARS]
        print(f"target https://x.com/i/status/{target['id']}: {target['text'][:80]!r}")
        print(f"  draft: {quote_text!r}")
        if args.dry_run:
            continue
        if approve_on_telegram(token, chat_id, target, quote_text, args.timeout_minutes):
            quote_id = publish_quote(quote_text, target["id"])
            if quote_id:
                log_quote(target, quote_text, quote_id)
                print(f"  published: https://x.com/i/status/{quote_id}")
        else:
            print("  skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
