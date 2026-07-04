#!/usr/bin/env python3
"""Send top post candidates to Telegram and wait for a pick.

Sends the candidates from a JSON file (scout.py --candidates-out) to a
private chat with inline buttons [1] [2] [3] [regenerate] [skip today],
long-polls for the tap, and writes the chosen text to an output file.
Replying to the chat with your own text posts that exact text instead,
and saves it as a taste example in config.json.

Environment variables:

    TELEGRAM_BOT_TOKEN   bot token from @BotFather
    TELEGRAM_CHAT_ID     chat id to send to (message the bot once, then
                         run `python tg_approve.py --chat-id` to find it)

Exit code is always 0; the decision is written to the GitHub output file
if GITHUB_OUTPUT is set, as `status` (chosen|edited|skip|timeout|
unconfigured) and `post` (the text to publish).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
POLL_SECONDS = 25
DEFAULT_TIMEOUT_MINUTES = 45
MAX_REGENS = 3
MAX_CHARS = 280


def api(token: str, method: str, **params) -> dict:
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=60
    )
    resp.raise_for_status()
    return resp.json()


def find_chat_id(token: str) -> int:
    updates = api(token, "getUpdates")["result"]
    for u in reversed(updates):
        msg = u.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            print(f"chat id: {chat['id']} ({chat.get('username') or chat.get('first_name')})")
            return 0
    print("no messages found; send any message to the bot first")
    return 1


def write_output(status: str, post: str = "") -> None:
    print(f"telegram: {status}" + (f" -> {post!r}" if post else ""))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"status={status}\n")
            f.write(f"post<<POST_EOF\n{post}\nPOST_EOF\n")


def save_taste_example(text: str) -> None:
    """Hand-written posts are the strongest taste signal; keep them."""
    if not CONFIG_PATH.exists():
        return
    cfg = json.loads(CONFIG_PATH.read_text())
    examples = cfg.setdefault("examples", [])
    if text not in examples:
        examples.append(text)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


def regenerate(path: str) -> list[dict]:
    subprocess.run(
        [sys.executable, str(ROOT / "scout.py"), "--generate-only", "regen_post.txt",
         "--candidates-out", path],
        check=True,
        cwd=ROOT,
    )
    return json.loads(Path(path).read_text())[:3]


def send_candidates(token: str, chat_id: str, candidates: list[dict], regens_left: int) -> int:
    lines = ["today's candidates:\n"]
    for i, c in enumerate(candidates):
        lines.append(f"{i + 1}. [{c['score']:.0f}] {c['text']}\n")
    lines.append("tap a number to post it, reply with your own text to post that instead, or skip.")
    row = [{"text": str(i + 1), "callback_data": f"pick:{i}"} for i in range(len(candidates))]
    bottom = [{"text": "skip today", "callback_data": "skip"}]
    if regens_left > 0:
        bottom.insert(0, {"text": "regenerate", "callback_data": "regen"})
    sent = api(
        token,
        "sendMessage",
        chat_id=chat_id,
        text="\n".join(lines),
        reply_markup={"inline_keyboard": [row, bottom]},
    )["result"]
    return sent["message_id"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--candidates", metavar="FILE", help="JSON file with scored candidates")
    parser.add_argument("--chat-id", action="store_true", help="print the chat id of the last message sent to the bot")
    parser.add_argument("--timeout-minutes", type=int, default=DEFAULT_TIMEOUT_MINUTES)
    args = parser.parse_args(argv)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if args.chat_id:
        if not token:
            print("missing TELEGRAM_BOT_TOKEN")
            return 1
        return find_chat_id(token)

    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        write_output("unconfigured")
        return 0
    if not args.candidates:
        raise SystemExit("--candidates FILE is required")

    candidates = json.loads(Path(args.candidates).read_text())[:3]
    regens_left = MAX_REGENS
    message_id = send_candidates(token, chat_id, candidates, regens_left)

    deadline = time.time() + args.timeout_minutes * 60
    offset = None
    while time.time() < deadline:
        params = {"timeout": POLL_SECONDS, "allowed_updates": ["callback_query", "message"]}
        if offset is not None:
            params["offset"] = offset
        for u in api(token, "getUpdates", **params)["result"]:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) == chat_id and msg.get("text", "").strip():
                text = msg["text"].strip()[:MAX_CHARS]
                save_taste_example(text)
                api(token, "sendMessage", chat_id=chat_id, text=f"posting your text:\n\n{text}")
                write_output("edited", text)
                return 0
            cq = u.get("callback_query")
            if not cq or cq.get("message", {}).get("message_id") != message_id:
                continue
            api(token, "answerCallbackQuery", callback_query_id=cq["id"])
            data = cq.get("data", "")
            if data == "skip":
                api(token, "editMessageText", chat_id=chat_id, message_id=message_id,
                    text="skipped today.")
                write_output("skip")
                return 0
            if data == "regen" and regens_left > 0:
                regens_left -= 1
                api(token, "editMessageText", chat_id=chat_id, message_id=message_id,
                    text="regenerating...")
                candidates = regenerate(args.candidates)
                message_id = send_candidates(token, chat_id, candidates, regens_left)
                continue
            if data.startswith("pick:"):
                idx = int(data.split(":", 1)[1])
                choice = candidates[idx]["text"]
                api(token, "editMessageText", chat_id=chat_id, message_id=message_id,
                    text=f"posting:\n\n{choice}")
                write_output("chosen", choice)
                return 0
    api(token, "editMessageText", chat_id=chat_id, message_id=message_id,
        text="no pick in time; falling back to github approval.")
    write_output("timeout")
    return 0


if __name__ == "__main__":
    sys.exit(main())
