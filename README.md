# ig-watch-bot

Telegram bot that watches Instagram accounts and messages you the moment a
suspended one comes back online.

Built for the case where you're waiting on an account to be reinstated: it
polls the ones that are down every few minutes, and pings you with the profile
photo, name, follower count, and how long it was gone.

## How it detects status

Instagram makes this harder than it sounds, and the approach here is the
result of finding out what actually works against the live site rather than
what ought to.

**The JSON API is checked first** —
`instagram.com/api/v1/users/web_profile_info`. It answers `404` for an
account that's suspended, deactivated, or gone, and returns the full profile
when it's live. No login required, so there's no session to expire — the
problem the original version of this bot suffered from.

**Retried across exit IPs.** That endpoint answers from some residential IPs
and returns `401` from others. Since the proxy hands out a different IP per
request, a refusal is retried (`API_ATTEMPTS`, default 6) rather than treated
as failure. These responses are a few hundred bytes, so retrying is cheap.

**Only the first 96KB of the page is read.** Everything the checker looks for
lives in `<head>`; the remaining ~570KB is the JavaScript bundle. The read is
aborted once that budget is spent, which genuinely stops the transfer rather
than downloading and discarding it — this is the difference between ~100KB and
~2KB per check on a metered proxy. If a truncated read settles nothing, the
full page is fetched as a last resort (`PAGE_READ_LIMIT`).

**The profile page is a fallback, not the primary.** Logged-out visitors now
get a ~600KB JavaScript shell that frequently contains *no profile data at
all* — no `og:` meta tags, no embedded JSON. When it does carry data, the
checker reads both the `og:` tags and the JSON the page ships to hydrate
itself. Worth knowing: `/diag` against a real suspended account showed the
page returning a 600KB shell with zero usernames in it while the API
correctly answered 404.

**A HEAD probe is the tiebreaker.** When the API refuses from every exit IP
and the page is a shell, a headers-only request still settles the common case:
Instagram answers `404` for an account that's gone. Only the 404 is trusted — a
`200` has been observed for an account that was in fact suspended — so it can
resolve "it's down" without ever being able to wrongly claim "it's live". It
runs before the page fetch, so confirming a suspended account usually costs
under a kilobyte.

**Nothing is guessed.** A blocked, throttled, or ambiguous response is
reported as a check issue and the last confirmed status is kept — the bot
never invents a status change it isn't sure about.

## How status changes are decided

A status only changes after `CONFIRM_CHECKS` checks in a row agree (default
2), so one flaky response can't fire a false "back online!" alert. Every path
that checks an account — the background loop, `/watch`, and the 🔍 Check now
button — runs through the same logic, so a manual check can't silently
consume the alert the background loop was about to send.

The first time an account resolves it's recorded silently as a baseline.
Nothing changed; the bot just learned where things stand, and announcing that
would be noise.

## Commands

- `/watch <username> [username2 ...]` — start tracking one or more accounts
- `/check <username>` — check right now, without adding it to your list
- `/list` — everything you're tracking, with quick-action buttons
- `/remove <username>` — stop tracking
- `/pause <username>` / `/resume <username>` — mute or unmute alerts
- `/uptime` — health, data usage, and projected monthly proxy cost
- `/myid` — your chat ID (works before you've been granted access)
- `/help` — usage

Owner-only:

- `/users` — who has access, each with a revoke button
- `/adduser <chat_id> [name]` — grant access (up to `MAX_GUEST_USERS`)
- `/removeuser <chat_id>` — revoke access and delete their watchlist
- `/allwatches` — every account being polled and whose list it's on
- `/diag <username>` — what Instagram actually returned: status codes, page
  size, which markers matched, proxy exit IP. This is the tool to reach for
  when a check misbehaves; it turns "inconclusive" into something readable.

## Access control

Set `OWNER_CHAT_ID` to your own chat ID to lock the bot down — without it (or
`ALLOWED_CHAT_IDS`) anyone who finds the bot can use it.

When someone without access messages the bot, **the owner gets a request with
Grant / Deny buttons**, showing their name and chat ID. One tap admits them
and tells them they're in. No copying IDs between people. Requests are rate
limited so a denied user can't pester you.

Guests get their own private watchlist: `/list` only returns rows for the
chat that asked, and notifications only reach chats tracking that specific
username — guests can't see your accounts or each other's. Guests can neither
grant nor revoke access; that check is separate from ordinary authorisation,
since a guest passes that. Revoking someone also deletes their watchlist, so
nothing keeps being polled on their behalf.

Guests do share your `PROXY_URL` data allowance.

`ALLOWED_CHAT_IDS` still works as a static allowlist and is additive to the
invite list. If `OWNER_CHAT_ID` isn't set, the *first* entry becomes the owner
(order matters — group chat IDs are large negative numbers).

## Proxy, and what it costs

Instagram blocks datacenter IP ranges — Railway, any VPS, AWS — regardless of
how browser-like the request looks. A residential proxy is not optional in
practice. Set `PROXY_URL` to a rotating residential endpoint, e.g.
`http://user:pass@geo.iproyal.com:12321`. Prefer per-request rotation over
sticky sessions.

Budget on the rate you'll actually pay, not the advertised one. IPRoyal
headlines $1.75/GB, but that's a bulk tier — a 1 GB order is $7 and 2 GB is
$12, so small buyers are really paying **$6–7/GB**. Set `PROXY_COST_PER_GB` to
your real rate or `/uptime` will flatter you.

Cost is driven by how many accounts are **down**, since those are the ones on
the fast cycle:

| | data per check | 20 accounts, per month |
|---|---|---|
| account is down, 5 min cycle | ~1 KB | ~0.17 GB (~$1.00 at $6/GB) |
| account is down, 1 min cycle | ~1 KB | ~0.86 GB (~$5.20) |
| account is live, 6 h cycle | ~15 KB | ~0.02 GB (~$0.12) |

The asymmetry is deliberate. A suspended account coming back is the event
you're waiting for, so those are polled on `CHECK_INTERVAL_SECONDS` (default 5
min). A live account going down is worth knowing but not worth paying to watch,
so those go on `LIVE_CHECK_SECONDS` (default 6 h) — a full profile is ~15× the
bytes of a 404.

Dropping the down-account cycle from 60s to 5 min is the single biggest lever
on the bill, and it costs almost nothing in practice: reinstatements take days
or weeks, so hearing about one four minutes later changes nothing. It also cuts
the request rate by 5×, which matters because Instagram rate limits a
watchlist of twenty accounts polled every minute.

`/uptime` reports data used and projects monthly GB and cost. Watch it for
the first day rather than trusting the estimate above.

**If the proxy runs out of data** every request fails with `ProxyError` — the
exit-IP probe in `/diag` fails too, which is how you tell it apart from
Instagram blocking you. The bot messages the owner when this happens, since
silence would otherwise look identical to "all accounts still down". It also
attempts one direct (unproxied) request as a long shot; set
`ALLOW_DIRECT_FALLBACK=0` to disable that.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and grab the token.
2. Copy `.env.example` to `.env`, fill in `BOT_TOKEN` and `OWNER_CHAT_ID`.
3. Install and run:

   ```bash
   python3 -m venv venv && source venv/bin/activate
   pip install -r requirements.txt
   export $(cat .env | xargs)
   python bot.py
   ```

Long polling, so it only needs outbound internet — no public URL or webhook.

## Deploying

### Railway (or any container host)

Connect the repo and deploy as a worker (uses the included `Procfile`). Set
the environment variables, then **mount a volume at `/data` and set
`DB_PATH=/data/watchlist.db`**.

That volume is not optional. Without it the database lives in the container
filesystem, which is rebuilt on every deploy — so shipping any code change
silently wipes every tracked account. The bot logs its database path and
watch count at startup and warns loudly when it isn't on a volume:

```
storage: /data/watchlist.db — 3 watch(es) across 1 chat(s)
```

### VPS

```bash
apt update && apt install -y python3-venv git
useradd --system --create-home --shell /usr/sbin/nologin igwatch

git clone <your-repo-url> /opt/ig-watch-bot
cd /opt/ig-watch-bot
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

cp .env.example .env
nano .env
chown -R igwatch:igwatch /opt/ig-watch-bot

cp deploy/ig-watch-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ig-watch-bot
journalctl -u ig-watch-bot -f
```

`Restart=on-failure` brings it back after a crash; `enable` starts it on
boot. To update: `git pull && systemctl restart ig-watch-bot`.

### Docker

```bash
docker build -t ig-watch-bot .
docker run -d --env-file .env -v $(pwd)/data:/data ig-watch-bot
```

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | *required* | Telegram bot token from BotFather |
| `OWNER_CHAT_ID` | *(first allowed ID)* | Chat allowed to grant/revoke access |
| `DB_PATH` | `/data/watchlist.db` if mounted, else `watchlist.db` | SQLite file |
| `PROXY_URL` | *(none)* | Residential proxy for all Instagram requests |
| `CHECK_INTERVAL_SECONDS` | `300` | How often down accounts are rechecked |
| `LIVE_CHECK_SECONDS` | `21600` | How often live accounts are rechecked (6h) |
| `PROXY_COST_PER_GB` | `6.00` | Your real per-GB rate, for the `/uptime` projection |
| `CONFIRM_CHECKS` | `2` | Agreeing checks needed before announcing a change |
| `API_ATTEMPTS` | `6` | API retries across exit IPs before falling back to the page |
| `CHECK_ATTEMPTS` | `1` | Page fetch attempts after the API gives up |
| `PAGE_READ_LIMIT` | `98304` | Bytes of the page to read before aborting the transfer |
| `ALLOWED_CHAT_IDS` | *(none)* | Static allowlist, comma-separated |
| `MAX_GUEST_USERS` | `5` | How many people the owner can invite |
| `ALLOW_DIRECT_FALLBACK` | `1` | Try unproxied when the proxy is unreachable |

## Tests

```bash
python test_bot.py
```

220 checks, no test dependencies. Covers status transitions and the debounce,
alert delivery and targeting, watchlist isolation between users, the access
model (including that guests can't grant themselves access), every kind of
Instagram response, cost shortcuts, and database upgrades from older schemas.

Worth running before any deploy — several of these exist because the
behaviour they check was once broken in a way that silently lost alerts.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `ProxyError` everywhere, `/diag` can't get an exit IP | Proxy provider out of data, or bad credentials |
| `no_profile_data` | Instagram served the empty shell, the API refused on every exit IP, and the HEAD probe didn't return 404 either. Usually resolves on the next cycle — the exit IPs are different each time |
| `login_wall` | That exit IP was asked to log in. Retried automatically |
| `rate_limited` | Throttled; the bot backs off and ends the cycle early |
| `/list` empty after a deploy | The database wasn't on a mounted volume |
| Everything reads Unknown | Check `/uptime` and `/diag` — usually the proxy |
