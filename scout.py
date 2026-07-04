#!/usr/bin/env python3
"""Generate a short post with an LLM and publish it via the X API v2.

No browser involved. Credentials come from environment variables:

    X_API_KEY              consumer key
    X_API_SECRET           consumer secret
    X_ACCESS_TOKEN         user access token
    X_ACCESS_TOKEN_SECRET  user access token secret
    OPENROUTER_API_KEY     for LLM content generation (not needed with -m)

Usage:

    python scout.py --verify           # check X credentials, no post
    python scout.py --dry-run          # generate a post, print it, do not publish
    python scout.py --generate-only post.txt   # generate and save for later approval
    python scout.py                    # generate and publish
    python scout.py -m "hello"         # publish a specific message
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests_oauthlib import OAuth1

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
HISTORY_PATH = ROOT / "posted.jsonl"

X_API = "https://api.x.com/2"
OPENROUTER_API = "https://openrouter.ai/api/v1/chat/completions"
MAX_CHARS = 280
HISTORY_CONTEXT = 20

DEFAULT_CONFIG = {
    "model": "deepseek/deepseek-chat",
    "persona": (
        "An indie hacker and AI engineer who ships small products fast. "
        "Curious, direct, a little dry. Writes in lowercase, no hashtags, "
        "no emojis, no links."
    ),
    "topics": [
        "agentic AI in real workflows",
        "shipping small products fast",
        "what LLMs are actually good at",
        "developer tools that feel magical",
        "building in public",
    ],
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text())
        return {**DEFAULT_CONFIG, **cfg}
    return dict(DEFAULT_CONFIG)


def load_history(limit: int = HISTORY_CONTEXT) -> list[str]:
    if not HISTORY_PATH.exists():
        return []
    lines = HISTORY_PATH.read_text().strip().splitlines()
    texts = []
    for line in lines[-limit:]:
        try:
            texts.append(json.loads(line)["text"])
        except (json.JSONDecodeError, KeyError):
            continue
    return texts


def append_history(text: str, tweet_id: str) -> None:
    entry = {
        "text": text,
        "tweet_id": tweet_id,
        "posted_at": datetime.now(timezone.utc).isoformat(),
    }
    with HISTORY_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing environment variable {name}")
    return value


def x_auth() -> OAuth1:
    return OAuth1(
        env("X_API_KEY"),
        env("X_API_SECRET"),
        env("X_ACCESS_TOKEN"),
        env("X_ACCESS_TOKEN_SECRET"),
    )


def verify_credentials() -> int:
    resp = requests.get(f"{X_API}/users/me", auth=x_auth(), timeout=30)
    if resp.status_code != 200:
        print(f"verify: FAILED ({resp.status_code}) {resp.text}")
        return 1
    data = resp.json().get("data", {})
    print(f"verify: OK, authenticated as @{data.get('username')} ({data.get('name')})")
    return 0


def generate_post(cfg: dict) -> str:
    topic = random.choice(cfg["topics"])
    history = load_history()
    recent = "\n".join(f"- {t}" for t in history) if history else "(none yet)"
    prompt = (
        f"You write posts for X (twitter). Persona: {cfg['persona']}\n\n"
        f"Topic for today: {topic}\n\n"
        f"Recent posts (do NOT repeat these ideas or phrasings):\n{recent}\n\n"
        f"Write ONE post under 260 characters. It should feel like a real "
        f"thought, not marketing copy. No hashtags, no emojis, no quotes "
        f"around it. Reply with the post text only."
    )
    resp = requests.post(
        OPENROUTER_API,
        headers={
            "Authorization": f"Bearer {env('OPENROUTER_API_KEY')}",
            "Content-Type": "application/json",
        },
        json={
            "model": cfg["model"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 200,
            "temperature": 0.9,
        },
        timeout=60,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"].strip().strip('"')
    if not text:
        raise SystemExit("generation returned an empty post")
    return text[:MAX_CHARS]


def publish(text: str) -> str:
    resp = requests.post(
        f"{X_API}/tweets", json={"text": text}, auth=x_auth(), timeout=30
    )
    if resp.status_code not in (200, 201):
        raise SystemExit(f"post failed ({resp.status_code}): {resp.text}")
    return resp.json()["data"]["id"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-m", "--message", help="post this exact text instead of generating one")
    parser.add_argument("--dry-run", action="store_true", help="generate and print, do not publish")
    parser.add_argument("--generate-only", metavar="FILE", help="generate a post and write it to FILE, do not publish")
    parser.add_argument("--verify", action="store_true", help="check X API credentials, no post")
    args = parser.parse_args(argv)

    if args.verify:
        return verify_credentials()

    cfg = load_config()
    text = args.message.strip()[:MAX_CHARS] if args.message else generate_post(cfg)
    print(f"post: {text!r} ({len(text)} chars)")
    if args.generate_only:
        Path(args.generate_only).write_text(text + "\n")
        print(f"generate-only: wrote post to {args.generate_only}")
        return 0
    if args.dry_run:
        print("dry-run: not publishing")
        return 0
    tweet_id = publish(text)
    append_history(text, tweet_id)
    print(f"published: https://x.com/i/status/{tweet_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
