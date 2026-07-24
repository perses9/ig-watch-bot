# ig-watch-bot

Telegram bot that watches Instagram usernames and tells you when they go
offline (banned / suspended / deactivated) and when they come back.

## Why this fixes the "session expired" problem

The previous version apparently used a logged-in Instagram session
(cookies / instagrapi-style login). Instagram actively invalidates those
sessions — especially when hit repeatedly from automation — which is why it
kept expiring and throwing `401`s.

This version never logs in. It calls Instagram's own public web endpoint
(`i.instagram.com/api/v1/users/web_profile_info`) the same way a logged-out
browser visiting `instagram.com/<username>` does, using the public
`X-IG-App-ID` header the website itself uses. There is no session to expire.

Trade-off: without a login, Instagram can't tell you *why* a profile isn't
reachable (never existed vs. banned vs. deactivated vs. deleted) — it just
returns "not found" in all of those cases. The bot reports this as
`not found / suspended / deactivated`.

## How status changes are decided

Every `CHECK_INTERVAL_SECONDS` (default 30s) the bot rechecks every watched
username. A single failed/blocked check does **not** flip the reported
status — it's logged and the last confirmed status is kept, so a transient
block doesn't spam you with false alarms. A status only changes after it's
seen consistently for `CONFIRM_CHECKS` checks in a row (default 2, i.e.
confirmed within ~1 minute at the default interval). When an account flips
from not-found back to live, everyone tracking it gets pinged immediately.

## Commands

- `/watch <username>` — start tracking an account
- `/list` — show tracked accounts and their current status
- `/remove <username>` — stop tracking an account
- `/help` — show usage

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and grab the token.
2. Copy `.env.example` to `.env` and fill in `BOT_TOKEN`.
3. Install deps and run:

   ```bash
   python3 -m venv venv && source venv/bin/activate
   pip install -r requirements.txt
   export $(cat .env | xargs)   # or use a process manager / systemd EnvironmentFile
   python bot.py
   ```

The bot uses long polling, so it just needs outbound internet — no public
URL or webhook needed.

## Running it 24/7

You need something that keeps the process alive continuously. Options,
cheapest/simplest first:

- **Railway / Render (recommended)**: connect this GitHub repo, set
  `BOT_TOKEN` as an env var, deploy as a "worker" (uses the included
  `Procfile`). No server maintenance. Add a persistent volume mounted at the
  working directory if you want the watchlist (`watchlist.db`) to survive
  redeploys — otherwise it resets to empty on each deploy but survives
  normal restarts.
- **Any small VPS** (e.g. a $4-6/mo droplet): clone the repo, `pip install
  -r requirements.txt`, run under `systemd` or `pm2` so it restarts on crash
  and on boot.
- **Docker anywhere**: `docker build -t ig-watch-bot . && docker run -d
  --env-file .env -v $(pwd)/data:/app ig-watch-bot`

## Rate-limit note

Instagram will throttle or temporarily block an IP that polls too
aggressively, especially with many tracked usernames on one bot. The check
job already spaces individual requests out (1-3s jitter) and treats
`429`/timeouts as "temporary check issue, not a real change" rather than a
status flip. If you track a lot of accounts and see frequent temporary
check issues in the logs, raise `CHECK_INTERVAL_SECONDS` or route requests
through a proxy.

## Environment variables

| Variable                | Default          | Meaning                                   |
|--------------------------|------------------|--------------------------------------------|
| `BOT_TOKEN`              | *required*       | Telegram bot token from BotFather          |
| `CHECK_INTERVAL_SECONDS` | `30`             | How often every watched account is rechecked |
| `CONFIRM_CHECKS`         | `2`              | Consecutive matching checks needed before announcing a status change |
| `DB_PATH`                | `watchlist.db`   | SQLite file storing watchlists + status    |

## Note on this build

This code was written and syntax/import-checked in a sandboxed environment
whose network policy blocks direct requests to Instagram's domains, so the
live Instagram response handling could not be exercised end-to-end here.
The endpoint and headers used are the standard, widely-used approach for
unauthenticated Instagram profile lookups. Test it against a couple of real
usernames (a live one and a known-suspended one) right after your first
deploy to confirm status detection matches what you expect, and ping me
with the results if anything looks off.
