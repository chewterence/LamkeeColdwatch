"""Telegram slash commands (python-telegram-bot). Answers only TELEGRAM_CHAT_ID and TELEGRAM_ADMIN_CHAT_ID.

Alerts do not depend on this: if the bot fails, main keeps evaluating and sending.
"""
import asyncio
import logging
from datetime import datetime, timezone

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes, filters

from . import db
from .logic import find_rooms, format_alerts, format_room, format_status
from .report import build_rows
from .senders import telegram_html

log = logging.getLogger("notifier.bot")

COMMANDS = [
    ("status", "All rooms right now"),
    ("room", "One room: /room 8"),
    ("alerts", "Open alerts"),
    ("help", "List commands"),
]


async def _snapshot(pool):
    now = datetime.now(timezone.utc)
    cfg = await db.load_config(pool)
    rooms = await db.load_rooms(pool)
    last = await db.last_packet_time(pool)
    age = (now - last).total_seconds() if last else None
    return now, cfg, rooms, age


NO_DATA_REPLY = "No data from any sensor, temporarily fixing the problem..."


def _no_data(cfg, age) -> bool:
    return age is None or age >= cfg.no_reading_s


def build_application(token: str, chat_ids: list, pool) -> Application:
    only_me = filters.Chat(chat_id=chat_ids)

    async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        now, cfg, rooms, age = await _snapshot(pool)
        if _no_data(cfg, age):
            await update.effective_message.reply_text(NO_DATA_REPLY)
            return
        rows = await build_rows(pool, rooms, cfg, now)
        await update.effective_message.reply_text(telegram_html(format_status(now, cfg, rows, age)), parse_mode="HTML")

    async def room(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.effective_message.reply_text("Which room? Example: /room 8")
            return
        now, cfg, rooms, age = await _snapshot(pool)
        if _no_data(cfg, age):
            await update.effective_message.reply_text(NO_DATA_REPLY)
            return
        hits = find_rooms(rooms, " ".join(ctx.args))
        if not hits:
            await update.effective_message.reply_text("No room matches that. Try /status to see names.")
        elif len(hits) > 1:
            names = "\n".join(f"• {r.name} ({r.code})" for r in hits)
            await update.effective_message.reply_text(f"Several rooms match, be more specific:\n{names}")
        else:
            rows = await build_rows(pool, {hits[0].code: hits[0]}, cfg, now)
            await update.effective_message.reply_text(format_room(now, rows[0]))

    async def alerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        now, _, rooms, _ = await _snapshot(pool)
        await update.effective_message.reply_text(format_alerts(now, rooms, await db.load_states(pool)))

    async def help_(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text("\n".join(f"/{c} - {d}" for c, d in COMMANDS))

    async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
        # Type only: exception text from network errors can contain the bot token.
        log.warning("bot error: %s", type(ctx.error).__name__)

    async def post_init(app: Application):
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])

    app = Application.builder().token(token).post_init(post_init).build()
    for name, fn in (("status", status), ("room", room), ("alerts", alerts),
                     ("help", help_), ("start", help_)):
        app.add_handler(CommandHandler(name, fn, filters=only_me))
    app.add_error_handler(on_error)
    return app


async def run_bot(token: str, chat_id: str, pool, admin_id: str = ""):
    """Poll for commands forever; retry if startup fails so alerts are never affected."""
    try:
        cid = [int(chat_id)] + ([int(admin_id)] if admin_id else [])
    except ValueError:
        log.warning("TELEGRAM_CHAT_ID / TELEGRAM_ADMIN_CHAT_ID is not a number: bot commands disabled")
        return
    while True:
        try:
            app = build_application(token, cid, pool)
            async with app:
                await app.start()
                await app.updater.start_polling(drop_pending_updates=True)
                log.info("bot commands ready: %s", ", ".join("/" + c for c, _ in COMMANDS))
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("bot stopped (%s), retrying in 30 s", type(e).__name__)
            await asyncio.sleep(30)
