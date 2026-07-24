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
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import storage
from ig_checker import check_instagram_status, make_client, warm_up_client

BOT_COMMANDS = [
    BotCommand("watch", "Track one or more Instagram accounts"),
    BotCommand("check", "Check a username's status right now"),
    BotCommand("list", "Show tracked accounts, status, and mute state"),
    BotCommand("remove", "Stop tracking an account"),
    BotCommand("pause", "Mute notifications for an account"),
    BotCommand("resume", "Unmute a paused account"),
    BotCommand("uptime", "Show bot uptime and check count"),
    BotCommand("help", "Show usage"),
]

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "15"))
CONFIRM_CHECKS = int(os.environ.get("CONFIRM_CHECKS", "2"))
ALLOWED_CHAT_IDS = {
    int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if x
}
MAX_WATCH_PER_MESSAGE = 10
# Re-fetching Instagram's homepage for fresh cookies on every single check is
# wasteful, especially through a metered proxy - reuse one warm client and
# only refresh its cookies this often instead of every check cycle.
COOKIE_REFRESH_SECONDS = 1800

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ig-watch-bot")

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
    "/uptime — bot health and stats\n\n"
    f"⏱ Rechecked every <b>{CHECK_INTERVAL_SECONDS}s</b>. A change is only announced after "
    f"<b>{CONFIRM_CHECKS}</b> checks in a row agree, to avoid false alarms."
)


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


def restricted(handler):
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if ALLOWED_CHAT_IDS and update.effective_chat.id not in ALLOWED_CHAT_IDS:
            await update.effective_message.reply_text("You're not authorized to use this bot.")
            return
        return await handler(update, context)

    return wrapper


@restricted
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        HELP_TEXT + "\n\n👇 Try <code>/watch instagram</code> to see it in action.",
        parse_mode=ParseMode.HTML,
    )


@restricted
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


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
        if result.status is None:
            await update.message.reply_text(
                f"⚠️ Couldn't verify {fmt(username)} right now ({result.error}).",
                parse_mode=ParseMode.HTML,
            )
            continue
        storage.add_watch(chat_id, username)
        storage.set_confirmed(username, result.status, vars(result))
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
            f"⚠️ Couldn't verify {fmt(username)} right now ({result.error}). Try again in a moment.",
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
    await update.message.reply_text(
        f"🤖 <b>Bot status</b>\n"
        f"Uptime: {format_duration(elapsed)}\n"
        f"Checks run this session: {CHECKS_RUN}\n"
        f"Check interval: {CHECK_INTERVAL_SECONDS}s",
        parse_mode=ParseMode.HTML,
    )


async def button_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id

    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        await query.answer("Not authorized.", show_alert=True)
        return

    action, _, username = query.data.partition(":")

    if action == "remove":
        storage.remove_watch(chat_id, username)
        await query.answer(f"Removed @{username}")
        await query.edit_message_text(f"🗑 Stopped tracking {fmt(username)}.", parse_mode=ParseMode.HTML)
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
        if result.status is not None:
            storage.set_confirmed(username, result.status, vars(result))
    else:
        await query.answer()
        return

    state = storage.get_state(username)
    status = state["confirmed_status"] or "unknown"
    watches = storage.list_watches(chat_id)
    paused = next((w["paused"] for w in watches if w["username"] == username), False)
    mute_tag = " · 🔇 muted" if paused else ""
    await query.edit_message_text(
        f"{fmt(username)}\n{STATUS_LABELS.get(status, status)}{profile_summary(state)}{mute_tag}",
        parse_mode=ParseMode.HTML,
        reply_markup=watch_keyboard(username, paused=paused),
    )


async def notify_watchers(context: ContextTypes.DEFAULT_TYPE, username: str, old_status: str, new_status: str):
    state = storage.get_state(username)
    summary = profile_summary(state)

    if new_status == "live" and old_status == "not_found":
        downtime = ""
        if state.get("down_since"):
            try:
                down_since = datetime.fromisoformat(state["down_since"])
                downtime_seconds = (datetime.utcnow() - down_since).total_seconds()
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

    for chat_id in storage.chats_watching(username, only_unpaused=True):
        try:
            if new_status == "live" and state.get("profile_pic_url"):
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=state["profile_pic_url"],
                    caption=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard
                )
        except Exception:
            logger.exception("failed to notify chat %s about %s", chat_id, username)


async def check_job(context: ContextTypes.DEFAULT_TYPE):
    global CHECKS_RUN
    usernames = storage.all_watched_usernames()
    if not usernames:
        return

    consecutive_blocks = 0
    client = await get_warm_client(context)

    for username in usernames:
        result = await check_instagram_status(username, client)
        CHECKS_RUN += 1

        if result.error == "rate_limited":
            consecutive_blocks += 1
            # Back off harder the more blocks we see in a row, instead of
            # keeping up the same pace and getting blocked even longer.
            backoff = min(60, 5 * (2 ** consecutive_blocks))
            logger.warning(
                "%s: rate limited (%d in a row), backing off %ds", username, consecutive_blocks, backoff
            )
            await asyncio.sleep(backoff)
            if consecutive_blocks >= 3:
                logger.warning("too many consecutive blocks, skipping rest of this cycle")
                break
        else:
            consecutive_blocks = 0
            await asyncio.sleep(random.uniform(1, 3))  # spread requests out, avoid bursty IP blocks

        if result.status is None:
            logger.info("%s: temporary check issue (%s) — keeping last confirmed status", username, result.error)
            continue

        state = storage.get_state(username)
        confirmed = state["confirmed_status"]

        if result.status == confirmed:
            storage.clear_pending(username)
            continue

        _, pending_count = storage.bump_pending(username, result.status)
        if pending_count < CONFIRM_CHECKS:
            continue

        storage.set_confirmed(username, result.status, vars(result))
        await notify_watchers(context, username, confirmed, result.status)


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
    app.add_handler(CallbackQueryHandler(button_cmd))

    app.job_queue.run_repeating(check_job, interval=CHECK_INTERVAL_SECONDS, first=10)

    logger.info("bot starting, checking every %ss", CHECK_INTERVAL_SECONDS)
    app.run_polling()


if __name__ == "__main__":
    main()
