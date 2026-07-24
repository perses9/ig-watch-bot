import asyncio
import logging
import os
import random
import time
from datetime import datetime
from functools import wraps

import httpx
from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes

import storage
from ig_checker import check_instagram_status

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ig-watch-bot")

START_TIME = time.monotonic()
CHECKS_RUN = 0

STATUS_LABELS = {
    "live": "🟢 live",
    "not_found": "🔴 not found / suspended / deactivated",
    "unknown": "⚠️ unknown (no confirmed check yet)",
}

HELP_TEXT = (
    "Instagram availability watcher\n\n"
    "/watch <username> [username2 ...] - start tracking one or more accounts\n"
    "/check <username> - check status right now, instead of waiting for the next cycle\n"
    "/list - show tracked accounts, status, and follower counts\n"
    "/remove <username> - stop tracking an account\n"
    "/pause <username> - mute notifications for an account without removing it\n"
    "/resume <username> - unmute a paused account\n"
    "/uptime - show how long the bot has been running and how many checks it's done\n"
    "/help - show this message\n\n"
    f"Accounts are rechecked every {CHECK_INTERVAL_SECONDS}s. "
    "A status change is only announced after it's confirmed on "
    f"{CONFIRM_CHECKS} checks in a row, to avoid false alarms from temporary blocks."
)


def fmt(username: str) -> str:
    return f"@{username}"


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
        bits.append(state["full_name"])
    if state.get("follower_count") is not None:
        bits.append(f"{state['follower_count']:,} followers")
    if state.get("is_private"):
        bits.append("private")
    return f" ({', '.join(bits)})" if bits else ""


def restricted(handler):
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if ALLOWED_CHAT_IDS and update.effective_chat.id not in ALLOWED_CHAT_IDS:
            await update.message.reply_text("You're not authorized to use this bot.")
            return
        return await handler(update, context)

    return wrapper


@restricted
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


@restricted
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


@restricted
async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /watch <username> [username2 ...]")
        return

    usernames = [clean_username(u) for u in context.args[:MAX_WATCH_PER_MESSAGE]]
    chat_id = update.effective_chat.id
    lines = []

    async with httpx.AsyncClient() as client:
        for username in usernames:
            result = await check_instagram_status(username, client)
            if result.status is None:
                lines.append(f"⚠️ {fmt(username)}: couldn't verify right now ({result.error})")
                continue
            storage.add_watch(chat_id, username)
            storage.set_confirmed(username, result.status, vars(result))
            lines.append(f"✅ {fmt(username)}: {STATUS_LABELS[result.status]}")

    await update.message.reply_text("\n".join(lines))


@restricted
async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /check <username>")
        return
    username = clean_username(context.args[0])

    async with httpx.AsyncClient() as client:
        result = await check_instagram_status(username, client)

    if result.status is None:
        await update.message.reply_text(
            f"⚠️ Couldn't verify {fmt(username)} right now ({result.error}). Try again in a moment."
        )
        return

    summary = profile_summary(vars(result))
    await update.message.reply_text(
        f"{fmt(username)}: {STATUS_LABELS[result.status]}{summary}\n"
        "(This is a one-off check and doesn't affect anything you're tracking.)"
    )


@restricted
async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /remove <username>")
        return
    username = clean_username(context.args[0])
    chat_id = update.effective_chat.id
    if storage.remove_watch(chat_id, username):
        await update.message.reply_text(f"🗑️ Stopped tracking {fmt(username)}.")
    else:
        await update.message.reply_text(f"You weren't tracking {fmt(username)}.")


@restricted
async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /pause <username>")
        return
    username = clean_username(context.args[0])
    chat_id = update.effective_chat.id
    if storage.set_paused(chat_id, username, True):
        await update.message.reply_text(f"🔇 Muted notifications for {fmt(username)} (still tracked).")
    else:
        await update.message.reply_text(f"You weren't tracking {fmt(username)}.")


@restricted
async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /resume <username>")
        return
    username = clean_username(context.args[0])
    chat_id = update.effective_chat.id
    if storage.set_paused(chat_id, username, False):
        await update.message.reply_text(f"🔊 Unmuted {fmt(username)}.")
    else:
        await update.message.reply_text(f"You weren't tracking {fmt(username)}.")


@restricted
async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    watches = storage.list_watches(chat_id)
    if not watches:
        await update.message.reply_text(
            "You're not tracking any accounts yet. Use /watch <username> to add one."
        )
        return
    lines = ["Tracking:"]
    for w in watches:
        state = storage.get_state(w["username"])
        status = state["confirmed_status"] or "unknown"
        mute_tag = " 🔇" if w["paused"] else ""
        lines.append(
            f"{fmt(w['username'])}: {STATUS_LABELS.get(status, status)}{profile_summary(state)}{mute_tag}"
        )
    await update.message.reply_text("\n".join(lines))


@restricted
async def uptime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    elapsed = time.monotonic() - START_TIME
    await update.message.reply_text(
        f"Bot has been running for {format_duration(elapsed)}.\n"
        f"Checks run this session: {CHECKS_RUN}\n"
        f"Check interval: {CHECK_INTERVAL_SECONDS}s"
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
                downtime = f" (was down for {format_duration(downtime_seconds)})"
            except ValueError:
                pass
        text = f"🚨 {fmt(username)} is BACK ONLINE!{downtime}{summary}\n\nhttps://instagram.com/{username}"
    elif new_status == "not_found":
        text = f"⚠️ {fmt(username)} is no longer reachable (banned, suspended, or deactivated)."
    else:
        text = f"ℹ️ {fmt(username)} status changed to {STATUS_LABELS.get(new_status, new_status)}{summary}"

    for chat_id in storage.chats_watching(username, only_unpaused=True):
        try:
            if new_status == "live" and state.get("profile_pic_url"):
                await context.bot.send_photo(chat_id=chat_id, photo=state["profile_pic_url"], caption=text)
            else:
                await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            logger.exception("failed to notify chat %s about %s", chat_id, username)


async def check_job(context: ContextTypes.DEFAULT_TYPE):
    global CHECKS_RUN
    usernames = storage.all_watched_usernames()
    if not usernames:
        return

    async with httpx.AsyncClient() as client:
        for username in usernames:
            result = await check_instagram_status(username, client)
            CHECKS_RUN += 1
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


async def register_commands(application: Application):
    await application.bot.set_my_commands(BOT_COMMANDS)


def main():
    storage.init_db()
    # Python 3.14 removed asyncio.get_event_loop()'s implicit loop creation, which
    # python-telegram-bot's run_polling() still relies on. Set one explicitly.
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(BOT_TOKEN).post_init(register_commands).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("pause", pause_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("uptime", uptime_cmd))

    app.job_queue.run_repeating(check_job, interval=CHECK_INTERVAL_SECONDS, first=10)

    logger.info("bot starting, checking every %ss", CHECK_INTERVAL_SECONDS)
    app.run_polling()


if __name__ == "__main__":
    main()
