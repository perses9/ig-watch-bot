import asyncio
import html
import logging
import os
import random
import time
from datetime import datetime
from functools import wraps

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import storage
from ig_checker import (
    bytes_used,
    check_instagram_status,
    diagnose,
    make_client,
    proxy_status,
    warm_up_client,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ig-watch-bot")

BOT_COMMANDS = [
    BotCommand("watch", "Track one or more Instagram accounts"),
    BotCommand("check", "Check a username's status right now"),
    BotCommand("list", "Show tracked accounts, status, and mute state"),
    BotCommand("remove", "Stop tracking an account"),
    BotCommand("pause", "Mute notifications for an account"),
    BotCommand("resume", "Unmute a paused account"),
    BotCommand("uptime", "Show bot uptime and check count"),
    BotCommand("myid", "Show your chat ID (needed to be granted access)"),
    BotCommand("help", "Show usage"),
]

BOT_TOKEN = os.environ["BOT_TOKEN"]
# How often DOWN accounts are rechecked. This is where nearly all the request
# volume goes, since most of a watchlist is usually down. 5 minutes rather
# than 1: you're waiting on a reinstatement that takes days or weeks, so
# hearing about it 4 minutes later costs nothing, and it cuts both the proxy
# bill and the request rate that gets you rate limited by a factor of 5.
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "300"))
CONFIRM_CHECKS = int(os.environ.get("CONFIRM_CHECKS", "2"))
# Order is preserved: the fallback owner is whoever is listed first, which
# would be wrong if this were a set - group chat IDs are large negative
# numbers, so picking the smallest would hand ownership to a group.
ALLOWED_CHAT_ID_LIST = [
    int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if x
]
ALLOWED_CHAT_IDS = set(ALLOWED_CHAT_ID_LIST)


def _resolve_owner():
    """The owner can grant and revoke access, and never counts against the
    guest limit. Falls back to the first ALLOWED_CHAT_IDS entry so an existing
    deployment keeps working without having to set a new variable first."""
    raw = os.environ.get("OWNER_CHAT_ID", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning("OWNER_CHAT_ID=%r is not a number, ignoring it", raw)
    return ALLOWED_CHAT_ID_LIST[0] if ALLOWED_CHAT_ID_LIST else None


OWNER_CHAT_ID = _resolve_owner()
MAX_GUEST_USERS = int(os.environ.get("MAX_GUEST_USERS", "5"))
MAX_WATCH_PER_MESSAGE = 10
# Re-fetching Instagram's homepage for fresh cookies on every single check is
# wasteful, especially through a metered proxy - reuse one warm client and
# only refresh its cookies this often instead of every check cycle.
COOKIE_REFRESH_SECONDS = 1800
# Don't let someone who's been denied keep pinging the owner.
ACCESS_REQUEST_COOLDOWN = 3600
_access_requests = {}
# Infrastructure problems are worth telling the owner about, but not
# every cycle.
OWNER_ALERT_COOLDOWN = 3600
_owner_alerts = {}
# An account that's already live is checked far less often than one that's
# down. The event worth catching quickly is a suspended account coming back;
# a live account going down is worth knowing but not worth polling for every
# minute - and it's the expensive direction, since the API returns a full
# profile for a live account and a tiny 404 for a missing one.
LIVE_CHECK_SECONDS = int(os.environ.get("LIVE_CHECK_SECONDS", "21600"))
# What the proxy provider charges, used only to turn projected GB into a
# number that means something. Use the rate on the tier you actually buy, not
# the headline price: IPRoyal advertises $1.75/GB but that's a bulk rate, and
# small orders run $6-7/GB. Guessing low here is worse than not showing a
# figure at all.
PROXY_COST_PER_GB = float(os.environ.get("PROXY_COST_PER_GB", "6.00"))
_last_checked = {}
# Where the next cycle should start, when the last one exited early. See
# cycle_order().
_resume_after = None

START_TIME = time.monotonic()
CHECKS_RUN = 0

STATUS_LABELS = {
    "live": "🟢 <b>Live</b>",
    "not_found": "🔴 <b>Down</b> (suspended, banned, or deactivated)",
    "unknown": "⚪️ <b>Unknown</b> (no confirmed check yet)",
}

HELP_TEXT = (
    "👁️ <b>Instagram Watcher</b>\n"
    "Tracks Instagram accounts and pings you the moment their status changes.\n\n"
    "<b>Commands</b>\n"
    "/watch <code>user1 user2 ...</code> — start tracking one or more accounts\n"
    "/check <code>user</code> — check status right now, no waiting\n"
    "/list — see everything you're tracking, with quick-action buttons\n"
    "/remove <code>user</code> — stop tracking an account\n"
    "/pause <code>user</code> / /resume <code>user</code> — mute or unmute alerts\n"
    "/uptime — bot health and stats\n"
    "/myid — show your chat ID\n\n"
    f"⏱ Rechecked every <b>{CHECK_INTERVAL_SECONDS}s</b>. A change is only announced after "
    f"<b>{CONFIRM_CHECKS}</b> checks in a row agree, to avoid false alarms."
)

OWNER_HELP_TEXT = (
    "\n\n<b>Owner commands</b>\n"
    "/users — see who has access, with a button to revoke\n"
    "/adduser <code>chat_id [name]</code> — grant access "
    f"(up to {MAX_GUEST_USERS} people)\n"
    "/removeuser <code>chat_id</code> — revoke access\n"
    "/allwatches — every account being polled, and whose list it's on\n"
    "/diag <code>user</code> — show what Instagram actually returns\n\n"
    "<i>When someone messages the bot without access, you get a request here "
    "with a button to approve them — no chat IDs to copy around.</i>\n"
    "<i>Guests each have a private watchlist and can't see yours or each "
    "other's. You can see all of them via /allwatches — they're polled with "
    "your proxy data, so they're your bill.</i>"
)


def help_for(chat_id: int) -> str:
    return HELP_TEXT + (OWNER_HELP_TEXT if is_owner(chat_id) else "")


def fmt(username: str) -> str:
    return f"<code>@{html.escape(username)}</code>"


def clean_username(raw: str) -> str:
    return raw.strip().lstrip("@").lower()


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


ERROR_EXPLANATIONS = {
    "ProxyError": (
        "couldn't reach the proxy — usually means the IPRoyal balance is used up, "
        "or the PROXY_URL credentials are wrong"
    ),
    "ConnectionError": "network problem reaching Instagram",
    "ConnectTimeout": "the proxy didn't respond in time",
    "ReadTimeout": "Instagram didn't respond in time",
    "Timeout": "the request timed out",
    "rate_limited": "Instagram is throttling us right now",
    "login_wall": "Instagram demanded a login for this request",
    "no_profile_data": "Instagram returned a page with no profile data in it",
    "bad_json": "Instagram returned something unreadable",
}


def explain_error(error: str) -> str:
    """Turn an internal error name into something actionable. These strings
    surface directly to users, and 'ProxyError' on its own tells them nothing
    about the thing they'd actually need to go and fix."""
    if not error:
        return "unknown problem"
    if error in ERROR_EXPLANATIONS:
        return ERROR_EXPLANATIONS[error]
    if error.startswith("http_") or error.startswith("head_"):
        return f"Instagram answered {error.split('_', 1)[1]}"
    return error


def profile_summary(state: dict) -> str:
    bits = []
    if state.get("full_name"):
        bits.append(html.escape(state["full_name"]))
    if state.get("follower_count") is not None:
        bits.append(f"{state['follower_count']:,} followers")
    if state.get("is_private"):
        bits.append("private")
    return f"\n<i>{' · '.join(bits)}</i>" if bits else ""


def watch_keyboard(username: str, paused: bool) -> InlineKeyboardMarkup:
    mute_button = (
        InlineKeyboardButton("🔊 Unmute", callback_data=f"resume:{username}")
        if paused
        else InlineKeyboardButton("🔇 Mute", callback_data=f"pause:{username}")
    )
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔍 Check now", callback_data=f"check:{username}"), mute_button],
            [InlineKeyboardButton("🗑 Remove", callback_data=f"remove:{username}")],
        ]
    )


async def get_warm_client(context: ContextTypes.DEFAULT_TYPE):
    bot_data = context.application.bot_data
    client = bot_data["http_client"]
    warmed_at = bot_data.get("cookies_warmed_at")
    if warmed_at is None or time.monotonic() - warmed_at > COOKIE_REFRESH_SECONDS:
        await warm_up_client(client)
        bot_data["cookies_warmed_at"] = time.monotonic()
    return client


def is_owner(chat_id: int) -> bool:
    return OWNER_CHAT_ID is not None and chat_id == OWNER_CHAT_ID


def env_guest_ids() -> set:
    """People granted access by the ALLOWED_CHAT_IDS setting rather than by
    invite. They can't be revoked from chat, but they do occupy a slot."""
    return ALLOWED_CHAT_IDS - {OWNER_CHAT_ID}


def used_guest_slots() -> int:
    return storage.count_allowed_users() + len(env_guest_ids())


def is_authorized(chat_id: int) -> bool:
    # With no owner and no allowlist configured the bot is open to anyone -
    # same as before access control existed. Setting either one locks it down.
    if OWNER_CHAT_ID is None and not ALLOWED_CHAT_IDS:
        return True
    if is_owner(chat_id) or chat_id in ALLOWED_CHAT_IDS:
        return True
    return storage.is_allowed_user(chat_id)


async def notify_owner_of_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward an access request to the owner with approve/deny buttons, so
    granting access is a tap rather than copying chat IDs between people."""
    if OWNER_CHAT_ID is None:
        return

    chat_id = update.effective_chat.id
    last_asked = _access_requests.get(chat_id)
    if last_asked is not None and time.monotonic() - last_asked < ACCESS_REQUEST_COOLDOWN:
        return  # already asked recently; don't let someone spam the owner
    _access_requests[chat_id] = time.monotonic()

    user = update.effective_user
    who = html.escape(user.full_name if user else "Someone")
    handle = f" (@{html.escape(user.username)})" if user and user.username else ""

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Grant access", callback_data=f"grant:{chat_id}"),
            InlineKeyboardButton("❌ Deny", callback_data=f"deny:{chat_id}"),
        ]]
    )
    try:
        await context.bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text=(
                f"🔔 <b>Access request</b>\n"
                f"{who}{handle}\n"
                f"chat ID: <code>{chat_id}</code>\n\n"
                f"{used_guest_slots()} of {MAX_GUEST_USERS} slots in use."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except Exception:
        logger.exception("couldn't forward an access request to the owner")


def restricted(handler):
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update.effective_chat.id):
            await update.effective_message.reply_text(
                "🔒 You don't have access to this bot yet.\n"
                "I've let the owner know — you'll get a message here if they approve."
            )
            await notify_owner_of_request(update, context)
            return
        return await handler(update, context)

    return wrapper


def owner_only(handler):
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update.effective_chat.id):
            await update.effective_message.reply_text("🔒 Only the bot's owner can manage access.")
            return
        return await handler(update, context)

    return wrapper


@restricted
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        help_for(update.effective_chat.id)
        + "\n\n👇 Try <code>/watch instagram</code> to see it in action.",
        parse_mode=ParseMode.HTML,
    )


@restricted
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(help_for(update.effective_chat.id), parse_mode=ParseMode.HTML)


@restricted
async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /watch <username> [username2 ...]")
        return

    usernames = [clean_username(u) for u in context.args[:MAX_WATCH_PER_MESSAGE]]
    chat_id = update.effective_chat.id

    client = await get_warm_client(context)
    for username in usernames:
        result = await check_instagram_status(username, client)

        # Track it either way. Instagram's logged-out responses are flaky, and
        # refusing to add an account just because one check was inconclusive
        # means the background loop never gets a chance to sort it out.
        storage.add_watch(chat_id, username)

        if result.status is None:
            await update.message.reply_text(
                f"✅ Now tracking {fmt(username)}\n"
                f"⚪️ <b>Unknown</b> — {html.escape(explain_error(result.error))}.\n"
                f"Retrying every {CHECK_INTERVAL_SECONDS}s.",
                parse_mode=ParseMode.HTML,
                reply_markup=watch_keyboard(username, paused=False),
            )
            continue

        # Goes through the same transition logic as the background loop rather
        # than writing the status directly: someone adding an account that
        # others already track must not clobber a pending change or skip the
        # alert that change was about to produce.
        await apply_check_result(context, username, result)
        state = storage.get_state(username)
        await update.message.reply_text(
            f"✅ Now tracking {fmt(username)}\n{STATUS_LABELS[result.status]}{profile_summary(state)}",
            parse_mode=ParseMode.HTML,
            reply_markup=watch_keyboard(username, paused=False),
        )


@restricted
async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /check <username>")
        return
    username = clean_username(context.args[0])

    client = await get_warm_client(context)
    result = await check_instagram_status(username, client)

    if result.status is None:
        await update.message.reply_text(
            f"⚠️ Couldn't verify {fmt(username)} — {html.escape(explain_error(result.error))}.",
            parse_mode=ParseMode.HTML,
        )
        return

    summary = profile_summary(vars(result))
    await update.message.reply_text(
        f"{fmt(username)}\n{STATUS_LABELS[result.status]}{summary}\n\n"
        "<i>One-off check — doesn't affect your tracked list.</i>",
        parse_mode=ParseMode.HTML,
    )


@restricted
async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /remove <username>")
        return
    username = clean_username(context.args[0])
    chat_id = update.effective_chat.id
    if storage.remove_watch(chat_id, username):
        await update.message.reply_text(f"🗑 Stopped tracking {fmt(username)}.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"You weren't tracking {fmt(username)}.", parse_mode=ParseMode.HTML)


@restricted
async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /pause <username>")
        return
    username = clean_username(context.args[0])
    chat_id = update.effective_chat.id
    if storage.set_paused(chat_id, username, True):
        await update.message.reply_text(f"🔇 Muted {fmt(username)} (still tracked).", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"You weren't tracking {fmt(username)}.", parse_mode=ParseMode.HTML)


@restricted
async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /resume <username>")
        return
    username = clean_username(context.args[0])
    chat_id = update.effective_chat.id
    if storage.set_paused(chat_id, username, False):
        await update.message.reply_text(f"🔊 Unmuted {fmt(username)}.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"You weren't tracking {fmt(username)}.", parse_mode=ParseMode.HTML)


async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Deliberately not @restricted: someone who hasn't been granted access yet
    # needs this to find the ID they must send to the owner.
    chat_id = update.effective_chat.id
    status = "✅ You have access." if is_authorized(chat_id) else "🔒 You don't have access yet."
    await update.message.reply_text(
        f"Your chat ID is <code>{chat_id}</code>\n{status}",
        parse_mode=ParseMode.HTML,
    )


@owner_only
async def adduser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: /adduser <chat_id> [name]\n"
            "Ask the person to send /myid to this bot and give you the number."
        )
        return

    try:
        new_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "That doesn't look like a chat ID — it should be a number, e.g. /adduser 1363317234"
        )
        return

    if is_owner(new_id):
        await update.message.reply_text("That's your own ID — you already have full access.")
        return

    if storage.is_allowed_user(new_id) or new_id in ALLOWED_CHAT_IDS:
        await update.message.reply_text("That person already has access.")
        return

    if used_guest_slots() >= MAX_GUEST_USERS:
        await update.message.reply_text(
            f"⚠️ You've reached the limit of {MAX_GUEST_USERS} people.\n"
            "Use /users to see who has access, and /removeuser &lt;chat_id&gt; to free up a slot.",
            parse_mode=ParseMode.HTML,
        )
        return

    label = " ".join(context.args[1:]).strip() or None
    storage.add_allowed_user(new_id, label)
    used = used_guest_slots()
    who = f" ({html.escape(label)})" if label else ""
    await update.message.reply_text(
        f"✅ Access granted to <code>{new_id}</code>{who}\n"
        f"{used} of {MAX_GUEST_USERS} slots used.",
        parse_mode=ParseMode.HTML,
    )


@owner_only
async def removeuser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removeuser <chat_id>   (see /users)")
        return

    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("That doesn't look like a chat ID — it should be a number.")
        return

    if storage.remove_allowed_user(target):
        used = used_guest_slots()
        await update.message.reply_text(
            f"🚫 Access revoked for <code>{target}</code>, and their tracked accounts were removed.\n"
            f"{used} of {MAX_GUEST_USERS} slots used.",
            parse_mode=ParseMode.HTML,
        )
    elif target in ALLOWED_CHAT_IDS:
        # Granted by the ALLOWED_CHAT_IDS setting, so there's no database row
        # to delete - saying "didn't have access" would be a lie, since they
        # still do.
        await update.message.reply_text(
            f"⚠️ <code>{target}</code> was granted access by the <code>ALLOWED_CHAT_IDS</code> "
            "setting, so I can't revoke it from here. Remove them from that variable in your "
            "hosting dashboard instead.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text("That chat ID didn't have access.")


@owner_only
async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = storage.list_allowed_users()
    await update.message.reply_text(
        f"👥 <b>Access</b> — {used_guest_slots()} of {MAX_GUEST_USERS} slots used\n"
        f"👑 <code>{OWNER_CHAT_ID}</code> — you (owner)",
        parse_mode=ParseMode.HTML,
    )

    for user in users:
        label = f" — {html.escape(user['label'])}" if user.get("label") else ""
        tracked = len(storage.list_watches(user["chat_id"]))
        await update.message.reply_text(
            f"<code>{user['chat_id']}</code>{label}\ntracking {tracked} account(s)",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🚫 Revoke access", callback_data=f"revoke:{user['chat_id']}")]]
            ),
        )

    for chat_id in sorted(env_guest_ids()):
        tracked = len(storage.list_watches(chat_id))
        await update.message.reply_text(
            f"<code>{chat_id}</code> — granted by the ALLOWED_CHAT_IDS setting\n"
            f"tracking {tracked} account(s)\n"
            "<i>Remove them from that variable to revoke.</i>",
            parse_mode=ParseMode.HTML,
        )

    if not users and not env_guest_ids():
        await update.message.reply_text(
            "No one else has access yet. When someone messages the bot you'll get a "
            "request here with a button to approve them — or use /adduser &lt;chat_id&gt;.",
            parse_mode=ParseMode.HTML,
        )


@owner_only
async def allwatches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Every account being polled, and who it belongs to.

    /list is scoped to the asking chat, which is right for guests but leaves
    the owner unable to see what their proxy data is being spent on - only a
    total that doesn't match their own list."""
    rows = storage.all_watches_detailed()
    if not rows:
        await update.message.reply_text("Nothing is being tracked by anyone.")
        return

    labels = {u["chat_id"]: u.get("label") for u in storage.list_allowed_users()}
    grouped = {}
    for row in rows:
        grouped.setdefault(row["chat_id"], []).append(row)

    unique = len({r["username"] for r in rows})
    await update.message.reply_text(
        f"📋 <b>Everything being polled</b>\n"
        f"{len(rows)} watch(es) · {unique} unique account(s) · {len(grouped)} chat(s)\n"
        "<i>Unique accounts is what costs data — two people watching the same "
        "username is still one check.</i>",
        parse_mode=ParseMode.HTML,
    )

    for chat_id, watches in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        if chat_id == update.effective_chat.id:
            who = "👑 you"
        elif labels.get(chat_id):
            who = f"👤 {html.escape(str(labels[chat_id]))}"
        else:
            who = f"👤 chat {chat_id}"
        if chat_id != OWNER_CHAT_ID and chat_id not in ALLOWED_CHAT_IDS and chat_id not in labels:
            who += " ⚠️ <b>no longer has access</b>"

        lines = [f"{who} — <code>{chat_id}</code> · {len(watches)} account(s)"]
        for w in watches:
            status = storage.get_state(w["username"])["confirmed_status"] or "unknown"
            icon = {"live": "🟢", "not_found": "🔴"}.get(status, "⚪️")
            lines.append(f"{icon} {fmt(w['username'])}{' 🔇' if w['paused'] else ''}")
        # Telegram rejects anything over 4096 characters, and a long
        # watchlist is exactly when this command matters most.
        for chunk in chunk_lines(lines):
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)


def chunk_lines(lines: list, limit: int = 3500) -> list:
    """Group lines into messages Telegram will accept."""
    chunks, current, size = [], [], 0
    for line in lines:
        if current and size + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


@owner_only
async def diag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows what Instagram actually returns, so a failing check can be
    diagnosed from real responses rather than guessed at."""
    if not context.args:
        await update.message.reply_text("Usage: /diag <username>")
        return

    username = clean_username(context.args[0])
    await update.message.reply_text(f"🔬 Probing {fmt(username)}…", parse_mode=ParseMode.HTML)

    client = await get_warm_client(context)
    proxy = await proxy_status(client)
    reports = await diagnose(username, client)
    result = await check_instagram_status(username, client)

    lines = [f"🔬 <b>Diagnostics for {fmt(username)}</b>", ""]
    if proxy["configured"]:
        lines.append(f"🌐 proxy: <b>on</b> · <code>{html.escape(proxy.get('endpoint', '?'))}</code>")
    else:
        lines.append("🌐 proxy: <b>OFF</b> — requests come from the server's own IP")
    if proxy.get("exit_ip"):
        lines.append(f"  exit IP: <code>{html.escape(proxy['exit_ip'])}</code>")
    elif proxy.get("exit_ip_error"):
        lines.append(f"  exit IP unknown: <code>{proxy['exit_ip_error']}</code>")
        if proxy.get("exit_ip_detail"):
            lines.append(f"  ↳ {html.escape(proxy['exit_ip_detail'])}")
    for report in reports:
        lines.append(f"\n<b>{report['persona']}</b>")
        if report.get("head_verdict") is not None:
            lines.append(f"  verdict: <code>{html.escape(str(report['head_verdict']))}</code>")
            continue
        if report.get("error"):
            lines.append(f"  request failed: <code>{report['error']}</code>")
            if report.get("error_detail"):
                lines.append(f"  ↳ {html.escape(report['error_detail'])}")
            continue
        lines.append(f"  HTTP <b>{report['status']}</b> · {report['bytes']:,} bytes")
        if report.get("location"):
            lines.append(f"  → <code>{html.escape(str(report['location'])[:80])}</code>")
        if report.get("title"):
            lines.append(f"  title: <code>{html.escape(report['title'])}</code>")
        lines.append(
            f"  og:title {'✅' if report['og_title'] else '❌'} · "
            f"og:desc {'✅' if report['og_description'] else '❌'}"
        )
        lines.append(
            f"  embedded JSON: username {'✅' if report.get('json_username_match') else '❌'} · "
            f"name {'✅' if report.get('json_full_name') else '❌'} · "
            f"followers {'✅' if report.get('json_followers') else '❌'} "
            f"({report.get('json_usernames_seen', 0)} usernames in page)"
        )
        if report.get("not_found_marker"):
            lines.append(f"  not-found marker: <code>{html.escape(report['not_found_marker'])}</code>")
        if report.get("login_marker"):
            lines.append(f"  login marker: <code>{html.escape(report['login_marker'])}</code>")
        if report.get("text"):
            lines.append(f"  text: <code>{html.escape(report['text'][:250])}</code>")

    verdict = result.status or f"inconclusive ({result.error})"
    lines.append(f"\n<b>Verdict:</b> {html.escape(verdict)}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@restricted
async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    watches = storage.list_watches(chat_id)
    if not watches:
        await update.message.reply_text(
            "You're not tracking any accounts yet. Use /watch <username> to add one."
        )
        return

    await update.message.reply_text(f"👁 <b>Tracking {len(watches)} account(s)</b>", parse_mode=ParseMode.HTML)
    for w in watches:
        state = storage.get_state(w["username"])
        status = state["confirmed_status"] or "unknown"
        mute_tag = " · 🔇 muted" if w["paused"] else ""
        await update.message.reply_text(
            f"{fmt(w['username'])}\n{STATUS_LABELS.get(status, status)}{profile_summary(state)}{mute_tag}",
            parse_mode=ParseMode.HTML,
            reply_markup=watch_keyboard(w["username"], paused=w["paused"]),
        )


@restricted
async def uptime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    elapsed = time.monotonic() - START_TIME
    used = bytes_used()
    hours = max(elapsed / 3600, 1 / 60)
    per_day = used / hours * 24

    per_month_gb = per_day * 30 / 1073741824
    tracked = storage.all_watched_usernames()
    live = sum(1 for u in tracked if storage.get_state(u)["confirmed_status"] == "live")

    lines = [
        "🤖 <b>Bot status</b>",
        f"Uptime: {format_duration(elapsed)}",
        f"Checks run this session: {CHECKS_RUN}",
        f"Tracking {len(tracked)} account(s) — {len(tracked) - live} down, {live} live",
        "",
        f"⏱ Down accounts checked every {CHECK_INTERVAL_SECONDS}s",
        f"⏱ Live accounts checked every {format_duration(LIVE_CHECK_SECONDS)}",
        "",
        f"📶 Proxy data this session: <b>{used / 1048576:.1f} MB</b>",
        f"Projected: ~{per_month_gb:.2f} GB/month "
        f"(about ${per_month_gb * PROXY_COST_PER_GB:.2f} at ${PROXY_COST_PER_GB:g}/GB)",
    ]
    if elapsed < 900:
        lines.append("<i>Projection is rough until the bot has run a while.</i>")

    # The count above is every account being polled, which is not the same as
    # the owner's own list - and the difference is what they're paying for.
    # Left unexplained it just reads as a wrong number.
    if is_owner(update.effective_chat.id):
        by_chat = storage.watch_counts_by_chat()
        if len(by_chat) > 1:
            labels = {u["chat_id"]: u.get("label") for u in storage.list_allowed_users()}
            lines.append("\n<b>Who's tracking what</b>")
            for entry in by_chat:
                cid = entry["chat_id"]
                if cid == update.effective_chat.id:
                    who = "you"
                elif labels.get(cid):
                    who = html.escape(str(labels[cid]))
                else:
                    who = f"chat {cid}"
                # A list belonging to nobody still costs money every cycle.
                orphan = (
                    cid != OWNER_CHAT_ID
                    and cid not in ALLOWED_CHAT_IDS
                    and cid not in labels
                )
                flag = " ⚠️ no longer has access" if orphan else ""
                lines.append(f"  {who}: {entry['count']}{flag}")
            if any(
                e["chat_id"] != OWNER_CHAT_ID
                and e["chat_id"] not in ALLOWED_CHAT_IDS
                and e["chat_id"] not in labels
                for e in by_chat
            ):
                lines.append(
                    "<i>⚠️ lists are still polled after access is revoked via "
                    "ALLOWED_CHAT_IDS. /removeuser &lt;chat_id&gt; clears one.</i>"
                )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def edit_result_message(query, text: str, reply_markup=None):
    """Update the message a button lives on.

    The back-online alert is sent as a photo, and Telegram refuses
    editMessageText on a media message — so the buttons on the single most
    important message the bot sends did nothing at all. Edit the caption in
    that case. Re-tapping a button that produces identical text is also a
    Telegram error, and a harmless one."""
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        return
    except BadRequest as exc:
        detail = str(exc).lower()
        if "message is not modified" in detail:
            return
        if "no text in the message" not in detail:
            logger.warning("couldn't edit message: %s", exc)
            return

    try:
        await query.edit_message_caption(caption=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.warning("couldn't edit caption: %s", exc)


async def button_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id

    if not is_authorized(chat_id):
        await query.answer("Not authorized.", show_alert=True)
        return

    action, _, username = query.data.partition(":")

    # Access decisions are the owner's alone — checked separately from the
    # general authorisation above, since a guest passes that check too.
    if action in ("grant", "deny", "revoke"):
        if not is_owner(chat_id):
            await query.answer("Only the owner can manage access.", show_alert=True)
            return
        try:
            target = int(username)
        except ValueError:
            await query.answer()
            return

        if action == "deny":
            await query.answer("Denied")
            await edit_result_message(query, f"❌ Denied access to <code>{target}</code>.")
            return

        if action == "revoke":
            storage.remove_allowed_user(target)
            await query.answer("Access revoked")
            await edit_result_message(
                query,
                f"🚫 Revoked <code>{target}</code>, and removed their tracked accounts.\n"
                f"{used_guest_slots()} of {MAX_GUEST_USERS} slots in use.",
            )
            return

        if storage.is_allowed_user(target) or target in ALLOWED_CHAT_IDS:
            await query.answer("They already have access")
            await edit_result_message(query, f"✅ <code>{target}</code> already has access.")
            return

        if used_guest_slots() >= MAX_GUEST_USERS:
            await query.answer("No slots left", show_alert=True)
            await edit_result_message(
                query,
                f"⚠️ All {MAX_GUEST_USERS} slots are in use. Free one with /users first.",
            )
            return

        storage.add_allowed_user(target)
        await query.answer("Access granted")
        await edit_result_message(
            query,
            f"✅ Granted access to <code>{target}</code>.\n"
            f"{used_guest_slots()} of {MAX_GUEST_USERS} slots in use.",
        )
        try:
            await context.bot.send_message(
                chat_id=target,
                text="✅ You've been granted access. Send /help to get started.",
            )
        except Exception:
            logger.info("granted %s but couldn't message them", target)
        return

    if action == "remove":
        storage.remove_watch(chat_id, username)
        await query.answer(f"Removed @{username}")
        await edit_result_message(query, f"🗑 Stopped tracking {fmt(username)}.")
        return

    if action == "pause":
        storage.set_paused(chat_id, username, True)
        await query.answer(f"Muted @{username}")
    elif action == "resume":
        storage.set_paused(chat_id, username, False)
        await query.answer(f"Unmuted @{username}")
    elif action == "check":
        await query.answer("Checking...")
        client = await get_warm_client(context)
        result = await check_instagram_status(username, client)
        # Same transition logic as everywhere else. Writing the status
        # directly here used to consume the pending change, so a manual check
        # that happened to catch the recovery meant nobody was ever told.
        await apply_check_result(context, username, result)
        if result.status is None:
            await query.answer(explain_error(result.error), show_alert=True)
    else:
        await query.answer()
        return

    state = storage.get_state(username)
    status = state["confirmed_status"] or "unknown"
    watches = storage.list_watches(chat_id)
    paused = next((w["paused"] for w in watches if w["username"] == username), False)
    mute_tag = " · 🔇 muted" if paused else ""
    await edit_result_message(
        query,
        f"{fmt(username)}\n{STATUS_LABELS.get(status, status)}{profile_summary(state)}{mute_tag}",
        reply_markup=watch_keyboard(username, paused=paused),
    )


async def notify_watchers(
    context: ContextTypes.DEFAULT_TYPE,
    username: str,
    old_status: str,
    new_status: str,
    down_since: str = None,
):
    """down_since has to be passed in by the caller, read *before* the status
    was updated: recording a live status clears the column, so by the time we
    get here there is nothing left to measure the downtime against."""
    state = storage.get_state(username)
    summary = profile_summary(state)

    if new_status == "live" and old_status == "not_found":
        downtime = ""
        if down_since:
            try:
                went_down = datetime.fromisoformat(down_since)
                downtime_seconds = (datetime.utcnow() - went_down).total_seconds()
                downtime = f" after {format_duration(downtime_seconds)}"
            except ValueError:
                pass
        text = (
            f"🚨 <b>{fmt(username)} is BACK ONLINE</b>{downtime}!{summary}\n\n"
            f"https://instagram.com/{username}"
        )
    elif new_status == "not_found":
        text = f"⚠️ {fmt(username)} <b>is no longer reachable</b> (banned, suspended, or deactivated).{summary}"
    else:
        text = f"ℹ️ {fmt(username)} status changed to {STATUS_LABELS.get(new_status, new_status)}{summary}"

    keyboard = watch_keyboard(username, paused=False)
    photo = state.get("profile_pic_url") if new_status == "live" else None

    for chat_id in storage.chats_watching(username, only_unpaused=True):
        if photo:
            try:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
                continue
            except Exception:
                # Instagram's CDN URLs are signed and expire, so Telegram
                # often can't fetch them. The alert matters far more than the
                # picture — fall through and send it as plain text.
                logger.warning("couldn't attach photo for %s, sending text instead", username)

        try:
            await context.bot.send_message(
                chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )
        except Exception:
            logger.exception("failed to notify chat %s about %s", chat_id, username)


def due_for_check(username: str) -> bool:
    """Down accounts are checked every cycle; live ones much less often.

    Waiting for a suspended account to come back is the whole point, so that
    direction stays fast. The reverse - a live account going down - still gets
    caught, just not within a minute, and skipping those checks is where
    nearly all the proxy bill goes: the API returns a full profile for a live
    account and a few hundred bytes for a missing one.
    """
    if storage.get_state(username)["confirmed_status"] != "live":
        return True
    last = _last_checked.get(username)
    return last is None or time.monotonic() - last >= LIVE_CHECK_SECONDS


async def alert_owner_once(context: ContextTypes.DEFAULT_TYPE, key: str, text: str):
    """Tell the owner about an infrastructure problem, at most hourly.

    Silence looks identical to "nothing has changed" — if checks are failing
    for everything, the owner needs to hear it rather than assume the
    accounts are simply still down."""
    if OWNER_CHAT_ID is None:
        return
    last = _owner_alerts.get(key)
    if last is not None and time.monotonic() - last < OWNER_ALERT_COOLDOWN:
        return
    _owner_alerts[key] = time.monotonic()
    try:
        await context.bot.send_message(chat_id=OWNER_CHAT_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("couldn't alert the owner")


async def apply_check_result(context: ContextTypes.DEFAULT_TYPE, username: str, result) -> bool:
    """Record a check result and announce it if it's a genuine status change.

    Every path that checks an account goes through here — the background loop
    and the "Check now" button alike. When the button wrote straight to
    storage instead, a manual check landing on the recovery would overwrite
    the stored status without telling anyone, and the background loop would
    then see no change left to report, silently eating the alert.

    Returns True if watchers were notified."""
    if result.status is None:
        logger.info("%s: check inconclusive (%s) — keeping last confirmed status", username, result.error)
        return False

    state = storage.get_state(username)
    confirmed = state["confirmed_status"]

    if result.status == confirmed:
        storage.clear_pending(username)
        return False

    # First time this account ever resolved (it was added while unverifiable):
    # record the baseline silently. Nothing changed, we simply learned where
    # it stands, and announcing that would just be noise.
    if confirmed in (None, "unknown"):
        storage.set_confirmed(username, result.status, vars(result))
        logger.info("%s: baseline status recorded as %s", username, result.status)
        return False

    _, pending_count = storage.bump_pending(username, result.status)
    if pending_count < CONFIRM_CHECKS:
        return False

    # Read before writing — set_confirmed clears down_since on recovery.
    down_since = state.get("down_since")
    storage.set_confirmed(username, result.status, vars(result))
    await notify_watchers(context, username, confirmed, result.status, down_since=down_since)
    return True


def cycle_order(usernames: list) -> list:
    """Start each cycle where the last one left off.

    A cycle can end early - the proxy is down, or Instagram rate limited us
    twice in a row - and it always walked the list in the same order, so the
    accounts near the end were never reached. Not "checked late": never
    checked, cycle after cycle, because the next cycle started from the top
    and hit the same wall in the same place. With a handful of accounts you'd
    never see it; with twenty and Instagram throttling, most of the watchlist
    is silently unmonitored.

    Rotating the start point turns that into an even delay for everyone
    instead of starvation for whoever sorts last."""
    if _resume_after is None or _resume_after not in usernames:
        return usernames
    cut = usernames.index(_resume_after) + 1
    return usernames[cut:] + usernames[:cut]


async def check_job(context: ContextTypes.DEFAULT_TYPE):
    global CHECKS_RUN, _resume_after
    usernames = storage.all_watched_usernames()
    if not usernames:
        return

    consecutive_blocks = 0
    client = await get_warm_client(context)
    # A cycle that runs to the end has no unfinished business; only an early
    # exit needs to hand a starting point to the next one.
    _resume_after = None

    for username in cycle_order(usernames):
        if not due_for_check(username):
            continue
        _last_checked[username] = time.monotonic()

        result = await check_instagram_status(username, client)
        CHECKS_RUN += 1
        await apply_check_result(context, username, result)

        if result.error == "ProxyError":
            # The proxy itself is unreachable, so no account can be checked -
            # stop the cycle instead of failing through the whole list.
            logger.error("proxy unreachable — check the provider's balance and credentials")
            await alert_owner_once(
                context,
                "proxy_down",
                "⚠️ <b>The proxy is unreachable.</b>\nChecks are failing for every account. "
                "This usually means the IPRoyal balance has run out — worth checking your "
                "dashboard.",
            )
            _resume_after = username
            break

        if result.error == "rate_limited":
            consecutive_blocks += 1
            # Give up on the cycle rather than sleeping it off in place. A long
            # sleep here stalls every other account behind it, and the next
            # cycle is only CHECK_INTERVAL_SECONDS away anyway.
            if consecutive_blocks >= 2:
                logger.warning("rate limited repeatedly, ending this cycle early")
                _resume_after = username
                break
            await asyncio.sleep(5)
        else:
            consecutive_blocks = 0
            # Small gap between accounts so a cycle doesn't arrive as one
            # burst. Kept short: overrunning the interval makes the scheduler
            # skip cycles, which quietly stretches the detection time.
            await asyncio.sleep(random.uniform(0.4, 1.2))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Without this, an exception in a handler is swallowed and the user just
    sees the bot do nothing."""
    logger.exception("unhandled error while processing %s", update, exc_info=context.error)


async def post_init(application: Application):
    application.bot_data["http_client"] = make_client()
    await warm_up_client(application.bot_data["http_client"])
    application.bot_data["cookies_warmed_at"] = time.monotonic()
    await application.bot.set_my_commands(BOT_COMMANDS)


async def post_shutdown(application: Application):
    client = application.bot_data.get("http_client")
    if client is not None:
        await client.close()


def main():
    storage.init_db()
    # Python 3.14 removed asyncio.get_event_loop()'s implicit loop creation, which
    # python-telegram-bot's run_polling() still relies on. Set one explicitly.
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("pause", pause_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("uptime", uptime_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))
    app.add_handler(CommandHandler("adduser", adduser_cmd))
    app.add_handler(CommandHandler("removeuser", removeuser_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("allwatches", allwatches_cmd))
    app.add_handler(CommandHandler("diag", diag_cmd))
    app.add_handler(CallbackQueryHandler(button_cmd))
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(check_job, interval=CHECK_INTERVAL_SECONDS, first=10)

    logger.info("bot starting, checking every %ss", CHECK_INTERVAL_SECONDS)
    app.run_polling()


if __name__ == "__main__":
    main()
