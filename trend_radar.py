#!/usr/bin/env python3
"""Find timely topics worth posting about and send them to Telegram.

    python trend_radar.py --signals-only
    python trend_radar.py --dry-run
    python trend_radar.py --no-telegram
    python trend_radar.py
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests_oauthlib import OAuth1

from scout import (
    X_API,
    github_activity,
    llm,
    load_config,
    load_history_entries,
    load_unslop,
    parse_json_block,
)
from tg_approve import api

ROOT = Path(__file__).resolve().parent
TOPICS_PATH = ROOT / "topics.json"
TOPICS_LOG_PATH = ROOT / "topics.jsonl"
REPORT_PATH = ROOT / "topics.md"
IDEAS_PATH = ROOT / "ideas.jsonl"
MAX_SIGNALS = 40
DEFAULT_SUBREDDITS = [
    "LocalLLaMA",
    "MachineLearning",
    "indiehackers",
    "SaaS",
    "ExperiencedDevs",
    "programming",
]
USER_AGENT = "x-scout-trend-radar/1.0 (https://github.com/HustleCoding/x-scout)"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: str | int | float | None) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    if not value:
        return now_utc()
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return now_utc()
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def age_hours(value: str | int | float | None) -> float:
    return max(0.0, (now_utc() - parse_datetime(value)).total_seconds() / 3600)


def signal(
    source: str,
    title: str,
    url: str,
    score: float,
    comments: float,
    age: float,
    extra: dict | None = None,
) -> dict:
    return {
        "source": source,
        "title": re.sub(r"\s+", " ", html.unescape(title or "")).strip(),
        "url": url,
        "score": max(0.0, float(score or 0)),
        "comments": max(0.0, float(comments or 0)),
        "age_hours": round(max(0.0, age), 2),
        "extra": extra or {},
    }


def collect_hacker_news(cfg: dict) -> list[dict]:
    """Collect front-page and topic-matched HN stories, without failing the run."""
    result = []
    try:
        response = requests.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json", timeout=15
        )
        response.raise_for_status()
        ids = response.json()[:15]
        for item_id in ids:
            try:
                item = requests.get(
                    f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json", timeout=10
                ).json() or {}
            except (requests.RequestException, ValueError):
                continue
            if item.get("title"):
                result.append(
                    signal(
                        "hacker_news",
                        item["title"],
                        item.get("url")
                        or f"https://news.ycombinator.com/item?id={item_id}",
                        item.get("score", 0),
                        item.get("descendants", 0),
                        age_hours(item.get("time")),
                        {"id": item_id, "kind": "front_page"},
                    )
                )
    except (requests.RequestException, ValueError, KeyError, TypeError):
        pass
    since = int((now_utc() - timedelta(hours=24)).timestamp())
    for topic in cfg.get("topics", []):
        try:
            response = requests.get(
                "https://hn.algolia.com/api/v1/search_by_date",
                params={
                    "query": topic,
                    "tags": "(story,comment)",
                    "numericFilters": f"created_at_i>{since}",
                    "hitsPerPage": 10,
                },
                timeout=20,
            )
            response.raise_for_status()
            hits = response.json().get("hits", [])
        except (requests.RequestException, ValueError, KeyError, TypeError):
            continue
        for item in hits:
            title = item.get("title") or item.get("story_title") or ""
            if not title:
                continue
            result.append(
                signal(
                    "hacker_news",
                    title,
                    item.get("url")
                    or f"https://news.ycombinator.com/item?id={item.get('objectID')}",
                    item.get("points", 0),
                    item.get("num_comments", 0),
                    age_hours(item.get("created_at_i")),
                    {"query": topic, "kind": "algolia"},
                )
            )
    return result


def reddit_rss(subreddit: str) -> list[dict]:
    response = requests.get(
        f"https://www.reddit.com/r/{subreddit}/top.rss?t=day&limit=15",
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    result = []
    for entry in root.findall("a:entry", ns):
        title = entry.findtext("a:title", "", ns)
        link = entry.find("a:link", ns)
        url = link.get("href") if link is not None else ""
        result.append(
            signal(
                "reddit",
                title,
                url,
                0,
                0,
                age_hours(entry.findtext("a:updated", "", ns)),
                {"subreddit": subreddit, "kind": "rss"},
            )
        )
    return result


def collect_reddit(cfg: dict) -> list[dict]:
    """Use public JSON first and RSS as a rate-limit-friendly fallback."""
    result = []
    for subreddit in cfg.get("topic_subreddits", DEFAULT_SUBREDDITS):
        try:
            response = requests.get(
                f"https://www.reddit.com/r/{subreddit}/top.json",
                params={"t": "day", "limit": 15},
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=30,
            )
            response.raise_for_status()
            posts = response.json().get("data", {}).get("children", [])
            for child in posts:
                data = child.get("data", {})
                result.append(
                    signal(
                        "reddit",
                        data.get("title", ""),
                        f"https://www.reddit.com{data.get('permalink', '')}",
                        data.get("ups", 0),
                        data.get("num_comments", 0),
                        age_hours(data.get("created_utc")),
                        {"subreddit": subreddit, "kind": "json"},
                    )
                )
        except (requests.RequestException, ValueError, ET.ParseError, AttributeError):
            try:
                result.extend(reddit_rss(subreddit))
            except (requests.RequestException, ET.ParseError, ValueError):
                continue
    return result


def x_credentials_available() -> bool:
    return all(
        os.environ.get(name, "").strip()
        for name in (
            "X_API_KEY",
            "X_API_SECRET",
            "X_ACCESS_TOKEN",
            "X_ACCESS_TOKEN_SECRET",
        )
    )


def collect_x(cfg: dict) -> list[dict]:
    """Search a small number of X queries, skipping unavailable/paid reads."""
    if not x_credentials_available():
        return []
    try:
        terms = list(dict.fromkeys((cfg.get("reply_keywords") or []) + (cfg.get("topics") or [])))
        terms = terms[: int(cfg.get("topic_x_queries", 3))]
        auth = OAuth1(
            os.environ["X_API_KEY"],
            os.environ["X_API_SECRET"],
            os.environ["X_ACCESS_TOKEN"],
            os.environ["X_ACCESS_TOKEN_SECRET"],
        )
        start_time = (now_utc() - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
        result = []
        for term in terms:
            response = requests.get(
                f"{X_API}/tweets/search/recent",
                params={
                    "query": f'"{term}" -is:retweet -is:reply -is:quote lang:en',
                    "max_results": 25,
                    "start_time": start_time,
                    "tweet.fields": "public_metrics,created_at",
                },
                auth=auth,
                timeout=30,
            )
            if response.status_code == 429:
                continue
            response.raise_for_status()
            tweets = response.json().get("data", [])
            tweets.sort(
                key=lambda tweet: sum(
                    tweet.get("public_metrics", {}).get(metric, 0) * weight
                    for metric, weight in (
                        ("like_count", 1),
                        ("retweet_count", 2),
                        ("reply_count", 3),
                        ("quote_count", 2),
                    )
                ),
                reverse=True,
            )
            for tweet in tweets[:8]:
                metrics = tweet.get("public_metrics", {})
                result.append(
                    signal(
                        "x",
                        tweet.get("text", ""),
                        f"https://x.com/i/status/{tweet['id']}",
                        metrics.get("like_count", 0)
                        + 2 * metrics.get("retweet_count", 0)
                        + 2 * metrics.get("quote_count", 0),
                        metrics.get("reply_count", 0),
                        age_hours(tweet.get("created_at")),
                        {"query": term},
                    )
                )
        return result
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return []


def collect_github() -> list[dict]:
    try:
        since = (now_utc() - timedelta(hours=24)).date().isoformat()
        headers = {"Accept": "application/vnd.github+json"}
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = requests.get(
            "https://api.github.com/search/repositories",
            params={"q": f"created:>{since} stars:>50", "sort": "stars", "per_page": 15},
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        return [
            signal(
                "github",
                item.get("full_name", ""),
                item.get("html_url", ""),
                item.get("stargazers_count", 0),
                item.get("open_issues_count", 0),
                age_hours(item.get("created_at")),
                {"description": item.get("description", ""), "language": item.get("language", "")},
            )
            for item in response.json().get("items", [])
        ]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return []


def collect_devto() -> list[dict]:
    try:
        response = requests.get(
            "https://dev.to/api/articles",
            params={"top": 1, "per_page": 15},
            timeout=30,
        )
        response.raise_for_status()
        return [
            signal(
                "dev.to",
                item.get("title", ""),
                item.get("url", ""),
                item.get("positive_reactions_count", 0),
                item.get("comments_count", 0),
                age_hours(item.get("published_at") or item.get("created_at")),
                {"tags": item.get("tag_list", [])},
            )
            for item in response.json()
        ]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return []


def heat_score(score: float, comments: float, age: float) -> float:
    """Measure engagement velocity with recency decay."""
    return (score + 2 * comments) / ((max(age, 0.0) + 2) ** 1.3)


def rank_signals(signals: list[dict]) -> list[dict]:
    by_source: dict[str, list[float]] = {}
    for item in signals:
        raw = heat_score(item["score"], item["comments"], item["age_hours"])
        item["raw_heat"] = raw
        by_source.setdefault(item["source"], []).append(raw)
    for item in signals:
        values = by_source[item["source"]]
        low, high = min(values), max(values)
        item["heat"] = (
            (item["raw_heat"] - low) / (high - low)
            if high > low
            else (1.0 if high > 0 else 0.0)
        )
    return sorted(signals, key=lambda item: item["heat"], reverse=True)


def collect_signals(cfg: dict) -> list[dict]:
    collectors = (collect_hacker_news, collect_reddit, collect_x, lambda c: collect_github(), lambda c: collect_devto())
    result = []
    for collector in collectors:
        try:
            result.extend(collector(cfg))
        except Exception:
            continue
    return rank_signals([item for item in result if item.get("title") and item.get("url")])


def recent_topic_headlines(days: int = 14) -> list[str]:
    if not TOPICS_LOG_PATH.exists():
        return []
    cutoff = now_utc() - timedelta(days=days)
    result = []
    for line in TOPICS_LOG_PATH.read_text().splitlines():
        try:
            entry = json.loads(line)
            if parse_datetime(entry.get("date")) >= cutoff:
                result.extend(topic.get("headline", "") for topic in entry.get("topics", []))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return [headline for headline in result if headline]


def llm_topics(cfg: dict, signals: list[dict]) -> list[dict]:
    signal_block = "\n".join(
        f"{i}. [{item['source']}, heat {item['heat']:.2f}] {item['title']} | {item['url']}"
        for i, item in enumerate(signals[:MAX_SIGNALS], 1)
    )
    history = "\n".join(f"- {headline}" for headline in recent_topic_headlines()) or "(none)"
    posts = "\n".join(f"- {item.get('text', '')}" for item in load_history_entries()[-14:]) or "(none)"
    memo = Path(ROOT / "memo.md").read_text().strip() if (ROOT / "memo.md").exists() else "(none)"
    prompt = f"""You find timely X post topics for a solo indie hacker and AI engineer.
Persona: {cfg['persona']}
Configured topics: {', '.join(cfg.get('topics', []))}

Ranked signals (cite exact URLs from this list):
{signal_block}

Style guide:
{load_unslop()}

Editor's memo:
{memo}

Recent GitHub activity:
{github_activity(cfg, limit=12, links=True) or '(none)'}

Topics already covered in the last 14 days. Do not repeat them:
{history}

Recent posts, for voice and repetition avoidance:
{posts}

Return ONLY a JSON array of {int(cfg.get('topic_count', 5))} objects. Each object must have:
headline, why_now, sources (array of exact signal URLs), relevance (0-10),
angle (a specific take tied to the persona's actual work where possible), and
drafts (exactly 2 X drafts, each <=280 characters). Avoid generic summaries,
fabricated facts, and AI-sounding phrasing. Prefer topics with a clear reason
to post today and a defensible personal angle."""
    raw = llm(
        cfg,
        prompt,
        max_tokens=3500,
        temperature=0.8,
        model=cfg.get("writer_model") or cfg["model"],
    )
    parsed = parse_json_block(raw)
    signal_by_url = {item["url"]: item for item in signals}
    topics = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        headline = str(item.get("headline", "")).strip()
        sources = [url for url in item.get("sources", []) if url in signal_by_url]
        drafts = [str(d).strip()[:280] for d in item.get("drafts", []) if str(d).strip()]
        try:
            relevance = max(0.0, min(10.0, float(item.get("relevance", 0))))
        except (TypeError, ValueError):
            relevance = 0.0
        if not headline or not sources or not drafts:
            continue
        mean_heat = sum(signal_by_url[url]["heat"] for url in sources) / len(sources)
        topics.append(
            {
                "headline": headline,
                "why_now": str(item.get("why_now", "")).strip(),
                "sources": sources,
                "relevance": relevance,
                "angle": str(item.get("angle", "")).strip(),
                "drafts": drafts[:2],
                "heat": round(mean_heat, 4),
                "score": round(relevance * mean_heat, 4),
            }
        )
    topics.sort(key=lambda item: item["score"], reverse=True)
    return topics[: int(cfg.get("topic_count", 5))]


def write_outputs(topics: list[dict], dry_run: bool = False) -> None:
    entry = {"date": now_utc().isoformat(), "topics": topics}
    if dry_run:
        return
    TOPICS_PATH.write_text(json.dumps(entry, indent=2, ensure_ascii=False) + "\n")
    with TOPICS_LOG_PATH.open("a") as file:
        file.write(json.dumps(entry, ensure_ascii=False) + "\n")
    lines = ["# hot topics radar", ""]
    for index, topic in enumerate(topics, 1):
        lines.extend(
            [
                f"## {index}. {topic['headline']}",
                f"**score:** {topic['score']:.2f}  ",
                f"**why now:** {topic['why_now']}  ",
                f"**angle:** {topic['angle']}  ",
                f"**draft:** {topic['drafts'][0]}  ",
                f"**sources:** {', '.join(topic['sources'])}",
                "",
            ]
        )
    REPORT_PATH.write_text("\n".join(lines))


def append_idea(topic: dict) -> None:
    text = f"{topic['angle']}\n\n{topic['drafts'][0]}".strip()
    with IDEAS_PATH.open("a") as file:
        file.write(json.dumps({"date": now_utc().isoformat(), "text": text}) + "\n")


def send_telegram(topics: list[dict], cfg: dict) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("topics: telegram not configured, skipping")
        return
    lines = ["hot topics radar\n"]
    for index, topic in enumerate(topics, 1):
        lines.extend(
            [
                f"{index}. {topic['headline']}",
                f"why now: {topic['why_now']}",
                f"angle: {topic['angle']}",
                f"draft: {topic['drafts'][0]}",
                "",
            ]
        )
    keyboard = [
        [{"text": f"save {i}", "callback_data": f"topic:{i - 1}"}]
        for i in range(len(topics))
    ]
    message = api(
        token,
        "sendMessage",
        chat_id=chat_id,
        text="\n".join(lines)[:4000],
        reply_markup={"inline_keyboard": keyboard},
    )["result"]
    deadline = time.time() + int(cfg.get("topic_telegram_wait_minutes", 10)) * 60
    offset = None
    while time.time() < deadline:
        params = {"timeout": 25, "allowed_updates": ["callback_query"]}
        if offset is not None:
            params["offset"] = offset
        for update in api(token, "getUpdates", **params)["result"]:
            offset = update["update_id"] + 1
            callback = update.get("callback_query") or {}
            if callback.get("message", {}).get("message_id") != message["message_id"]:
                continue
            api(token, "answerCallbackQuery", callback_query_id=callback["id"])
            data = callback.get("data", "")
            if data.startswith("topic:"):
                index = int(data.split(":", 1)[1])
                if 0 <= index < len(topics):
                    append_idea(topics[index])
                    api(
                        token,
                        "editMessageText",
                        chat_id=chat_id,
                        message_id=message["message_id"],
                        text=f"saved topic {index + 1} to the idea inbox.",
                    )
                return
    print("topics: no Telegram pick")


def print_topics(topics: list[dict]) -> None:
    for index, topic in enumerate(topics, 1):
        print(f"{index}. {topic['headline']} (score {topic['score']:.2f})")
        print(f"   why now: {topic['why_now']}")
        print(f"   angle: {topic['angle']}")
        print(f"   draft: {topic['drafts'][0]}")
        print(f"   sources: {', '.join(topic['sources'])}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="collect, rank with the LLM, and print; no Telegram or writes")
    parser.add_argument("--no-telegram", action="store_true", help="write files without Telegram")
    parser.add_argument("--signals-only", action="store_true", help="print raw collected signals and exit")
    args = parser.parse_args(argv)
    cfg = load_config()
    signals = collect_signals(cfg)
    if args.signals_only:
        for item in signals:
            print(json.dumps(item, ensure_ascii=False))
        return 0
    try:
        topics = llm_topics(cfg, signals)
    except (requests.RequestException, ValueError, KeyError, TypeError, SystemExit) as exc:
        print(f"topics: LLM generation failed: {exc}")
        return 1
    print_topics(topics)
    write_outputs(topics, dry_run=args.dry_run)
    if not args.dry_run and not args.no_telegram:
        try:
            send_telegram(topics, cfg)
        except (requests.RequestException, ValueError, KeyError, TypeError):
            print("topics: Telegram delivery failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
