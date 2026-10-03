"""Database access for the notifier. Tables are created on startup (idempotent),
so no change to db/init/ or a fresh volume is needed."""
import json
from datetime import datetime

from .logic import Config, Reading, Room, State

SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_config (
    key   text PRIMARY KEY,
    value text NOT NULL
);
CREATE TABLE IF NOT EXISTS alert_state (
    code             text NOT NULL,
    kind             text NOT NULL,
    since            timestamptz NOT NULL,
    notified_at      timestamptz,
    last_notified_at timestamptz,
    PRIMARY KEY (code, kind)
);
CREATE TABLE IF NOT EXISTS alert_meta (
    key   text PRIMARY KEY,
    value text NOT NULL
);
CREATE TABLE IF NOT EXISTS alert_events (
    id         bigserial PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    code       text,
    type       text NOT NULL,
    severity   text,
    text       text NOT NULL,
    payload    jsonb
);
CREATE TABLE IF NOT EXISTS alert_deliveries (
    event_id        bigint NOT NULL REFERENCES alert_events(id) ON DELETE CASCADE,
    channel         text NOT NULL,
    status          text NOT NULL DEFAULT 'pending',
    attempts        int NOT NULL DEFAULT 0,
    last_error      text,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    sent_at         timestamptz,
    PRIMARY KEY (event_id, channel)
);
CREATE INDEX IF NOT EXISTS alert_deliveries_pending_idx
    ON alert_deliveries (next_attempt_at) WHERE status = 'pending';
INSERT INTO alert_config (key, value) VALUES
    ('bad_min_c', '-14'),
    ('bad_for_s', '600'),
    ('no_reading_s', '300'),
    ('unit_off_for_s', '0'),
    ('remind_s', '3600'),
    ('startup_grace_s', '180'),
    ('summary_times', '08:00,18:00'),
    ('summary_tz', 'Asia/Singapore')
ON CONFLICT (key) DO NOTHING;
"""


async def init_schema(pool):
    async with pool.connection() as conn:
        await conn.execute(SCHEMA)


async def load_config(pool) -> Config:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT key, value FROM alert_config")
        return Config.from_rows(dict(await cur.fetchall()))


async def load_rooms(pool) -> dict:
    """Latest temperature and On reading per active room."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT s.code, COALESCE(rn.display_name, s.code)
            FROM sensors s LEFT JOIN room_names rn ON rn.code = s.code
            WHERE s.active AND s.point = 'temp' ORDER BY s.position
            """
        )
        names = await cur.fetchall()
        cur = await conn.execute(
            """
            SELECT DISTINCT ON (sensor_id, point) sensor_id, point, time, value, fault
            FROM readings WHERE time > now() - interval '1 day'
            ORDER BY sensor_id, point, time DESC
            """
        )
        latest = {(r[0], r[1]): Reading(r[2], r[3], r[4]) for r in await cur.fetchall()}
    return {
        code: Room(code, name, latest.get((code, "temp")), latest.get((code, "on")))
        for code, name in names
    }


async def last_packet_time(pool) -> datetime | None:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT max(received_at) FROM packets WHERE error IS NULL")
        return (await cur.fetchone())[0]


async def load_ranges(pool) -> dict:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT sensor_id, min(value), max(value) FROM readings
            WHERE point = 'temp' AND time > now() - interval '24 hours'
            GROUP BY sensor_id
            """
        )
        return {r[0]: (r[1], r[2]) for r in await cur.fetchall()}


async def load_states(pool) -> dict:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT code, kind, since, notified_at, last_notified_at FROM alert_state"
        )
        return {(r[0], r[1]): State(r[2], r[3], r[4]) for r in await cur.fetchall()}


async def save_states(pool, old: dict, new: dict):
    if old == new:
        return
    async with pool.connection() as conn:
        for key in old.keys() - new.keys():
            await conn.execute("DELETE FROM alert_state WHERE code=%s AND kind=%s", key)
        for key, st in new.items():
            if old.get(key) != st:
                await conn.execute(
                    """
                    INSERT INTO alert_state (code, kind, since, notified_at, last_notified_at)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (code, kind) DO UPDATE SET since=EXCLUDED.since,
                        notified_at=EXCLUDED.notified_at, last_notified_at=EXCLUDED.last_notified_at
                    """,
                    (*key, st.since, st.notified_at, st.last_notified_at),
                )


async def add_event(pool, channels, code, typ, severity, text, payload=None) -> int:
    """Write an event and one pending delivery per channel, atomically."""
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "INSERT INTO alert_events (code, type, severity, text, payload) "
                "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (code, typ, severity, text, json.dumps(payload) if payload else None),
            )
            eid = (await cur.fetchone())[0]
            for ch in channels:
                await conn.execute(
                    "INSERT INTO alert_deliveries (event_id, channel) VALUES (%s,%s)", (eid, ch)
                )
    return eid


async def due_deliveries(pool, channels):
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT d.event_id, d.channel, d.attempts, e.text
            FROM alert_deliveries d JOIN alert_events e ON e.id = d.event_id
            WHERE d.status = 'pending' AND d.next_attempt_at <= now() AND d.channel = ANY(%s)
            ORDER BY d.event_id LIMIT 50
            """,
            (list(channels),),
        )
        return await cur.fetchall()


MAX_ATTEMPTS = 30


async def mark_sent(pool, event_id, channel):
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE alert_deliveries SET status='sent', sent_at=now(), attempts=attempts+1, "
            "last_error=NULL WHERE event_id=%s AND channel=%s",
            (event_id, channel),
        )


async def mark_failed(pool, event_id, channel, attempts, error):
    delay = min(300, 10 * 2**attempts)
    status = "failed" if attempts + 1 >= MAX_ATTEMPTS else "pending"
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE alert_deliveries SET status=%s, attempts=attempts+1, last_error=%s, "
            "next_attempt_at=now() + make_interval(secs => %s) WHERE event_id=%s AND channel=%s",
            (status, error[:300], delay, event_id, channel),
        )


async def get_meta(pool, key, default=""):
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT value FROM alert_meta WHERE key=%s", (key,))
        row = await cur.fetchone()
        return row[0] if row else default


async def set_meta(pool, key, value):
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO alert_meta (key, value) VALUES (%s,%s) "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            (key, value),
        )
