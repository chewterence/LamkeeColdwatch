# AGENTS.md

Guidance for AI coding agents working on LamkeeColdwatch.

## Purpose

LamkeeColdwatch monitors 17 coldrooms and freezers. It alerts through Telegram when a room is too warm, has no reading or its unit is off, sends summaries, and answers slash commands.

Stack: Docker Compose, Python, FastAPI, asyncio UDP, TimescaleDB (Postgres 16) and Telegram.

A sensor server (192.168.1.250) pushes UDP data about every 60s to the ingest container (port 2049), which stores it in TimescaleDB.

## Layout

- `docker-compose.yml`: project `lamkeecoldwatch`.
  - `db`: `timescale/timescaledb:latest-pg16`, volume `coldwatch_db`, init SQL in `db/init/`.
  - `ingest`: FastAPI (uvicorn) with psycopg3 async pool and an asyncio UDP listener.
  - `notifier`: evaluates alerts, sends Telegram messages and answers slash commands.
  - All services use `restart: unless-stopped`.
- `ingest/app/udp.py`: `parse`, `store` and `UdpProtocol`.
- `ingest/app/main.py`: HTTP routes. The catch-all `/{path:path}` must stay last.
- `notifier/app/`: `logic.py` (pure alert rules and message formats, no I/O), `main.py` (evaluator, dispatcher, routing), `bot.py` (slash commands), `db.py` (schema and queries), `report.py`, `senders.py`, `chatid.py`.
- `tests/test_ingest.py`: regression tests for all rooms. `tests/test_notifier_logic.py`: alert logic tests.
- `.env`: secrets and settings. Never print or commit it. `.env.example` documents the keys.

## Configuration

- `INGEST_BIND_IP` (default `0.0.0.0`), `INGEST_PORT` (default 8000), `UDP_PORT` (default 2049).
- The ingest listens on all interfaces and never stores the Mac's IP. Only the sensor server's push target needs updating if the Mac's IP changes.
- Notifier: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (users group), `TELEGRAM_ADMIN_CHAT_ID` (admin). Empty token and chat means log-only mode.
- Apply `.env` changes with `docker compose up -d`. Rebuild the notifier after code changes: `docker compose up -d --build notifier`.

## Packet format

`LamkeeColdwatch,<count>,<YYYY-MM-DD HH:MM:SS>,<v1..vN>,<status>`

- Positions 0-16 are temperature, 17-33 are "On", 34-50 are defrost, for 17 rooms.
- Identity is positional, via the `sensors` table.
- If the value count doesn't match the active sensors (51), the packet is stored with an error and no readings.
- Fault markers `-`, `*`, `#`, `/` are stored as a NULL value plus the marker in `readings.fault`.
- Docker Desktop rewrites the UDP source address, so the stored `client` is not the real sender.

## Rooms

RS1-001 Room 1, 002 Room 2, 003 Room 3 System 1, 004 Room 4, 005 Room 5, 006 Room 5 (Small System), 007 Room 5 System 3, 008 Room 5 System 1, 009 Room 5 System 2, 010 Room 5 System 4, 011 Room 8, 012 Room 3 System 2, 013 Room 1 System 2, 014 Room 3 System 3, 015 Room 6 Blast Freezer, 020 Room 7 System 1, 022 Room 7 System 2.

Room names are user-editable via `PUT /rooms/{code}`.

## Alerts (notifier)

Thresholds are shared by all rooms and live in the `alert_config` table.

- BAD: warmer than -12°C for 120 min in a row. STILL BAD repeats hourly. Both use ❗. /status, /room and the summary use ⚠️.
- CRITICAL (❌, STILL CRITICAL hourly with 🔁): no reading for 5 min, or UNIT OFF (immediate).
- RECOVERED (✅) closes an alert and says how long the room was too warm, had no reading or was off.
- 3 min startup grace after the notifier starts.
- Defrost: while a room defrosts, BAD alerts pause and /status shows 🧊 DEFROSTING. Defrost value 1 = active is unconfirmed. START/FINISHED DEFROST messages are off (`NOTIFY_DEFROST = False` in `main.py`).
- System-wide no-data alert (no packet from any sensor) is admin-only and off (`NOTIFY_SYSTEM = False`). While no data flows, room alerts pause and /status and /room reply "No data from any sensor, temporarily fixing the problem...".
- Summaries at 07:00 and 20:00 Asia/Singapore: the /status message with a "Good Morning ☀️" or "Good Evening 🌙" line on top.
- Slash commands: /status, /room, /alerts, /help. Only the two configured chats are answered.

Routing (`emit()` in `main.py`):

| Alert | Users group | Admin |
| --- | --- | --- |
| BAD, STILL BAD | yes | yes |
| UNIT OFF (alone or mixed) | yes | yes |
| No reading alone (open, remind, recover) | no | yes |
| Summary | yes | yes |
| Notifier restarted | no | yes |

- Alerts go through an outbox: rows in `alert_events` and `alert_deliveries`, one per channel (`log`, `telegram`, `telegram-admin`). The dispatcher sends them every 5 s and retries failures.
- `alert_meta` holds `silent`, `defrost_init`, `last_summary_slot` and `pause_users`. Setting `pause_users` to `1` skips all pending deliveries to the users group while admin still receives them. Use it while fixing a fault.
- Keep rules in `logic.py` free of Telegram specifics so WhatsApp, email or Teams senders can reuse them.

## Testing

```
tests/.venv/bin/pytest tests -q
```

- Tests run on the host and use `docker compose exec -T db psql`, because the DB port is not published. The stack must be running.
- Test packets use identifier `RegressionTest` and year-2000 sensor times, and are deleted before and after each test.
- A real sensor packet arriving mid-test can rarely make a test read the wrong packet. Rerun if so.
- Run the tests after any change to parsing, storage, the `sensors` table or alert logic.
- Warning: ingest tests write fake packets with `received_at = now` into the live DB. The notifier then thinks data is flowing and can send false RECOVERED or CRITICAL alerts. Pause the users group (`pause_users`) or warn the user before running them on the live stack.
- DB access: `docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "..."'`.

## Gotchas

- Init SQL in `db/init/` runs only on an empty volume. Schema changes on a live DB need a migration.
- Compression, retention and backup are not configured yet.
- Ingest HTTP port 8000 is unauthenticated (`PUT /rooms` and the catch-all). Don't expose it beyond the LAN.
- `raw_requests` and `POST /readings` are legacy.
- `sensors.high_threshold` exists but is unused.

## Not yet built

- Per-room thresholds and tests for the admin/users routing.
- Backup (pg_dump), compression and retention.
- Auth on the ingest HTTP API.
- Open questions: which rooms are freezers vs chillers and their thresholds, the meaning of the status code and fault markers, whether defrost 1 means active, and the sensor server's timezone. An outage on 2026-10-03 (packets stopped 08:14 to 08:27 UTC) has no known cause.
