import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from psycopg_pool import AsyncConnectionPool

from . import db
from .bot import run_bot
from .logic import due_summary_slot, format_event, format_summary, step
from .report import build_rows
from .senders import enabled_senders

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines contain the bot token in the URL
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("notifier")

TICK_S = 30
DISPATCH_S = 5


async def emit(pool, senders, code, typ, severity, text, payload=None):
    await db.add_event(pool, [s.name for s in senders], code, typ, severity, text, payload)
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
                if silent:
                    mins = f"{int(age // 60)} min" if age is not None else "ever"
                    await emit(pool, senders, None, "SYSTEM", "CRITICAL",
                               f"❌ CRITICAL • No data from any sensor (last packet {mins} ago). "
                               "Room alerts are paused until packets return.")
                else:
                    await emit(pool, senders, None, "SYSTEM", None,
                               "✅ RECOVERED • Sensor data is flowing again.")
                await db.set_meta(pool, "silent", "1" if silent else "0")

            new, events = step(now, rooms, old, cfg, can_notify=can_notify, silent=silent)
            for ev in events:
                await emit(pool, senders, ev.code, ev.type, ev.severity,
                           format_event(ev, rooms[ev.code].name),
                           {"kinds": list(ev.kinds), "temp": ev.temp})
            await db.save_states(pool, old, new)

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
               f"🟢 **Coldwatch notifier restarted** 🟢\nDate: {local:%a %-d %b %Y}\nTime: {local:%H:%M}")
    tasks = [evaluator(pool, senders, started), dispatcher(pool, senders)]
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if token and chat:
        tasks.append(run_bot(token, chat, pool))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
