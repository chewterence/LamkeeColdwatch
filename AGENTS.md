# AGENTS.md

Guidance for AI coding agents working on LamkeeColdwatch.

## Purpose

LamkeeColdwatch monitors 17 coldrooms and freezers. It should alert when a sensor is down or too warm, and send periodic updates. A dockerized Telegram bot is planned.

A sensor server (192.168.1.250) pushes UDP data about every 60s to the ingest container (port 2049), which stores it in TimescaleDB.

## Layout

- `docker-compose.yml`: project `lamkeecoldwatch`.
  - `db`: `timescale/timescaledb:latest-pg16`, volume `coldwatch_db`, init SQL in `db/init/`.
  - `ingest`: FastAPI (uvicorn) with psycopg3 async pool and an asyncio UDP listener.
  - Both services use `restart: unless-stopped`.
- `ingest/app/udp.py`: `parse`, `store` and `UdpProtocol`.
- `ingest/app/main.py`: HTTP routes. The catch-all `/{path:path}` must stay last.
- `tests/test_ingest.py`: regression tests for all rooms.
- `.env`: secrets and settings. Never print or commit it. `.env.example` documents the keys.

## Configuration

- `INGEST_BIND_IP` (default `0.0.0.0`), `INGEST_PORT` (default 8000), `UDP_PORT` (default 2049).
- The ingest listens on all interfaces and never stores the Mac's IP. Only the sensor server's push target needs updating if the Mac's IP changes.
- Apply `.env` changes with `docker compose up -d`.

## Packet format

`LamkeeColdwatch,<count>,<YYYY-MM-DD HH:MM:SS>,<v1..vN>,<status>`

- Positions 0-16 are temperature, 17-33 are "On", for 17 rooms.
- Identity is positional, via the `sensors` table.
- If the value count doesn't match the active sensors (34), the packet is stored with an error and no readings.
- Fault markers `-`, `*`, `#`, `/` are stored as a NULL value plus the marker in `readings.fault`.
- Docker Desktop rewrites the UDP source address, so the stored `client` is not the real sender.

## Rooms

RS1-001 Room 1, 002 Room 2, 003 Room 3 System 1, 004 Room 4, 005 Room 5, 006 Room 5 (Small System), 007 Room 5 System 3, 008 Room 5 System 1, 009 Room 5 System 2, 010 Room 5 System 4, 011 Room 8, 012 Room 3 System 2, 013 Room 1 System 2, 014 Room 3 System 3, 015 Room 6 Blast Freezer, 020 Room 7 System 1, 022 Room 7 System 2.

Room names are user-editable via `PUT /rooms/{code}`.

## Testing

```
tests/.venv/bin/pytest tests -q
```

- Tests run on the host and use `docker compose exec -T db psql`, because the DB port is not published. The stack must be running.
- Test packets use identifier `RegressionTest` and year-2000 sensor times, and are deleted before and after each test.
- A real sensor packet arriving mid-test can rarely make a test read the wrong packet. Rerun if so.
- Run the tests after any change to parsing, storage or the `sensors` table.

## Gotchas

- Init SQL in `db/init/` runs only on an empty volume. Schema changes on a live DB need a migration.
- Compression, retention and backup are not configured yet.
- Ingest HTTP port 8000 is unauthenticated (`PUT /rooms` and the catch-all). Don't expose it beyond the LAN.
- `raw_requests` and `POST /readings` are legacy.
- `sensors.high_threshold` exists but is unused.

## Not yet built

- Sensor-down detection, high-temperature alerts, defrost handling.
- Telegram bot service with periodic updates.
- Backup (pg_dump), compression and retention.
- Auth on the ingest HTTP API.
- Open questions: which rooms are freezers vs chillers and their thresholds, the sensor-down timeout, the meaning of the status code and fault markers, and the sensor server's timezone.
