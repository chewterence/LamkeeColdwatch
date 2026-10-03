"""Tests for the notifier's pure alert logic. No DB or network needed."""
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "notifier"))

from app.logic import (  # noqa: E402
    BAD_S, CRITICAL_S, GOOD_S, HOT, NO_READING, OPEN, RECOVER, REMIND, UNIT_OFF,
    Config, Reading, Room, State, due_summary_slot, find_rooms, format_alerts, format_event,
    format_room, format_status, format_summary, room_status, step,
    DEFROST_END_EV, DEFROST_START_EV, DefrostEvent, defrost_step, format_defrost_event,
)

T0 = datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc)
# Mechanics tests use the original numbers; the live defaults are covered by the DEFAULTS tests at the bottom.
CFG = Config(bad_min_c=-14.0, bad_for_s=600, bad_remind_s=3600)


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
    cfg = replace(CFG, unit_off_for_s=600)
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
    assert due_summary_slot(at(6, 59), CFG, "") is None
    assert due_summary_slot(at(7, 0), CFG, "") == "2026-10-03T07:00"
    assert due_summary_slot(at(7, 5), CFG, "2026-10-03T07:00") is None
    assert due_summary_slot(at(10, 30), CFG, "") is None  # missed by over 2 h: skipped
    assert due_summary_slot(at(20, 1), CFG, "2026-10-03T07:00") == "2026-10-03T20:00"


def test_summary_lists_problems_first():
    rows = [
        {"code": "RS1-001", "name": "Room 1", "status": GOOD_S, "temp": -18.2, "on": 1.0, "lo": -19.0, "hi": -17.0},
        {"code": "RS1-011", "name": "Room 8", "status": BAD_S, "temp": -8.4, "on": 1.0, "lo": -18.0, "hi": -8.0},
        {"code": "RS1-002", "name": "Room 2", "status": GOOD_S, "temp": -17.0, "on": 0.0, "lo": None, "hi": None},
    ]
    text = format_summary(T0, CFG, rows, 12)
    assert text.index("Problems (2)") < text.index("Good (1)")
    assert "Last packet: 12 s ago" in text and "UNIT OFF" in text


def test_format_status_groups_rooms_and_puts_date_last():
    base = {"lo": None, "hi": None, "age": 5}
    def row(name, status, temp, on=1.0):
        return {**base, "code": name, "name": name, "status": status, "temp": temp, "on": on}
    rows = [
        row("Room 1", GOOD_S, -18.2),
        row("Room 5 System 2", GOOD_S, -16.2),
        row("Room 8", BAD_S, -8.4, on=0.0),
        row("Room 5 (Small System)", GOOD_S, -14.5),
        row("Room 5 System 1", GOOD_S, -16.6),
        row("Room 2", CRITICAL_S, None, on=None),
        row("Room 1 System 2", BAD_S, -12.0),
        row("Room 6 Blast Freezer", GOOD_S, -25.0),
    ]
    text = format_status(T0, CFG, rows, 14).splitlines()
    assert text[:2] == ["❄️ **Coldroom Status**:", ""]
    assert text[-2:] == ["", "📆 Sat 3 Oct 8:00 A.M."]
    assert text[2:-2] == [
        "-----------------",
        "**Room 1**",
        "Main: -18.2°C • ✅",
        "System 2: -12.0°C • ⚠️",
        "-----------------",
        "**Room 5**",
        "System 1: -16.6°C • ✅",
        "System 2: -16.2°C • ✅",
        "Small System: -14.5°C • ✅",
        "-----------------",
        "**Room 8**",
        "-8.4°C • ⚠️ • UNIT OFF",
        "-----------------",
        "**Room 2**",
        "no reading • ❌",
        "-----------------",
        "**Room 6**",
        "Blast Freezer: -25.0°C • ✅",
        "-----------------",
    ]


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
    assert "Room 8 (RS1-011)" in text and "⚠️" in text and "-8.4°C (updated 20 s ago)" in text
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


# ---- defrost ----------------------------------------------------------------

def drooms(defrost, temp=-9.0, minute=0):
    now = T0 + timedelta(minutes=minute)
    d = None if defrost is None else Reading(now - timedelta(seconds=5), defrost)
    r = Room("RS1-002", "Room 2", Reading(now - timedelta(seconds=5), temp), Reading(now, 1.0), d)
    return now, {r.code: r}


def run_defrost(seq, **kw):
    """seq: [(minute, defrost value, temp)] -> list of (minute, events)."""
    states, out = {}, []
    for minute, d, temp in seq:
        now, rooms = drooms(d, temp, minute)
        states, evs = defrost_step(now, rooms, states, CFG, **kw)
        out.append((minute, evs))
    return out, states


def test_defrost_start_and_finish_with_duration_and_temp():
    seq = [(m, 0, -18) for m in range(0, 3)] + [(m, 1, -9) for m in range(3, 25)] \
        + [(m, 0, -9.1) for m in range(25, 30)]
    res, states = run_defrost(seq)
    evs = [(m, e) for m, es in res for e in es]
    assert [(m, e.type) for m, e in evs] == [(4, DEFROST_START_EV), (26, DEFROST_END_EV)]
    end = evs[1][1]
    assert end.temp == -9.1
    assert round(end.duration_s / 60) == 22  # first reading at 1 (min 3) to first back at 0 (min 25)
    assert states == {}
    assert format_defrost_event(end, "Room 2", "RS1-002") == \
        "✅ FINISHED DEFROST\n**Room 2** (RS1-002)\n(now -9.1°C)"
    assert "🧊 START DEFROST\n**Room 2** (RS1-002)\n(now -9.0°C)" in format_defrost_event(evs[0][1], "Room 2", "RS1-002")


def test_defrost_blip_is_ignored():
    res, _ = run_defrost([(0, 0, -18), (1, 1, -18), (2, 0, -18), (3, 0, -18)])
    assert all(not es for _, es in res)


def test_defrost_baseline_is_silent_and_finish_has_no_duration():
    res, states = run_defrost([(m, 1, -9) for m in range(0, 5)] + [(m, 0, -9) for m in range(5, 9)],
                              baseline=True)
    evs = [e for _, es in res for e in es]
    assert [e.type for e in evs] == [DEFROST_END_EV]
    assert evs[0].duration_s is None
    assert format_defrost_event(evs[0], "Room 2", "RS1-002").endswith("(now -9.0°C)")


def test_defrost_ignores_fault_missing_and_silent():
    res, _ = run_defrost([(m, None, -9) for m in range(5)])
    assert all(not es for _, es in res)
    now, rooms = drooms(1, minute=0)
    assert defrost_step(now, rooms, {}, CFG, silent=True) == ({}, [])


def test_defrost_waits_for_startup_grace():
    res, states = run_defrost([(m, 1, -9) for m in range(5)], can_notify=False)
    assert all(not es for _, es in res)
    now, rooms = drooms(1, minute=5)
    _, evs = defrost_step(now, rooms, states, CFG)
    assert [e.type for e in evs] == [DEFROST_START_EV]


def test_bad_alert_paused_during_defrost_then_counts_again():
    states, opened = {}, []
    for m in range(0, 60):
        now, rooms = drooms(1 if 5 <= m < 40 else 0, temp=-8.0, minute=m)
        states, evs = step(now, rooms, states, CFG)
        opened += [(m, e.type) for e in evs]
    # hot from minute 0: still counting at minute 5 when defrost starts (10 min rule is reset),
    # then counts again from minute 40 and opens at 50
    assert opened == [(50, OPEN)]


def test_open_bad_alert_survives_defrost_without_recovering():
    states, seen = {}, []
    for m in range(0, 40):
        now, rooms = drooms(1 if m >= 15 else 0, temp=-8.0, minute=m)
        states, evs = step(now, rooms, states, CFG)
        seen += [(m, e.type) for e in evs]
    assert seen == [(10, OPEN)]  # no RECOVER, no REMIND while defrosting


def test_status_shows_defrost_marker_and_not_bad():
    now, rooms = drooms(1, temp=-8.0)
    r = rooms["RS1-002"]
    assert room_status(now, r, CFG) == GOOD_S
    row = {"code": r.code, "name": r.name, "status": GOOD_S, "temp": -8.0, "on": 1.0,
           "lo": None, "hi": None, "defrost": True, "age": 5}
    assert "🧊 DEFROSTING" in format_status(now, CFG, [row], 1)
    assert "🧊 DEFROSTING" in format_summary(now, CFG, [row], 1)
    assert "Defrost" in format_room(now, row)
    assert "DEFROSTING" not in format_status(now, CFG, [dict(row, defrost=False)], 1)


def test_no_still_bad_right_after_defrost_ends():
    # BAD opens at 10, defrost 50-100 (past the 60 min reminder due at 70), still hot after.
    states, seen = {}, []
    for m in range(0, 200):
        now, rooms = drooms(1 if 50 <= m < 100 else 0, temp=-8.0, minute=m)
        states, evs = step(now, rooms, states, CFG)
        seen += [(m, e.type) for e in evs]
    assert seen == [(10, OPEN), (159, REMIND)]  # 60 min after the last paused tick


def test_reminder_still_fires_hourly_when_no_defrost():
    states, seen = {}, []
    for m in range(0, 140):
        now, rooms = drooms(0, temp=-8.0, minute=m)
        states, evs = step(now, rooms, states, CFG)
        seen += [(m, e.type) for e in evs]
    assert seen == [(10, OPEN), (70, REMIND), (130, REMIND)]


def test_reminder_timer_restarts_for_every_defrost():
    states, seen = {}, []
    for m in range(0, 260):
        now, rooms = drooms(1 if (50 <= m < 100 or 120 <= m < 150) else 0, temp=-8.0, minute=m)
        states, evs = step(now, rooms, states, CFG)
        seen += [(m, e.type) for e in evs]
    assert seen == [(10, OPEN), (209, REMIND)]


def test_defrost_end_while_recovered_sends_recover_not_still():
    states, seen = {}, []
    for m in range(0, 120):
        now, rooms = drooms(1 if 50 <= m < 80 else 0, temp=-8.0 if m < 85 else -20.0, minute=m)
        states, evs = step(now, rooms, states, CFG)
        seen += [(m, e.type) for e in evs]
    assert seen == [(10, OPEN), (85, RECOVER)]


def test_paused_alert_keeps_original_start_time():
    states = {}
    for m in range(0, 70):
        now, rooms = drooms(1 if m >= 50 else 0, temp=-8.0, minute=m)
        states, _ = step(now, rooms, states, CFG)
    st = states[("RS1-002", HOT)]
    assert (now - st.since).total_seconds() == 69 * 60  # duration still counts from the first hot reading
    assert st.last_notified_at == now


# --- DEFAULTS: the live rules (BAD above -12C for 2 h, STILL BAD every 15 min, CRITICAL hourly) ---

DEF = Config()


def test_defaults_are_the_live_rules():
    assert (DEF.bad_min_c, DEF.bad_for_s, DEF.bad_remind_s, DEF.remind_s) == (-12.0, 7200, 900, 3600)


def test_default_bad_threshold_boundary():
    assert room_status(T0, room(-12), DEF) == GOOD_S  # exactly -12 is good
    assert room_status(T0, room(-11.9), DEF) == BAD_S
    assert room_status(T0, room(-13), DEF) == GOOD_S


def test_default_bad_waits_two_hours_then_repeats_every_fifteen_minutes():
    res, _ = run([(m, {"temp": -8.0}) for m in range(0, 170)], cfg=DEF)
    seen = [(m, e.type) for m, evs in res for e in evs]
    assert seen == [(120, OPEN), (135, REMIND), (150, REMIND), (165, REMIND)]


def test_default_warm_for_under_two_hours_never_alerts():
    res, _ = run([(m, {"temp": -8.0}) for m in range(0, 119)], cfg=DEF)
    assert all(not evs for _, evs in res)


def test_default_bad_messages_use_exclamation_icon():
    res, _ = run([(m, {"temp": -8.4}) for m in range(0, 136)], cfg=DEF)
    opened, still = [e for _, evs in res for e in evs]
    assert format_event(opened, "Room 8") == "❗ BAD\n**Room 8 (RS1-011)**\n-8.4°C for 2 h"
    assert format_event(still, "Room 8") == "❗ STILL BAD\n**Room 8 (RS1-011)**\n-8.4°C for 2 h 15 min"


def test_critical_reminders_stay_hourly_with_old_icon():
    seq = [(m, {"temp": -20.0, "on": 0.0}) for m in range(0, 130)]
    res, _ = run(seq, cfg=DEF)
    evs = [(m, e) for m, es in res for e in es]
    assert [(m, e.type) for m, e in evs] == [(0, OPEN), (60, REMIND), (120, REMIND)]
    assert format_event(evs[0][1], "Room 8").startswith("❌ CRITICAL")
    assert format_event(evs[1][1], "Room 8").startswith("🔁 STILL CRITICAL")


def test_default_defrost_pauses_bad_and_still_resumes_after_ten_minutes():
    states, seen = {}, []
    for m in range(0, 300):
        now, rooms = drooms(1 if 130 <= m < 150 else 0, temp=-8.0, minute=m)
        states, evs = step(now, rooms, states, DEF)
        seen += [(m, e.type) for e in evs]
    # OPEN at 2 h; the 130 reminder is held back by the defrost; timer restarts at 149
    assert seen[0] == (120, OPEN) and seen[1][1] == REMIND and seen[1][0] >= 159
