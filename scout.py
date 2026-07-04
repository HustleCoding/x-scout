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
CANDIDATES_LOG_PATH = ROOT / "candidates.jsonl"
UNSLOP_PATH = ROOT / "unslop.md"
JUDGE_PATH = ROOT / "judge.md"
MEMO_PATH = ROOT / "memo.md"

X_API = "https://api.x.com/2"
OPENROUTER_API = "https://openrouter.ai/api/v1/chat/completions"
MAX_CHARS = 280
HISTORY_CONTEXT = 20

FORMATS = [
    "a tiny story or specific moment from the real work listed",
    "a concrete number or before/after (only real, verifiable ones)",
    "an unpopular opinion stated plainly",
    "a question-shaped thought (not engagement bait)",
    "a plain observation with no twist",
    "a lesson learned the hard way",
    "a small confession or mistake",
    "a one-liner",
]

DEFAULT_CONFIG = {
    "model": "deepseek/deepseek-chat",
    "writer_model": "anthropic/claude-sonnet-4.5",
    "github_user": "HustleCoding",
    "examples": [],
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
    "candidates": 8,
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


def load_unslop() -> str:
    if not UNSLOP_PATH.exists():
        return ""
    text = UNSLOP_PATH.read_text()
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3 :]
    return text.strip()


def clean_text(text: str) -> str:
    text = text.strip().strip('"')
    for old, new in {"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"', "\u2014": ", ", "\u00a0": " "}.items():
        text = text.replace(old, new)
    return text[:MAX_CHARS]


def hn_front_page(limit: int = 8) -> str:
    try:
        ids = requests.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json", timeout=15
        ).json()[:limit]
        titles = []
        for i in ids:
            item = requests.get(
                f"https://hacker-news.firebaseio.com/v0/item/{i}.json", timeout=10
            ).json()
            title = (item or {}).get("title", "")
            if title:
                titles.append(title)
    except (requests.RequestException, ValueError):
        return ""
    return "\n".join(f"- {t}" for t in titles)


def github_activity(cfg: dict, limit: int = 12) -> str:
    user = cfg.get("github_user", "")
    if not user:
        return ""
    try:
        resp = requests.get(
            f"https://api.github.com/users/{user}/events/public",
            params={"per_page": 30},
            timeout=15,
        )
        resp.raise_for_status()
        events = resp.json()
    except requests.RequestException:
        return ""
    lines: list[str] = []
    for e in events:
        repo = e.get("repo", {}).get("name", "")
        payload = e.get("payload", {})
        if e.get("type") == "PushEvent":
            for c in payload.get("commits", [])[:3]:
                msg = c.get("message", "").splitlines()[0]
                if msg:
                    lines.append(f"commit to {repo}: {msg}")
        elif e.get("type") == "PullRequestEvent":
            title = payload.get("pull_request", {}).get("title", "")
            if title:
                lines.append(f"PR ({payload.get('action', '')}) in {repo}: {title}")
        elif e.get("type") == "CreateEvent" and payload.get("ref_type") == "repository":
            lines.append(f"created repo {repo}")
        if len(lines) >= limit:
            break
    return "\n".join(f"- {l}" for l in lines)


def log_candidates(winner: str, scored: list[dict]) -> None:
    entry = {
        "date": datetime.now(timezone.utc).isoformat(),
        "winner": winner,
        "candidates": [{"text": s["text"], "score": s["score"]} for s in scored],
    }
    with CANDIDATES_LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def load_history_entries() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    entries = []
    for line in HISTORY_PATH.read_text().strip().splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def save_history_entries(entries: list[dict]) -> None:
    HISTORY_PATH.write_text("".join(json.dumps(e) + "\n" for e in entries))


def engagement_score(m: dict) -> float:
    return (
        m.get("reply_count", 0) * 3.0
        + m.get("retweet_count", 0) * 2.0
        + m.get("quote_count", 0) * 1.5
        + m.get("like_count", 0) * 1.0
    )


def update_metrics(limit: int = 30) -> int:
    """Fetch public_metrics for recent posts and store them in posted.jsonl."""
    entries = load_history_entries()
    recent = [e for e in entries[-limit:] if e.get("tweet_id")]
    if not recent:
        print("metrics: no posts to update")
        return 0
    ids = ",".join(e["tweet_id"] for e in recent)
    resp = requests.get(
        f"{X_API}/tweets",
        params={"ids": ids, "tweet.fields": "public_metrics"},
        auth=x_auth(),
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"metrics: fetch failed ({resp.status_code}): {resp.text}")
        return 1
    by_id = {t["id"]: t.get("public_metrics", {}) for t in resp.json().get("data", [])}
    now = datetime.now(timezone.utc).isoformat()
    updated = 0
    for e in entries:
        m = by_id.get(e.get("tweet_id"))
        if m is not None:
            e["metrics"] = m
            e["metrics_at"] = now
            updated += 1
    save_history_entries(entries)
    print(f"metrics: updated {updated} posts")
    return 0


def performance_blocks(top_n: int = 3) -> str:
    """Prompt block describing top performers and flops among measured posts."""
    measured = [e for e in load_history_entries() if e.get("metrics")]
    if len(measured) < 4:
        return ""
    ranked = sorted(measured, key=lambda e: engagement_score(e["metrics"]), reverse=True)

    def fmt(e: dict) -> str:
        m = e["metrics"]
        return (
            f"- {e['text']} ({m.get('impression_count', 0)} views, "
            f"{m.get('like_count', 0)} likes, {m.get('reply_count', 0)} replies, "
            f"{m.get('retweet_count', 0)} reposts)"
        )

    tops = "\n".join(fmt(e) for e in ranked[:top_n])
    flops = "\n".join(fmt(e) for e in ranked[-top_n:])
    return (
        f"\nYour best performing posts (the audience wants more like these):\n{tops}\n"
        f"\nYour worst performing posts (avoid whatever these did):\n{flops}\n"
    )


def load_memo() -> str:
    if MEMO_PATH.exists():
        return MEMO_PATH.read_text().strip()
    return ""


def self_review(cfg: dict) -> int:
    """Write an editor's memo from recent performance and taste signals."""
    measured = [e for e in load_history_entries() if e.get("metrics")]
    if len(measured) < 4:
        print("self-review: not enough measured posts yet (need 4+)")
        return 0
    ranked = sorted(measured, key=lambda e: engagement_score(e["metrics"]), reverse=True)
    posts = "\n".join(
        f"- {e['text']} | {e['metrics']}" for e in ranked[-20:]
    )
    rejected = rejected_winners()
    rejected_block = "\n".join(f"- {r}" for r in rejected) or "(none)"
    old_memo = load_memo()
    prompt = (
        f"You are the editor for an X (twitter) account. Persona: {cfg['persona']}\n\n"
        f"Recent posts with engagement metrics:\n{posts}\n\n"
        f"Drafts the author rejected before posting:\n{rejected_block}\n\n"
        f"Previous memo (revise, don't repeat):\n{old_memo or '(none)'}\n\n"
        f"Write a short editor's memo (under 200 words) for the ghostwriter: "
        f"what is working with this audience, what is not, and 3-5 concrete "
        f"directives for future posts. Be specific to the data, not generic "
        f"social media advice. Plain markdown, no preamble."
    )
    memo = llm(cfg, prompt, max_tokens=500, temperature=0.4)
    MEMO_PATH.write_text(memo.strip() + "\n")
    print(f"self-review: wrote memo ({len(memo)} chars)")
    return 0


def rejected_winners(limit: int = 10) -> list[str]:
    """Winners from past runs that never made it into posted.jsonl."""
    if not CANDIDATES_LOG_PATH.exists():
        return []
    posted = set(load_history(limit=1000))
    today = datetime.now(timezone.utc).date().isoformat()
    rejected = []
    for line in CANDIDATES_LOG_PATH.read_text().strip().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        winner = entry.get("winner", "")
        if winner and winner not in posted and not entry.get("date", "").startswith(today):
            rejected.append(winner)
    return rejected[-limit:]


def llm(cfg: dict, prompt: str, max_tokens: int, temperature: float, model: str | None = None) -> str:
    resp = requests.post(
        OPENROUTER_API,
        headers={
            "Authorization": f"Bearer {env('OPENROUTER_API_KEY')}",
            "Content-Type": "application/json",
        },
        json={
            "model": model or cfg["model"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def parse_json_block(raw: str):
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1:
        raise SystemExit(f"could not parse JSON array from model output:\n{raw}")
    return json.loads(raw[start : end + 1])


def generate_candidates(cfg: dict, activity: str = "", news: str = "") -> list[str]:
    n = int(cfg.get("candidates", 8))
    topics = random.sample(cfg["topics"], min(3, len(cfg["topics"])))
    history = load_history()
    recent = "\n".join(f"- {t}" for t in history) if history else "(none yet)"
    unslop = load_unslop()
    style = (
        f"\n\nStyle guide (apply these rules to your writing, then self-audit: "
        f"'what makes this obviously AI generated?' and fix it):\n{unslop}\n"
        if unslop
        else ""
    )
    formats = random.sample(FORMATS, min(n, len(FORMATS)))
    format_lines = "\n".join(f"{i + 1}. {f}" for i, f in enumerate(formats))
    activity_block = (
        f"\nRecent real work (ground posts in these when it fits; a specific "
        f"detail from real work beats any generic take):\n{activity}\n"
        if activity
        else ""
    )
    news_block = (
        f"\nWhat devs are talking about today (hacker news front page; react "
        f"ONLY if you have a genuine take, never force it):\n{news}\n"
        if news
        else ""
    )
    examples = (cfg.get("examples") or [])[-10:]
    examples_block = (
        "\nPosts whose taste/rhythm to match (do not copy content):\n"
        + "\n".join(f"- {e}" for e in examples)
        + "\n"
        if examples
        else ""
    )
    rejected = rejected_winners()
    rejected_block = (
        "\nPast drafts the author REJECTED (wrong taste, do not write like these):\n"
        + "\n".join(f"- {r}" for r in rejected)
        + "\n"
        if rejected
        else ""
    )
    performance = performance_blocks()
    memo = load_memo()
    memo_block = f"\nEditor's memo (follow these directives):\n{memo}\n" if memo else ""
    prompt = (
        f"You write posts for X (twitter). Persona: {cfg['persona']}\n\n"
        f"Today's topics (pick per candidate): {', '.join(topics)}\n\n"
        f"Recent posts (do NOT repeat these ideas or phrasings):\n{recent}\n"
        f"{activity_block}{news_block}{examples_block}{rejected_block}{performance}{memo_block}{style}\n"
        f"Write {n} candidate posts, each under 260 characters. Use these "
        f"formats, one per candidate in order:\n{format_lines}\n\n"
        f"Each should feel like a real thought, not marketing copy. "
        f"No hashtags, no emojis. NEVER invent specific incidents, numbers, "
        f"user counts, or events that did not happen; concrete details must "
        f"come from the real work listed above or be clearly generic. "
        f"General observations and opinions are fine.\n"
        f'Reply with ONLY a JSON array of {n} strings, like ["post one", ...].'
    )
    raw = llm(cfg, prompt, max_tokens=1500, temperature=1.0, model=cfg.get("writer_model") or cfg["model"])
    posts = [clean_text(p) for p in parse_json_block(raw) if isinstance(p, str) and p.strip()]
    if not posts:
        raise SystemExit("generation returned no candidates")
    return posts


def judge_candidates(cfg: dict, candidates: list[str], activity: str = "") -> list[dict]:
    rubric = JUDGE_PATH.read_text() if JUDGE_PATH.exists() else ""
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(candidates))
    activity_block = (
        f"\nThe author's listed real work (specifics not traceable to these "
        f"are fabricated):\n{activity}\n"
        if activity
        else ""
    )
    prompt = (
        f"You judge draft posts for X (twitter) using this rubric:\n\n{rubric}\n\n"
        f"Persona the posts should fit: {cfg['persona']}\n{activity_block}\n"
        f"Candidates:\n{numbered}\n\n"
        f"Score each candidate 0-100 per the rubric. Be harsh on AI tells and "
        f"generic takes. Reply with ONLY a JSON array like "
        f'[{{"index": 0, "score": 55, "reason": "..."}}, ...] '
        f"covering every candidate, reasons under 15 words."
    )
    raw = llm(cfg, prompt, max_tokens=1200, temperature=0.2)
    scored = []
    for item in parse_json_block(raw):
        i = item.get("index")
        if isinstance(i, int) and 0 <= i < len(candidates):
            scored.append(
                {
                    "text": candidates[i],
                    "score": float(item.get("score", 0)),
                    "reason": str(item.get("reason", "")),
                }
            )
    if not scored:
        raise SystemExit("judge returned no usable scores")
    scored.sort(key=lambda s: s["score"], reverse=True)
    return scored


def generate_post(
    cfg: dict,
    report_path: str | None = None,
    log: bool = False,
    candidates_out: str | None = None,
) -> str:
    activity = github_activity(cfg)
    news = hn_front_page()
    candidates = generate_candidates(cfg, activity, news)
    scored = judge_candidates(cfg, candidates, activity)
    for s in scored[:3]:
        print(f"  [{s['score']:.0f}] {s['text']!r} ({s['reason']})")
    if candidates_out:
        Path(candidates_out).write_text(json.dumps(scored[:3], indent=2) + "\n")
    if report_path:
        lines = ["| score | candidate | judge notes |", "|---|---|---|"]
        for s in scored:
            lines.append(f"| {s['score']:.0f} | {s['text']} | {s['reason']} |")
        Path(report_path).write_text("\n".join(lines) + "\n")
    if log:
        log_candidates(scored[0]["text"], scored)
    return scored[0]["text"]


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
    parser.add_argument("--report", metavar="FILE", help="write a markdown table of all scored candidates to FILE")
    parser.add_argument("--update-metrics", action="store_true", help="fetch engagement metrics for recent posts, no post")
    parser.add_argument("--self-review", action="store_true", help="write an editor's memo from recent performance, no post")
    parser.add_argument("--candidates-out", metavar="FILE", help="write the top 3 scored candidates as JSON to FILE")
    args = parser.parse_args(argv)

    if args.verify:
        return verify_credentials()
    if args.update_metrics:
        return update_metrics()
    if args.self_review:
        return self_review(load_config())

    cfg = load_config()
    text = (
        args.message.strip()[:MAX_CHARS]
        if args.message
        else generate_post(
            cfg, args.report, log=bool(args.generate_only), candidates_out=args.candidates_out
        )
    )
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
