# x-scout

## How you use it (day-to-day)

Everything happens in your Telegram chat with the bot. Romania times (UTC+2/+3):

| When | What arrives | What you do |
|---|---|---|
| ~09:17/10:17 | **Leads** — up to 5 messages: someone on X / Reddit / Hacker News asking for a developer/app/MVP, with a link and a suggested reply | Open the link, adapt the suggested reply, and answer them from your own account. Or ignore it. |
| ~15:17/16:17 | **Morning briefing** — your last post's metrics, your repo activity, today's HN front page (all linked) | Just read it. |
| right after | **3 candidate posts** with buttons | Tap `1/2/3` to publish that one, `regenerate` for a fresh batch, or `skip today`. Type your own text instead to post exactly that (it's also saved as a taste example). No answer in 45 min → falls back to the GitHub approve gate. |
| ~17:47/18:47 | **Quote opportunities** — 2 high-traction posts in your topics with a drafted comment | Currently blocked by X policy (API can't quote strangers at this tier) — treat as suggestions to post manually, or disable the workflow. |

Anytime: **text or voice-note the bot a raw idea** — it lands in the idea inbox
and the next day's candidates are drafted from your ideas first.

Nothing is ever published without your tap. Tune the voice in `config.json`
(`persona`, `examples`, `topics`); tune lead hunting with `lead_keywords`,
`lead_subreddits`, `lead_pitch_context`.

---

Posts to X (x.com) two ways:

1. **`scout.py`** — generates a post with an LLM (OpenRouter) and publishes it
   via the X API v2. No browser. Runs automatically once a day via GitHub
   Actions.
2. **`post_vague.py`** — the original Selenium/Brave script, still useful for
   `--find` (search X and open the best post to reply to).

## Daily automation (scout.py)

`.github/workflows/daily-post.yml` runs every day at 13:17 UTC (after 3pm Romania time) in two stages:

1. **generate** — creates several candidate posts (writer model, one format
   each: story, number, unpopular opinion, ...), grounded in recent public
   GitHub activity and steered by the taste `examples` in `config.json`,
   scores each against the X algorithm rubric in `judge.md` (judge model),
   and writes the winner plus the full scored table to the run's summary
   page. All candidates are logged to `candidates.jsonl`; winners that were
   rejected in past runs are fed back as negative examples.
   Before generating, the run refreshes engagement metrics for recent posts
   (`scout.py --update-metrics`) so the prompt can cite your best and worst
   performers. A weekly job (`weekly-review.yml`, Sundays) has the LLM write
   an "editor's memo" (`memo.md`) from the data — concrete directives that
   steer future posts.
   The run also collects any messages you sent the bot since the last run
   into an idea inbox (`ideas.jsonl`) — text (or send a voice note to) the
   bot a raw thought anytime
   and the next generation drafts candidates from your ideas first (ideas
   stay active for 7 days). It then sends you a morning briefing on
   Telegram: your latest post's metrics, your recent repo activity, and
   today's Hacker News front page.
2. **approve** — if `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` secrets are
   set, the bot messages you the top 3 candidates with buttons: tap 1/2/3 to
   post that one, "regenerate" for a fresh batch (up to 3 times), or "skip
   today". Sending the bot your own text posts that exact text instead and
   saves it to `examples` in `config.json` as a taste example. Setup: create
   a bot with @BotFather, add the two secrets (find your chat id with
   `python tg_approve.py --chat-id` after messaging the bot once).
   The generation prompt also sees the day's Hacker News front page, so
   candidates can react to what devs are talking about (only when the model
   has a genuine take).
3. **publish** — if you picked on Telegram, that candidate is posted
   directly. If Telegram isn't configured or you didn't answer within 45
   minutes, it falls back to the GitHub approval gate (the `approve-post`
   environment): Approve publishes the judge's top pick, Reject skips the
   day. Published posts are logged to `posted.jsonl` (used to avoid
   repeating ideas).

It can also be triggered manually from the Actions tab, optionally as a dry
run (generate only, no publish job).

A second daily workflow (`daily-replies.yml`, 15:47 UTC) grows the account
through quote posts: it searches recent X posts for `reply_keywords` in
`config.json`, ranks them by traction, drafts a quote post for the top 2 (never
the same author twice in a week — `replied.jsonl`), and asks you on Telegram
per quote ("post quote" / "skip"). Nothing is ever posted without your tap;
if Telegram isn't configured the job does nothing. Note: the search endpoint
is a paid X API read (~25 tweets/day).

A third daily workflow (`daily-leads.yml`, 07:17 UTC) scouts for clients
across X (`lead_keywords`, paid search read ~25 tweets/day), Reddit
(`lead_subreddits` via public RSS, free), and Hacker News (Algolia search,
free): posts by people asking for a developer/app/MVP are filtered by an LLM
check that drops developers advertising themselves, deduped against
`leads.jsonl`, and sent to Telegram with the post link and a suggested pitch
(tuned by `lead_pitch_context`). The agent never replies itself — you
paste/adapt the pitch from your phone, which also sidesteps the X API
engagement restrictions.

One-time setup:

- Repo Actions secrets: `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`,
  `X_ACCESS_TOKEN_SECRET`, `OPENROUTER_API_KEY`.
- Environment: Settings → Environments → New environment `approve-post` →
  enable "Required reviewers" and add yourself.

Run locally:

```sh
pip install -r requirements.txt
python scout.py --verify     # check X credentials, no post
python scout.py --dry-run    # generate a post, print it, do not publish
python scout.py              # generate and publish
python scout.py -m "hello"   # publish a specific message
```

Tune the voice and subject matter in `config.json` (`persona`, `topics`,
`model`).

---

# Browser script (post_vague.py)

Posts a vague message to X (x.com) through the local Brave browser.

## Setup (already done if the `venv/` folder exists)

```sh
python3 -m venv venv
./venv/bin/pip install selenium
```

## Use

1. Sign in once (saves the session to `./brave-profile`):

   ```sh
   ./run.sh --login
   ```

2. Post a vague message:

   ```sh
   ./run.sh                 # random vague line
   ./run.sh -m "hmm"        # your own text
   ./run.sh --dry-run       # type it, do not click Post
   ```

## Find a post to reply to

Searches X for a topic, picks the most engaging post, and opens it in Brave
for you to read. You write and send the reply yourself. The script does not
auto-reply.

```sh
./run.sh --find "agentic AI"              # open the top post to reply to
./run.sh --find "agentic AI" --sort live  # most recent instead of top
./run.sh --find "agentic AI" --no-wait    # print the post and quit
```

## Verify without posting

```sh
./run.sh --check
```

Launches Brave headless with a throwaway profile, reaches x.com, and reports the page title. Proves the Brave + chromedriver + network path works.

## Notes

- A dedicated profile under `./brave-profile` holds your login. It is separate from your normal Brave profile, so it never locks or touches your everyday browser.
- Selenium Manager auto-downloads a chromedriver matching your Brave version (149.x). No manual driver setup.
- If a run fails with "could not lock the profile", close any other window using this script's Brave profile and retry.
- X limits posts to 280 characters; longer messages are truncated.
