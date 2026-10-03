"""Room snapshot shared by the summary and the bot commands."""
from . import db
from .logic import room_status


async def build_rows(pool, rooms: dict, cfg, now) -> list:
    ranges = await db.load_ranges(pool)
    rows = []
    for r in rooms.values():
        lo, hi = ranges.get(r.code, (None, None))
        rows.append({
            "code": r.code, "name": r.name, "status": room_status(now, r, cfg),
            "temp": r.temp.value if r.temp else None,
            "on": r.on.value if r.on else None, "lo": lo, "hi": hi,
            "age": (now - r.temp.time).total_seconds() if r.temp else None,
        })
    return rows
