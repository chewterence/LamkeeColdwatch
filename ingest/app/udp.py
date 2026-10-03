import asyncio
from datetime import datetime

from psycopg_pool import AsyncConnectionPool

FAULT_MARKERS = {"-", "*", "#", "/"}


def parse(line: str):
    """Parse 'ident,count,timestamp,v1..vN,status' into its parts.

    Returns (ident, count, sensor_time, values, status); raises ValueError.
    """
    parts = [p.strip() for p in line.strip().split(",")]
    if len(parts) < 4:
        raise ValueError("too few fields")
    count = int(parts[1])
    sensor_time = datetime.strptime(parts[2], "%Y-%m-%d %H:%M:%S")
    if len(parts) != 4 + count:
        raise ValueError(f"count {count} but {len(parts) - 4} value fields")
    return parts[0], count, sensor_time, parts[3 : 3 + count], parts[3 + count]


async def store(pool: AsyncConnectionPool, client: str, raw: str):
    ident = count = sensor_time = status = values = error = None
    try:
        ident, count, sensor_time, values, status = parse(raw)
    except ValueError as e:
        error = f"unparsable: {e}"

    async with pool.connection() as conn:
        sensors = []
        if values is not None:
            cur = await conn.execute(
                "SELECT position, code, point FROM sensors WHERE active ORDER BY position"
            )
            sensors = await cur.fetchall()
            if [p for p, _, _ in sensors] != list(range(len(values))):
                error = (
                    f"{len(values)} values but {len(sensors)} active sensors; "
                    "readings not stored"
                )

        cur = await conn.execute(
            "INSERT INTO packets (client, raw, identifier, value_count, "
            "sensor_time, status, error) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (client, raw, ident, count, sensor_time, status, error),
        )
        packet_id = (await cur.fetchone())[0]

        if error:
            print(f"packet {packet_id} from {client}: {error}", flush=True)
            return

        rows = []
        for (_, code, point), v in zip(sensors, values):
            if v in FAULT_MARKERS:
                rows.append((code, point, None, v, sensor_time, packet_id))
                continue
            try:
                rows.append((code, point, float(v), None, sensor_time, packet_id))
            except ValueError:
                rows.append((code, point, None, v, sensor_time, packet_id))
        async with conn.cursor() as c:
            await c.executemany(
                "INSERT INTO readings (sensor_id, point, value, fault, sensor_time, packet_id) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                rows,
            )
    print(f"packet {packet_id} from {client}: {len(rows)} readings", flush=True)


class UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool
        self.tasks: set[asyncio.Task] = set()

    def datagram_received(self, data: bytes, addr):
        raw = data.decode("utf-8", errors="replace").strip()
        task = asyncio.create_task(self._safe_store(addr[0], raw))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _safe_store(self, client: str, raw: str):
        try:
            await store(self.pool, client, raw)
        except Exception as e:  # keep listening whatever happens
            print(f"failed to store packet from {client}: {e!r}: {raw[:200]}", flush=True)
