# x-scout

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
