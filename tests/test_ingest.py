"""Regression tests for the UDP ingest path.

Run against the live stack (`docker compose up -d`):

    pip install pytest
    pytest tests -v

Test packets use the identifier "RegressionTest" and sensor times in the year
2000, and are deleted before and after every test, so real data is untouched.
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UDP_ADDR = ("127.0.0.1", 2049)
IDENT = "RegressionTest"
TEST_TIME = "2000-01-01 00:00:00"


# ---------- helpers ----------

def _env():
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def sql(query: str) -> list[list[str]]:
    env = _env()
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "db", "psql", "-U", env["POSTGRES_USER"],
         "-d", env["POSTGRES_DB"], "-At", "-F", "|", "-c", query],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [line.split("|") for line in out.splitlines() if line]


def send(payload: str):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(payload.encode(), UDP_ADDR)


def packet(values, ts=TEST_TIME, status="0000", count=None):
    count = len(values) if count is None else count
    return f"{IDENT},{count},{ts},{','.join(map(str, values))},{status}"


def wait_for_packet(timeout=5):
    """Return (id, error) of the test packet once the ingest has stored it."""
    end = time.time() + timeout
    while time.time() < end:
        rows = sql(f"SELECT id, coalesce(error,'') FROM packets WHERE client IS NOT NULL "
                   f"AND (identifier='{IDENT}' OR raw LIKE '{IDENT}%' OR raw LIKE 'garbage%') "
                   f"ORDER BY id DESC LIMIT 1")
        if rows:
            return int(rows[0][0]), rows[0][1]
        time.sleep(0.2)
    raise AssertionError("packet was not stored within timeout")


def readings(packet_id):
    """{(room, point): (value, fault)} for a packet."""
    rows = sql(f"SELECT sensor_id, point, coalesce(value::text,''), coalesce(fault,'') "
               f"FROM readings WHERE packet_id={packet_id}")
    return {(r[0], r[1]): (r[2], r[3]) for r in rows}


def active_sensors():
    return [(int(p), c, pt) for p, c, pt in
            sql("SELECT position, code, point FROM sensors WHERE active ORDER BY position")]


def _cleanup():
    sql("DELETE FROM readings WHERE packet_id IN (SELECT id FROM packets "
        f"WHERE identifier='{IDENT}' OR raw LIKE '{IDENT}%' OR raw LIKE 'garbage%')")
    sql(f"DELETE FROM packets WHERE identifier='{IDENT}' OR raw LIKE '{IDENT}%' OR raw LIKE 'garbage%'")


@pytest.fixture(autouse=True)
def clean():
    _cleanup()
    yield
    _cleanup()


# ---------- parser (no stack needed) ----------

@pytest.fixture(scope="module")
def parse():
    pytest.importorskip("psycopg_pool", reason="parser tests need ingest deps on host")
    sys.path.insert(0, str(ROOT / "ingest"))
    from app.udp import parse
    return parse


def test_parse_ok(parse):
    ident, count, t, values, status = parse(packet(["1.5", "-", "2"]))
    assert (ident, count, values, status) == (IDENT, 3, ["1.5", "-", "2"], "0000")
    assert t.year == 2000


def test_parse_count_mismatch(parse):
    with pytest.raises(ValueError):
        parse(packet(["1", "2"], count=5))


def test_parse_bad_timestamp(parse):
    with pytest.raises(ValueError):
        parse(packet(["1"], ts="not-a-time"))


def test_parse_too_short(parse):
    with pytest.raises(ValueError):
        parse("garbage")


# ---------- end-to-end ----------

def test_sensors_table_is_contiguous():
    sensors = active_sensors()
    assert [p for p, _, _ in sensors] == list(range(len(sensors)))
    assert len(sensors) == 51


def test_all_17_rooms_configured():
    codes = {c for _, c, pt in active_sensors() if pt == "temp"}
    assert len(codes) == 17
    assert {c for _, c, pt in active_sensors() if pt == "on"} == codes


@pytest.mark.parametrize("position,code,point", active_sensors(),
                         ids=lambda v: str(v))
def test_each_sensor_is_ingested(position, code, point):
    """Every position gets its own unique value; check it lands on the right room."""
    values = [f"{pos + 0.5:.1f}" for pos, _, _ in active_sensors()]
    send(packet(values))
    pid, err = wait_for_packet()
    assert err == ""
    got = readings(pid)
    assert len(got) == len(values)
    assert got[(code, point)] == (f"{position + 0.5:.1f}", "")


def test_realistic_packet_stores_all_rooms():
    sensors = active_sensors()
    values = [(-18.0 - i * 0.1) if pt == "temp" else 1
              for i, (_, _, pt) in enumerate(sensors)]
    send(packet(values))
    pid, err = wait_for_packet()
    assert err == ""
    got = readings(pid)
    rooms_with_temp = {c for (c, pt), (v, _) in got.items() if pt == "temp" and v}
    rooms_with_on = {c for (c, pt), (v, _) in got.items() if pt == "on" and v}
    assert len(rooms_with_temp) == 17
    assert len(rooms_with_on) == 17


@pytest.mark.parametrize("marker", ["-", "*", "#", "/"])
def test_fault_markers_stored_as_null_with_marker(marker):
    sensors = active_sensors()
    values = [marker if i == 0 else "1.0" for i in range(len(sensors))]
    send(packet(values))
    pid, err = wait_for_packet()
    assert err == ""
    first = sensors[0]
    assert readings(pid)[(first[1], first[2])] == ("", marker)


def test_wrong_value_count_is_rejected_without_readings():
    send(packet(["1.0"] * 22))
    pid, err = wait_for_packet()
    assert "readings not stored" in err
    assert readings(pid) == {}


def test_garbage_packet_is_recorded_as_error():
    send("garbage")
    pid, err = wait_for_packet()
    assert err.startswith("unparsable")
    assert readings(pid) == {}


def test_listener_survives_bad_packet():
    send("garbage")
    wait_for_packet()
    _cleanup()
    send(packet(["1.0"] * len(active_sensors())))
    pid, err = wait_for_packet()
    assert err == ""
    assert len(readings(pid)) == 51


def test_sensor_time_is_stored():
    send(packet(["1.0"] * len(active_sensors()), ts="2000-02-03 04:05:06"))
    pid, _ = wait_for_packet()
    assert sql(f"SELECT DISTINCT sensor_time FROM readings WHERE packet_id={pid}") == [
        ["2000-02-03 04:05:06"]]
