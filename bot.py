import asyncio
import logging
import os
import random

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import storage
from ig_checker import check_instagram_status

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "15"))
CONFIRM_CHECKS = int(os.environ.get("CONFIRM_CHECKS", "2"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ig-watch-bot")

STATUS_LABELS = {
    "live": "🟢 live",
    "not_found": "🔴 not found / suspended / deactivated",
    "unknown": "⚠️ unknown (no confirmed check yet)",
}

HELP_TEXT = (
    "Instagram availability watcher\n\n"
    "/watch <username> - start tracking an account\n"
    "/list - show tracked accounts and their status\n"
    "/remove <username> - stop tracking an account\n"
    "/help - show this message\n\n"
    f"Accounts are rechecked every {CHECK_INTERVAL_SECONDS}s. "
    "A status change is only announced after it's confirmed on "
    f"{CONFIRM_CHECKS} checks in a row, to avoid false alarms from temporary blocks."
)


def fmt(username: str) -> str:
    return f"@{username}"


def clean_username(raw: str) -> str:
    return raw.strip().lstrip("@").lower()


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /watch <username>")
        return
    username = clean_username(context.args[0])
    chat_id = update.effective_chat.id

    async with httpx.AsyncClient() as client:
        result = await check_instagram_status(username, client)

    if result.status is None:
        await update.message.reply_text(
            f"⚠️ Couldn't verify {fmt(username)} right now ({result.error}). Try again in a moment."
        )
        return

    storage.add_watch(chat_id, username)
    storage.set_confirmed(username, result.status)
    await update.message.reply_text(
        f"✅ Now tracking {fmt(username)} — current status: {STATUS_LABELS[result.status]}"
    )


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


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    usernames = storage.list_watches(chat_id)
    if not usernames:
        await update.message.reply_text(
            "You're not tracking any accounts yet. Use /watch <username> to add one."
        )
        return
    lines = ["Tracking:"]
    for username in usernames:
        status = storage.get_state(username)["confirmed_status"] or "unknown"
        lines.append(f"{fmt(username)}: {STATUS_LABELS.get(status, status)}")
    await update.message.reply_text("\n".join(lines))


async def notify_watchers(context: ContextTypes.DEFAULT_TYPE, username: str, old_status: str, new_status: str):
    if new_status == "live" and old_status == "not_found":
        text = f"🚨 {fmt(username)} is BACK ONLINE!\n\nhttps://instagram.com/{username}"
    elif new_status == "not_found":
        text = f"⚠️ {fmt(username)} is no longer reachable (banned, suspended, or deactivated)."
    else:
        text = f"ℹ️ {fmt(username)} status changed to {STATUS_LABELS.get(new_status, new_status)}"

    for chat_id in storage.chats_watching(username):
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            logger.exception("failed to notify chat %s about %s", chat_id, username)


async def check_job(context: ContextTypes.DEFAULT_TYPE):
    usernames = storage.all_watched_usernames()
    if not usernames:
        return

    async with httpx.AsyncClient() as client:
        for username in usernames:
            result = await check_instagram_status(username, client)
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

            storage.set_confirmed(username, result.status)
            await notify_watchers(context, username, confirmed, result.status)


def main():
    storage.init_db()
    # Python 3.14 removed asyncio.get_event_loop()'s implicit loop creation, which
    # python-telegram-bot's run_polling() still relies on. Set one explicitly.
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("list", list_cmd))

    app.job_queue.run_repeating(check_job, interval=CHECK_INTERVAL_SECONDS, first=10)

    logger.info("bot starting, checking every %ss", CHECK_INTERVAL_SECONDS)
    app.run_polling()


if __name__ == "__main__":
    main()
