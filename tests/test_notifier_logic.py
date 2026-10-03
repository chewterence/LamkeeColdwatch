"""Tests for the notifier's pure alert logic. No DB or network needed."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "notifier"))

from app.logic import (  # noqa: E402
    BAD_S, CRITICAL_S, GOOD_S, HOT, NO_READING, OPEN, RECOVER, REMIND, UNIT_OFF,
    Config, Reading, Room, State, due_summary_slot, find_rooms, format_alerts, format_event,
    format_room, format_status, format_summary, room_status, step,
)

T0 = datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc)
CFG = Config()


def room(temp=-18.0, on=1.0, age=10, fault=None, now=T0):
    t = None if temp == "missing" else Reading(now - timedelta(seconds=age), temp, fault)
    return Room("RS1-011", "Room 8", t, Reading(now - timedelta(seconds=age), on))


def run(sequence, cfg=CFG, states=None):
    """sequence: [(minutes, Room-builder kwargs)] -> list of (minute, events)."""
    states = states or {}
    out = []
    for minute, kw in sequence:
        now = T0 + timedelta(minutes=minute)
        r = room(now=now, **kw)
        states, evs = step(now, {r.code: r}, states, cfg)
        out.append((minute, evs))
    return out, states


def test_status_bands():
    assert room_status(T0, room(-18), CFG) == GOOD_S
    assert room_status(T0, room(-25), CFG) == GOOD_S
    assert room_status(T0, room(-30), CFG) == GOOD_S  # very cold is not an alert
    assert room_status(T0, room(-14), CFG) == GOOD_S  # exactly -14 is still good
    assert room_status(T0, room(-13.9), CFG) == BAD_S
    assert room_status(T0, room(-10), CFG) == BAD_S
    assert room_status(T0, room(5), CFG) == BAD_S
    assert room_status(T0, room(-18, age=400), CFG) == CRITICAL_S
    assert room_status(T0, room("missing"), CFG) == CRITICAL_S


def test_good_never_alerts():
    for temp in (-14, -20, -30):
        res, _ = run([(m, {"temp": temp}) for m in range(0, 60)])
        assert all(not evs for _, evs in res)


def test_every_bad_label_alerts_after_ten_minutes():
    for temp in (-13.9, -12, -10, -8):
        res, _ = run([(m, {"temp": temp}) for m in range(0, 12)])
        assert [(m, e.type) for m, evs in res for e in evs] == [(10, OPEN)]


def test_bad_needs_ten_minutes_then_alerts_once():
    res, _ = run([(m, {"temp": -8.0}) for m in range(0, 15)])
    fired = [(m, e) for m, evs in res for e in evs]
    assert len(fired) == 1
    m, ev = fired[0]
    assert m == 10 and ev.type == OPEN and ev.kinds == (HOT,) and ev.severity == BAD_S


def test_good_reading_resets_the_bad_timer():
    seq = [(m, {"temp": -8.0}) for m in range(0, 9)] + [(9, {"temp": -18.0})]
    seq += [(m, {"temp": -8.0}) for m in range(10, 19)]
    res, _ = run(seq)
    assert all(not evs for _, evs in res)


def test_reminder_every_hour_and_recovery():
    seq = [(m, {"temp": -8.0}) for m in range(0, 140)] + [(140, {"temp": -18.0})]
    res, _ = run(seq)
    kinds = [(m, e.type) for m, evs in res for e in evs]
    assert kinds == [(10, OPEN), (70, REMIND), (130, REMIND), (140, RECOVER)]


def test_unit_off_alerts_immediately():
    res, _ = run([(0, {"on": 1.0}), (1, {"on": 0.0})])
    assert res[0][1] == []
    ev = res[1][1][0]
    assert ev.type == OPEN and ev.kinds == (UNIT_OFF,) and ev.severity == CRITICAL_S
    assert "UNIT OFF" in format_event(ev, "Room 8")


def test_unit_off_recovers_when_back_on():
    res, _ = run([(0, {"on": 0.0}), (5, {"on": 1.0})])
    assert res[1][1][0].type == RECOVER and res[1][1][0].kinds == (UNIT_OFF,)


def test_unit_off_fires_first_then_hot_follows_separately():
    res, _ = run([(m, {"temp": -8.0, "on": 0.0}) for m in range(0, 12)])
    fired = [(m, e.kinds) for m, evs in res for e in evs]
    assert fired == [(0, (UNIT_OFF,)), (10, (HOT,))]


def test_combined_when_both_open_in_same_tick():
    cfg = Config(unit_off_for_s=600)
    res, _ = run([(m, {"temp": -8.0, "on": 0.0}) for m in range(0, 12)], cfg=cfg)
    evs = [e for _, es in res for e in es]
    assert len(evs) == 1 and evs[0].kinds == (HOT, UNIT_OFF)
    text = format_event(evs[0], "Room 8")
    assert "UNIT OFF" in text and "-8.0°C" in text and text.startswith("❌ CRITICAL")


def test_no_reading_after_five_minutes_of_silence():
    seq = [(0, {"temp": -18.0})]
    # the room's last reading stays at minute 0; later ticks see it get older
    states, out = {}, []
    r0 = room(now=T0)
    for minute in range(0, 10):
        now = T0 + timedelta(minutes=minute)
        states, evs = step(now, {r0.code: r0}, states, CFG)
        out += [(minute, e) for e in evs]
    assert len(out) == 1
    minute, ev = out[0]
    assert minute == 5 and ev.kinds == (NO_READING,) and ev.severity == CRITICAL_S
    assert "no reading for 5 min" in format_event(ev, "Room 8")


def test_missing_room_is_critical():
    now = T0
    r = Room("RS1-011", "Room 8", None, None)
    _, evs = step(now, {r.code: r}, {}, CFG)
    assert evs and evs[0].kinds == (NO_READING,)


def test_fault_marker_alerts_after_debounce():
    res, _ = run([(m, {"temp": None, "fault": "#"}) for m in range(0, 8)])
    fired = [(m, e) for m, evs in res for e in evs]
    assert len(fired) == 1 and fired[0][0] == 5
    assert "sensor fault '#'" in format_event(fired[0][1], "Room 8")


def test_recovery_message_mentions_duration():
    res, _ = run([(m, {"temp": -8.0}) for m in range(0, 20)] + [(20, {"temp": -17.5})])
    ev = res[-1][1][0]
    text = format_event(ev, "Room 8")
    assert text.startswith("✅ RECOVERED") and "-17.5°C" in text and "20 min" in text


def test_silent_freezes_state_and_sends_nothing():
    res, states = run([(m, {"temp": -8.0}) for m in range(0, 11)])
    assert states
    now = T0 + timedelta(hours=3)
    stale = room(temp=-8.0, age=10800, now=now)
    new, evs = step(now, {stale.code: stale}, states, CFG, silent=True)
    assert new == states and evs == []


def test_startup_grace_suppresses_new_alerts_then_catches_up():
    now = T0
    r = room(temp=-8.0, now=now)
    states = {}
    for minute in range(0, 12):
        now = T0 + timedelta(minutes=minute)
        r = room(temp=-8.0, now=now)
        states, evs = step(now, {r.code: r}, states, CFG, can_notify=False)
        assert evs == []
    states, evs = step(now, {r.code: r}, states, CFG, can_notify=True)
    assert len(evs) == 1 and evs[0].type == OPEN


def test_state_survives_restart():
    res, states = run([(m, {"temp": -8.0}) for m in range(0, 11)])
    assert sum(len(e) for _, e in res) == 1
    # "restart": same states handed back in, no second OPEN
    res2, _ = run([(m, {"temp": -8.0}) for m in range(11, 20)], states=states)
    assert all(not evs for _, evs in res2)


def test_thresholds_are_configurable():
    cfg = Config(bad_min_c=-5.0, bad_for_s=0)
    assert room_status(T0, room(-8), cfg) == GOOD_S
    _, evs = step(T0, {"RS1-011": room(-4)}, {}, cfg)
    assert evs and evs[0].kinds == (HOT,)


def test_config_from_rows():
    cfg = Config.from_rows({"bad_min_c": "-8", "bad_for_s": "120", "summary_times": "07:30, 19:00"})
    assert cfg.bad_min_c == -8 and cfg.bad_for_s == 120 and cfg.summary_times == ("07:30", "19:00")


def test_summary_slots():
    sgt = timezone(timedelta(hours=8))
    at = lambda h, m: datetime(2026, 10, 3, h, m, tzinfo=sgt)
    assert due_summary_slot(at(7, 59), CFG, "") is None
    assert due_summary_slot(at(8, 0), CFG, "") == "2026-10-03T08:00"
    assert due_summary_slot(at(8, 5), CFG, "2026-10-03T08:00") is None
    assert due_summary_slot(at(10, 30), CFG, "") is None  # missed by over 2 h: skipped
    assert due_summary_slot(at(18, 1), CFG, "2026-10-03T08:00") == "2026-10-03T18:00"


def test_summary_lists_problems_first():
    rows = [
        {"code": "RS1-001", "name": "Room 1", "status": GOOD_S, "temp": -18.2, "on": 1.0, "lo": -19.0, "hi": -17.0},
        {"code": "RS1-011", "name": "Room 8", "status": BAD_S, "temp": -8.4, "on": 1.0, "lo": -18.0, "hi": -8.0},
        {"code": "RS1-002", "name": "Room 2", "status": GOOD_S, "temp": -17.0, "on": 0.0, "lo": None, "hi": None},
    ]
    text = format_summary(T0, CFG, rows, 12)
    assert text.index("Problems (2)") < text.index("Good (1)")
    assert "Last packet: 12 s ago" in text and "UNIT OFF" in text


def test_format_status_lists_every_room_in_order():
    base = {"lo": None, "hi": None, "age": 5}
    rows = [
        {**base, "code": "RS1-001", "name": "Room 1", "status": GOOD_S, "temp": -18.2, "on": 1.0},
        {**base, "code": "RS1-011", "name": "Room 8", "status": BAD_S, "temp": -8.4, "on": 0.0},
        {**base, "code": "RS1-002", "name": "Room 2", "status": CRITICAL_S, "temp": None, "on": None},
    ]
    text = format_status(T0, CFG, rows, 14).splitlines()
    assert text[0] == "❄️ **Lamkee Coldwatch Status** ❄️:" and text[1] == "📆 **Sat 3 Oct 08:00**" and text[2] == ""
    assert text[-2:] == ["", "__Last data: 14 s ago__"]
    assert text[3:-2] == ["Room 1: -18.2°C • ✅ GOOD",
                     "Room 8: -8.4°C • ⚠️ BAD • UNIT OFF",
                     "Room 2: no reading • ❌ CRITICAL"]


def test_find_rooms_by_code_name_and_number():
    rooms = {c: Room(c, n, None, None) for c, n in
             [("RS1-001", "Room 1"), ("RS1-010", "Room 10"), ("RS1-011", "Room 8")]}
    assert [r.code for r in find_rooms(rooms, "rs1-011")] == ["RS1-011"]
    assert [r.code for r in find_rooms(rooms, "room 1")] == ["RS1-001"]  # exact beats substring
    assert [r.code for r in find_rooms(rooms, "8")] == ["RS1-011"]
    assert [r.code for r in find_rooms(rooms, "10")] == ["RS1-010"]
    assert len(find_rooms(rooms, "room")) == 3
    assert find_rooms(rooms, "nope") == [] and find_rooms(rooms, " ") == []


def test_format_room_detail():
    row = {"code": "RS1-011", "name": "Room 8", "status": BAD_S, "temp": -8.4, "on": 0.0,
           "lo": -18.0, "hi": -8.0, "age": 20}
    text = format_room(T0, row)
    assert "Room 8 (RS1-011)" in text and "BAD" in text and "-8.4°C (updated 20 s ago)" in text
    assert "Unit: OFF" in text and "-18.0 to -8.0" in text
    gone = format_room(T0, {**row, "temp": None, "status": CRITICAL_S, "on": None, "lo": None, "age": None})
    assert "Temp: no reading" in gone and "Unit: unknown" in gone


def test_format_alerts_lists_only_notified_states():
    rooms = {"RS1-011": Room("RS1-011", "Room 8", None, None)}
    assert format_alerts(T0, rooms, {}) == "✅ No open alerts"
    since = T0 - timedelta(minutes=25)
    states = {("RS1-011", HOT): State(since, notified_at=since),
              ("RS1-011", UNIT_OFF): State(since)}  # not yet notified: hidden
    text = format_alerts(T0, rooms, states)
    assert "Open alerts (1)" in text and "Room 8" in text and "too warm for 25 min" in text
    assert "unit off" not in text


def test_bold_markup_per_channel():
    import pytest
    pytest.importorskip("httpx")  # senders imports it; the test venv may not have it
    from app.senders import plain, telegram_html
    assert telegram_html("**Hi** <a> & b") == "<b>Hi</b> &lt;a&gt; &amp; b"
    assert plain("**Hi** there") == "Hi there"
    assert telegram_html("__Last data: 5 s ago__") == "<i>Last data: 5 s ago</i>"
    assert plain("__x__ **y**") == "x y"
