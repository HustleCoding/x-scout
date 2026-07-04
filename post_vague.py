#!/usr/bin/env python3
"""Post a vague message to X (x.com) through the local Brave browser.

The script drives Brave with a dedicated, persistent browser profile. You log
into X once; that session is saved to the profile and reused on every later
run, so no credentials ever live in this file.

First run, sign in once:

    venv/bin/python post_vague.py --login

Then post:

    venv/bin/python post_vague.py            # a random vague message
    venv/bin/python post_vague.py -m "hmm"   # a specific message
    venv/bin/python post_vague.py --dry-run  # type it, do not click Post

Verify the browser layer without posting:

    venv/bin/python post_vague.py --check

Find an interesting post to reply to (you write and send the reply yourself):

    venv/bin/python post_vague.py --find "agentic AI"
    venv/bin/python post_vague.py --find "agentic AI" --sort live
    venv/bin/python post_vague.py --find "agentic AI" --no-wait   # print and quit
"""
from __future__ import annotations

import argparse
import random
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

BRAVE_PATH = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
PROFILE_DIR = Path(__file__).resolve().parent / "brave-profile"
X_HOME = "https://x.com/home"
X_LOGIN = "https://x.com/i/flow/login"
X_COMPOSE = "https://x.com/compose/post"
MAX_CHARS = 280
PAGE_TIMEOUT = 45

VAGUE_MESSAGES = [
    "hmm",
    "we'll see",
    "thinking about it",
    "interesting times",
    "not so sure anymore",
    "anyway",
    "could go either way",
    "noted",
]

# X ships stable data-testid hooks; the CSS fallbacks absorb redesigns.
COMPOSE_EDITOR = (
    '[data-testid="tweetTextarea_0"],'
    'div[role="textbox"][contenteditable="true"],'
    'textarea[aria-label*="Tweet"],'
    'textarea[aria-label*="What"]'
)
POST_BUTTON = (
    '[data-testid="tweetButton"],'
    '[data-testid="tweetButtonInline"]'
)
LOGGED_IN_MARKER = '[data-testid="SideNav_NewTweet_Button"], [data-testid="primaryColumn"]'
X_SEARCH = "https://x.com/search?q={q}&src=typed_query&f={f}"
TWEET_TEXT_SEL = '[data-testid="tweetText"]'
TWEET_PERMALINK_SEL = 'a[href*="/status/"]'


@dataclass
class PostConfig:
    brave_path: str
    profile_dir: Path
    message: str = ""
    dry_run: bool = False
    headless: bool = False


def build_driver(cfg: PostConfig, profile_dir: Path, headless: bool) -> webdriver.Chrome:
    options = Options()
    options.binary_location = cfg.brave_path
    # Dedicated user-data-dir so login persists and we never touch, or lock,
    # your normal Brave profile.
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    # Lower the automation fingerprint; X is bot-sensitive.
    options.add_argument("--disable-blink-features=AutomationControlled")
    if headless:
        options.add_argument("--headless=new")
    options.page_load_timeout = PAGE_TIMEOUT
    try:
        return webdriver.Chrome(options=options)
    except Exception as exc:
        msg = str(exc)
        if "user data directory" in msg.lower() or "profile" in msg.lower():
            raise SystemExit(
                "Brave could not lock the profile. Close any other window using "
                f"{profile_dir} and retry."
            ) from exc
        raise


def looks_logged_out(driver: webdriver.Chrome) -> bool:
    url = (driver.current_url or "").lower()
    return "login" in url or "flow" in url or "i/flow" in url


def run_check(cfg: PostConfig) -> int:
    # Throwaway profile + headless: proves Brave+driver reach X without
    # touching your saved login or posting anything.
    with tempfile.TemporaryDirectory(prefix="x-check-") as tmp:
        driver = build_driver(cfg, Path(tmp), headless=True)
        try:
            driver.get(X_HOME)
            title = driver.title or "(no title)"
            print(f"check: reached {driver.current_url}")
            print(f"check: page title = {title!r}")
            print("check: OK (Brave + chromedriver + network path work)")
            return 0
        finally:
            driver.quit()


def run_login(cfg: PostConfig) -> int:
    driver = build_driver(cfg, cfg.profile_dir, headless=False)
    try:
        driver.get(X_HOME)
        print("Brave is open at X. Log in (handle 2FA if prompted).")
        print("Waiting for the logged-in home timeline...")
        WebDriverWait(driver, 300).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, LOGGED_IN_MARKER))
        )
        print("login: signed in. Session saved to the profile. You can post now.")
        return 0
    finally:
        driver.quit()


def post_message(cfg: PostConfig) -> int:
    driver = build_driver(cfg, cfg.profile_dir, headless=cfg.headless)
    try:
        wait = WebDriverWait(driver, 30)
        driver.get(X_COMPOSE)
        if looks_logged_out(driver):
            raise SystemExit("Not logged in. Run with --login first.")
        editor = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, COMPOSE_EDITOR))
        )
        editor.click()
        editor.send_keys(cfg.message)
        if cfg.dry_run:
            print(f"dry-run: typed {cfg.message!r}, not posting")
            return 0
        button = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, POST_BUTTON))
        )
        button.click()
        try:
            WebDriverWait(driver, 15).until(
                EC.invisibility_of_element_located((By.CSS_SELECTOR, COMPOSE_EDITOR))
            )
            print(f"posted: {cfg.message!r}")
            return 0
        except Exception:
            print(
                "post: clicked Post but the composer is still open. Check the "
                "window for an error (duplicate post, rate limit, or modal prompt)."
            )
            return 1
    finally:
        driver.quit()


@dataclass
class FoundPost:
    text: str
    url: str
    likes: int = 0
    retweets: int = 0
    replies: int = 0
    score: int = 0

    @property
    def author(self) -> str:
        m = re.search(r"x\.com/([^/]+)/status/", self.url)
        return m.group(1) if m else "?"


def parse_count(s: str) -> int:
    s = s.strip().replace(",", "")
    mult = 1
    if s.upper().endswith("K"):
        mult, s = 1_000, s[:-1]
    elif s.upper().endswith("M"):
        mult, s = 1_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def _metric(art, testid: str) -> int:
    try:
        btn = art.find_element(By.CSS_SELECTOR, f'[data-testid="{testid}"]')
        label = btn.get_attribute("aria-label") or ""
        m = re.search(r"([\d,\.]+\s*[KkMm]?)", label)
        return parse_count(m.group(1)) if m else 0
    except Exception:
        return 0


def parse_article(art) -> FoundPost | None:
    try:
        text = art.find_element(By.CSS_SELECTOR, TWEET_TEXT_SEL).text
    except Exception:
        return None
    href = None
    for a in art.find_elements(By.CSS_SELECTOR, TWEET_PERMALINK_SEL):
        h = a.get_attribute("href") or ""
        if re.search(r"/status/\d+", h):
            href = h
            break
    if not href or "Promoted" in (art.text or ""):
        return None
    likes = _metric(art, "like")
    retweets = _metric(art, "retweet")
    replies = _metric(art, "reply")
    score = likes + retweets * 3 + replies
    return FoundPost(text=text, url=href, likes=likes, retweets=retweets, replies=replies, score=score)


def find_interesting_post(driver, wait, query: str, sort: str) -> FoundPost:
    driver.get(X_SEARCH.format(q=quote(query), f=sort))
    if looks_logged_out(driver):
        raise SystemExit("Not logged in. Run with --login first.")
    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "article")))
    except Exception:
        raise RuntimeError("no search results loaded; check the query or that X changed its DOM")
    for _ in range(2):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1.2)
    articles = driver.find_elements(By.CSS_SELECTOR, "article")
    posts = [p for p in (parse_article(a) for a in articles[:20]) if p]
    if not posts:
        raise RuntimeError("no tweets parsed from search results; X may have changed its DOM")
    posts.sort(key=lambda p: p.score, reverse=True)
    return posts[0]


def print_reply_angles(post: FoundPost) -> None:
    snippet = " ".join(post.text.split())[:140]
    print(f"post from @{post.author}")
    print(f"  {snippet}")
    print(f"engagement is {post.likes} likes, {post.retweets} reposts, {post.replies} replies")
    print(f"post url is {post.url}")
    print("Reply angle 1 (build on it). What does this concretely enable that wasn't possible before?")
    print("Reply angle 2 (stress-test it). Where does this break, or what's the hidden cost?")


def wait_for_close(driver, max_seconds: int) -> None:
    deadline = time.time() + max_seconds
    while time.time() < deadline:
        time.sleep(2)
        try:
            if not driver.window_handles:
                return
        except Exception:
            return


def run_find(cfg: PostConfig, query: str, sort: str, no_wait: bool) -> int:
    driver = build_driver(cfg, cfg.profile_dir, headless=cfg.headless)
    try:
        wait = WebDriverWait(driver, 30)
        post = find_interesting_post(driver, wait, query, sort)
        driver.get(post.url)
        print_reply_angles(post)
        if no_wait:
            print("\nfind done. Opened the post, then quitting (--no-wait).")
            return 0
        print("\nBrowser is open on the post. Read it and write your reply yourself.")
        print("Close the Brave window when you're done, and this script will exit.")
        wait_for_close(driver, 600)
        return 0
    finally:
        driver.quit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Post a vague message to X via the local Brave browser."
    )
    parser.add_argument("-m", "--message", help="message text (default: random vague line)")
    parser.add_argument("--login", action="store_true", help="open X to sign in once, then exit")
    parser.add_argument("--dry-run", action="store_true", help="type the message but do not click Post")
    parser.add_argument("--check", action="store_true", help="verify Brave reaches X, no post")
    parser.add_argument("--headless", action="store_true", help="run Brave headless (not recommended for posting)")
    parser.add_argument("--find", metavar="TOPIC", help="search X for TOPIC and open the most interesting post to read and reply to")
    parser.add_argument("--sort", choices=["top", "live"], default="top", help="search ranking (default top)")
    parser.add_argument("--no-wait", action="store_true", help="find and print the post, then quit without keeping the browser open")
    parser.add_argument("--brave", default=BRAVE_PATH, help="path to the Brave binary")
    parser.add_argument("--profile", default=str(PROFILE_DIR), help="browser profile directory")
    args = parser.parse_args(argv)

    cfg = PostConfig(
        brave_path=args.brave,
        profile_dir=Path(args.profile),
        dry_run=args.dry_run,
        headless=args.headless,
    )

    if args.check:
        return run_check(cfg)
    if args.login:
        return run_login(cfg)
    if args.find:
        return run_find(cfg, args.find, args.sort, args.no_wait)

    message = args.message or random.choice(VAGUE_MESSAGES)
    if len(message) > MAX_CHARS:
        message = message[:MAX_CHARS]
    cfg.message = message
    return post_message(cfg)


if __name__ == "__main__":
    sys.exit(main())
