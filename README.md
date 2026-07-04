# x-scout

Posts to X (x.com) two ways:

1. **`scout.py`** — generates a post with an LLM (OpenRouter) and publishes it
   via the X API v2. No browser. Runs automatically once a day via GitHub
   Actions.
2. **`post_vague.py`** — the original Selenium/Brave script, still useful for
   `--find` (search X and open the best post to reply to).

## Daily automation (scout.py)

`.github/workflows/daily-post.yml` runs `scout.py` every day at 09:17 UTC and
commits the post to `posted.jsonl` (used to avoid repeating ideas). It can also
be triggered manually from the Actions tab, optionally as a dry run.

Required repo Actions secrets: `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`,
`X_ACCESS_TOKEN_SECRET`, `OPENROUTER_API_KEY`.

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
