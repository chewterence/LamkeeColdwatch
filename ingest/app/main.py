import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from .udp import UdpProtocol


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = AsyncConnectionPool(os.environ["DATABASE_URL"], open=False)
    await app.state.pool.open()
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: UdpProtocol(app.state.pool),
        local_addr=("0.0.0.0", int(os.environ.get("UDP_PORT", "2049"))),
    )
    yield
    transport.close()
    await app.state.pool.close()


app = FastAPI(title="LamkeeColdwatch ingest", lifespan=lifespan)


class Reading(BaseModel):
    sensor_id: str
    value: float
    unit: str = "C"
    time: datetime | None = None


@app.get("/health")
async def health():
    async with app.state.pool.connection() as conn:
        await conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/readings")
async def post_reading(reading: Reading):
    async with app.state.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO readings (time, sensor_id, value, unit) "
            "VALUES (COALESCE(%s, now()), %s, %s, %s)",
            (reading.time, reading.sensor_id, reading.value, reading.unit),
        )
    return {"status": "stored"}


class RoomLabel(BaseModel):
    label: str | None = None  # null/empty clears the custom name


@app.get("/rooms")
async def list_rooms():
    async with app.state.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT code, device_name, label, display_name FROM room_names ORDER BY code"
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in await cur.fetchall()]


@app.put("/rooms/{code}")
async def set_room_label(code: str, body: RoomLabel):
    label = (body.label or "").strip() or None
    async with app.state.pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE rooms SET label = %s WHERE code = %s", (label, code)
        )
        if cur.rowcount == 0:
            raise HTTPException(404, f"unknown room {code}")
    return {"code": code, "label": label}


# Catch-all: store any other request verbatim so we can see what the sensor
# server sends. Must be registered last.
@app.api_route(
    "/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
)
async def capture(path: str, request: Request):
    body = (await request.body()).decode("utf-8", errors="replace")
    client = request.client.host if request.client else None
    async with app.state.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO raw_requests (client, method, path, query, headers, body) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                client,
                request.method,
                "/" + path,
                request.url.query or None,
                Jsonb(dict(request.headers)),
                body,
            ),
        )
    print(f"captured {request.method} /{path} from {client}: {body[:200]}", flush=True)
    return {"status": "captured"}
