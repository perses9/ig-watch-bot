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

- `/watch <username> [username2 ...]` — start tracking one or more accounts (checked immediately)
- `/check <username>` — check status right now, without waiting for the next cycle or adding it to your list
- `/list` — show tracked accounts, status, follower count, and mute state
- `/remove <username>` — stop tracking an account
- `/pause <username>` / `/resume <username>` — mute or unmute notifications for an account without removing it
- `/uptime` — bot uptime and how many checks it's run this session
- `/myid` — show your own chat ID (works before you've been granted access)
- `/help` — show usage

Owner-only:

- `/users` — see who currently has access
- `/adduser <chat_id> [name]` — grant access to someone (up to `MAX_GUEST_USERS`, default 5)
- `/removeuser <chat_id>` — revoke access, which also deletes that person's watchlist

## Access control

By default anyone who finds the bot on Telegram can use it, so set `OWNER_CHAT_ID`
to your own chat ID to lock it down. Send `/myid` to the bot to get that number.

The owner can then invite up to `MAX_GUEST_USERS` other people from inside
Telegram — no redeploy or config change needed. The person sends `/myid`, gives
you the number, and you run `/adduser <their_id> <name>`.

Guests get their own private watchlist: `/list` only ever returns rows for the
chat that asked, and notifications only go to chats tracking that specific
username, so guests can't see each other's accounts or yours. Guests cannot
grant access to anyone else. Revoking someone with `/removeuser` also removes
their tracked accounts, so nothing keeps getting polled on a revoked user's
behalf. Note that guests do share your `PROXY_URL` bandwidth.

`ALLOWED_CHAT_IDS` still works as a static allowlist and is additive to the
invite list; if `OWNER_CHAT_ID` isn't set, its lowest entry becomes the owner.

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

## Running it 24/7 (VPS, recommended for tight polling)

A small always-on VPS (Hetzner CX22 ~€4/mo, DigitalOcean basic droplet
~$6/mo) is the best fit if you want a short poll interval: flat-rate
pricing regardless of how often you poll, no cold starts, and full control
if you later add proxy rotation to poll faster. Ubuntu 22.04/24.04 steps:

```bash
# on the VPS, as root
apt update && apt install -y python3-venv git
useradd --system --create-home --shell /usr/sbin/nologin igwatch

git clone <your-repo-url> /opt/ig-watch-bot
cd /opt/ig-watch-bot
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

cp .env.example .env
nano .env               # fill in BOT_TOKEN, adjust CHECK_INTERVAL_SECONDS
chown -R igwatch:igwatch /opt/ig-watch-bot

cp deploy/ig-watch-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ig-watch-bot

# check it's running / watch logs live
systemctl status ig-watch-bot
journalctl -u ig-watch-bot -f
```

`Restart=on-failure` in the unit file means it comes back up if it crashes,
and `enable` means it starts automatically on VPS reboot. To deploy an
update later: `git pull`, `./venv/bin/pip install -r requirements.txt` (if
deps changed), `systemctl restart ig-watch-bot`.

## Alternative: Railway / Render (less ops, worse fit for frequent polling)

Connect this GitHub repo, set `BOT_TOKEN` as an env var, deploy as a
"worker" (uses the included `Procfile`). Zero server maintenance, but
usage-based billing scales worse the more aggressively you poll, and some
tiers sleep idle workers. Add a persistent volume mounted at the working
directory if you want the watchlist (`watchlist.db`) to survive redeploys —
otherwise it resets to empty on each deploy but survives normal restarts.

## Docker (either host)

```bash
docker build -t ig-watch-bot .
docker run -d --env-file .env -v $(pwd)/data:/app ig-watch-bot
```

## Rate-limit note

Instagram treats datacenter IPs (Railway, any VPS, AWS, etc.) with much more
suspicion than home/mobile connections, and can block a whole hosting
provider's IP range outright regardless of how browser-like the requests
look. The checker already does what's possible on the request side (real
browser headers, session-cookie warmup, matching the exact host/endpoint a
logged-out browser uses) and backs off automatically on `429`s — but if
Instagram has flagged the IP range itself, no request-shaping fixes it.

The actual fix at that point is a residential/mobile proxy: set `PROXY_URL`
(see `.env.example`) to a provider's proxy endpoint and every request routes
through a real residential/mobile IP instead of the server's own. Bright
Data, Oxylabs, Smartproxy, and IPRoyal all offer this; pricing is usage-based,
typically $10-50+/mo depending on volume. Prefer a per-request rotating IP
plan over a sticky one, since a fresh IP on every check is exactly what
prevents a block from forming in the first place.

## Environment variables

| Variable                | Default          | Meaning                                   |
|--------------------------|------------------|--------------------------------------------|
| `BOT_TOKEN`              | *required*       | Telegram bot token from BotFather          |
| `CHECK_INTERVAL_SECONDS` | `15`             | How often every watched account is rechecked |
| `CONFIRM_CHECKS`         | `2`              | Consecutive matching checks needed before announcing a status change |
| `DB_PATH`                | `watchlist.db`   | SQLite file storing watchlists + status    |
| `ALLOWED_CHAT_IDS`       | *(none)*         | Comma-separated Telegram chat IDs allowed to use the bot |
| `OWNER_CHAT_ID`          | *(lowest allowed ID)* | Chat ID allowed to grant/revoke access via `/adduser` |
| `MAX_GUEST_USERS`        | `5`              | How many people the owner can grant access to |
| `PROXY_URL`              | *(none)*         | Residential/mobile proxy URL to route all Instagram requests through |

## Note on this build

This code was written and syntax/import-checked in a sandboxed environment
whose network policy blocks direct requests to Instagram's domains, so the
live Instagram response handling could not be exercised end-to-end here.
The endpoint and headers used are the standard, widely-used approach for
unauthenticated Instagram profile lookups. Test it against a couple of real
usernames (a live one and a known-suspended one) right after your first
deploy to confirm status detection matches what you expect, and ping me
with the results if anything looks off.
