import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from psycopg_pool import AsyncConnectionPool

from . import db
from .bot import run_bot
from .logic import HOT, UNIT_OFF, defrost_step, due_summary_slot, format_defrost_event, format_event, format_summary, step
from .report import build_rows
from .senders import ADMIN_CHANNEL, enabled_senders

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines contain the bot token in the URL
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("notifier")

NOTIFY_DEFROST = False  # START/FINISHED DEFROST messages off; tracking still pauses BAD alerts
NOTIFY_SYSTEM = False  # "No data from any sensor" / RECOVERED messages off; room alerts still pause while silent
TICK_S = 30
DISPATCH_S = 5


async def emit(pool, senders, code, typ, severity, text, payload=None, admin_only=False,
               include_admin=True):
    """admin_only: debug messages. They go to the admin channel if one is configured
    (else to the normal channels). Everything else goes to the users group and,
    unless include_admin is False, to the admin chat as well."""
    names = [s.name for s in senders]
    if admin_only and ADMIN_CHANNEL in names:
        names = ["log", ADMIN_CHANNEL]
    elif not include_admin:
        names = [n for n in names if n != ADMIN_CHANNEL]
    await db.add_event(pool, names, code, typ, severity, text, payload)
    log.info("event %s %s %s", typ, code or "-", severity or "-")


async def evaluator(pool, senders, started):
    last_summary_check = 0.0
    while True:
        try:
            now = datetime.now(timezone.utc)
            cfg = await db.load_config(pool)
            rooms = await db.load_rooms(pool)
            old = await db.load_states(pool)
            last_pkt = await db.last_packet_time(pool)
            age = (now - last_pkt).total_seconds() if last_pkt else None
            silent = age is None or age >= cfg.no_reading_s
            can_notify = time.monotonic() - started >= cfg.startup_grace_s

            was_silent = await db.get_meta(pool, "silent") == "1"
            if silent != was_silent and (can_notify or not silent):
                if not NOTIFY_SYSTEM:
                    pass
                elif silent:
                    mins = f"{int(age // 60)} min" if age is not None else "ever"
                    await emit(pool, senders, None, "SYSTEM", "CRITICAL",
                               f"❌ CRITICAL • No data from any sensor (last packet {mins} ago). "
                               "Room alerts are paused until packets return.", admin_only=True)
                else:
                    await emit(pool, senders, None, "SYSTEM", None,
                               "✅ RECOVERED • Sensor data is flowing again.", admin_only=True)
                await db.set_meta(pool, "silent", "1" if silent else "0")

            new, events = step(now, rooms, old, cfg, can_notify=can_notify, silent=silent)
            for ev in events:
                await emit(pool, senders, ev.code, ev.type, ev.severity,
                           format_event(ev, rooms[ev.code].name),
                           {"kinds": list(ev.kinds), "temp": ev.temp},
                           admin_only=not {HOT, UNIT_OFF} & set(ev.kinds))  # no-reading alone: admin only
            baseline = await db.get_meta(pool, "defrost_init") != "1"
            new, dfs = defrost_step(now, rooms, new, cfg, can_notify=can_notify, silent=silent,
                                    baseline=baseline)
            for ev in dfs if NOTIFY_DEFROST else ():
                await emit(pool, senders, ev.code, ev.type, None,
                           format_defrost_event(ev, rooms[ev.code].name, ev.code),
                           {"temp": ev.temp, "duration_s": ev.duration_s})
            await db.save_states(pool, old, new)
            if baseline and not silent:
                await db.set_meta(pool, "defrost_init", "1")

            slot = due_summary_slot(now, cfg, await db.get_meta(pool, "last_summary_slot"))
            if slot:
                rows = await build_rows(pool, rooms, cfg, now)
                text = format_summary(now, cfg, rows, age)
                await emit(pool, senders, None, "SUMMARY", None, text)
                await db.set_meta(pool, "last_summary_slot", slot)
        except Exception:
            log.exception("evaluator tick failed")
        await asyncio.sleep(TICK_S)


async def dispatcher(pool, senders):
    by_name = {s.name: s for s in senders}
    while True:
        try:
            if await db.get_meta(pool, "pause_users") == "1" and "telegram" in by_name:
                await db.skip_pending(pool, "telegram")  # paused: the users group gets nothing, admin still does
            for event_id, channel, attempts, text in await db.due_deliveries(pool, by_name):
                try:
                    await by_name[channel].send(text)
                except Exception as e:
                    log.warning("delivery %s via %s failed: %s", event_id, channel, e)
                    await db.mark_failed(pool, event_id, channel, attempts, str(e))
                else:
                    await db.mark_sent(pool, event_id, channel)
        except Exception:
            log.exception("dispatcher tick failed")
        await asyncio.sleep(DISPATCH_S)


async def main():
    started = time.monotonic()
    pool = AsyncConnectionPool(os.environ["DATABASE_URL"], min_size=1, max_size=5, open=False)
    await pool.open(wait=True, timeout=60)
    await db.init_schema(pool)
    senders = enabled_senders()
    names = [s.name for s in senders]
    log.info("notifier started, channels: %s", ", ".join(names))
    local = datetime.now(timezone.utc).astimezone(ZoneInfo((await db.load_config(pool)).summary_tz))
    await emit(pool, senders, None, "SYSTEM", None,
               f"🟢 **Coldwatch notifier restarted** 🟢\nDate: {local:%a %-d %b %Y}\nTime: {local:%H:%M}",
               admin_only=True)
    tasks = [evaluator(pool, senders, started), dispatcher(pool, senders)]
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if token and chat:
        tasks.append(run_bot(token, chat, pool, os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
