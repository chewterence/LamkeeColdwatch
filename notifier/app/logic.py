"""Alert rules, state machine and message text. Pure: no DB, no network."""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

HOT = "HOT"
NO_READING = "NO_READING"
UNIT_OFF = "UNIT_OFF"

OPEN = "OPEN"
REMIND = "REMIND"
RECOVER = "RECOVER"

GOOD_S, BAD_S, CRITICAL_S = "GOOD", "BAD", "CRITICAL"


@dataclass(frozen=True)
class Config:
    bad_min_c: float = -14.0  # warmer than this is BAD (-14.0 itself is GOOD)
    bad_for_s: int = 600
    no_reading_s: int = 300
    unit_off_for_s: int = 0
    remind_s: int = 3600
    startup_grace_s: int = 180
    summary_times: tuple = ("08:00", "18:00")
    summary_tz: str = "Asia/Singapore"

    @classmethod
    def from_rows(cls, rows: dict) -> "Config":
        d = cls()
        kw = {}
        if "bad_min_c" in rows:
            kw["bad_min_c"] = float(rows["bad_min_c"])
        for f in ("bad_for_s", "no_reading_s", "unit_off_for_s", "remind_s", "startup_grace_s"):
            if f in rows:
                kw[f] = int(float(rows[f]))
        if "summary_times" in rows:
            kw["summary_times"] = tuple(t.strip() for t in rows["summary_times"].split(",") if t.strip())
        if "summary_tz" in rows:
            kw["summary_tz"] = rows["summary_tz"].strip()
        return replace(d, **kw)


@dataclass(frozen=True)
class Reading:
    time: datetime
    value: float | None
    fault: str | None = None


@dataclass(frozen=True)
class Room:
    code: str
    name: str
    temp: Reading | None
    on: Reading | None


@dataclass(frozen=True)
class Condition:
    active: bool
    since: datetime | None = None  # when it really began, if known
    debounce_s: int = 0


@dataclass(frozen=True)
class State:
    since: datetime
    notified_at: datetime | None = None
    last_notified_at: datetime | None = None


@dataclass
class Event:
    code: str
    type: str
    kinds: tuple
    durations: dict  # kind -> seconds
    temp: float | None
    fault: str | None
    severity: str


def evaluate(now: datetime, room: Room, cfg: Config) -> dict:
    """Which conditions hold for a room right now."""
    t = room.temp
    out = {}

    if t is None:
        out[NO_READING] = Condition(True, now - timedelta(days=1))
    else:
        age = (now - t.time).total_seconds()
        if age >= cfg.no_reading_s:
            out[NO_READING] = Condition(True, t.time)
        elif t.value is None:  # fresh packet but the sensor reported a fault marker
            out[NO_READING] = Condition(True, None, cfg.no_reading_s)
        else:
            out[NO_READING] = Condition(False)

    hot = t is not None and t.value is not None and t.value > cfg.bad_min_c
    out[HOT] = Condition(hot, None, cfg.bad_for_s)

    off = room.on is not None and room.on.value is not None and room.on.value < 0.5
    out[UNIT_OFF] = Condition(off, None, cfg.unit_off_for_s)
    return out


def room_status(now: datetime, room: Room, cfg: Config) -> str:
    c = evaluate(now, room, cfg)
    if c[NO_READING].active:
        return CRITICAL_S
    return BAD_S if room.temp.value > cfg.bad_min_c else GOOD_S


def step(now, rooms: dict, states: dict, cfg: Config, can_notify=True, silent=False):
    """Advance alert state by one tick. Returns (new_states, events).

    states is keyed by (code, kind). While `silent` (no packets from anywhere)
    everything is frozen: stale data says nothing about individual rooms.
    """
    if silent:
        return dict(states), []
    new = dict(states)
    raw = []  # (code, type, kind, duration_s)
    for code, room in rooms.items():
        for kind, cond in evaluate(now, room, cfg).items():
            key = (code, kind)
            st = new.get(key)
            if cond.active:
                if st is None:
                    st = State(since=cond.since or now)
                dur = (now - st.since).total_seconds()
                if st.notified_at is None:
                    if can_notify and dur >= cond.debounce_s:
                        st = replace(st, notified_at=now, last_notified_at=now)
                        raw.append((code, OPEN, kind, dur))
                elif (now - st.last_notified_at).total_seconds() >= cfg.remind_s:
                    st = replace(st, last_notified_at=now)
                    raw.append((code, REMIND, kind, dur))
                new[key] = st
            elif st is not None:
                if st.notified_at is not None:
                    raw.append((code, RECOVER, kind, (now - st.since).total_seconds()))
                del new[key]

    grouped = {}
    for code, typ, kind, dur in raw:
        grouped.setdefault((code, typ), {})[kind] = dur
    events = []
    for (code, typ), durs in grouped.items():
        room = rooms[code]
        kinds = tuple(sorted(durs))
        sev = BAD_S if kinds == (HOT,) else CRITICAL_S
        events.append(Event(
            code=code, type=typ, kinds=kinds, durations=durs,
            temp=room.temp.value if room.temp else None,
            fault=room.temp.fault if room.temp else None,
            severity=sev,
        ))
    return new, events


def fmt_duration(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 1:
        return "<1 min"
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min" if m else f"{h} h"


_ICON = {BAD_S: "⚠️", CRITICAL_S: "❌"}
_WAS = {HOT: "too warm", NO_READING: "no reading", UNIT_OFF: "unit off"}


def format_event(ev: Event, name: str) -> str:
    who = f"{name} ({ev.code})"
    if ev.type == RECOVER:
        t = f"{ev.temp:.1f}°C" if ev.temp is not None else "back online"
        was = ", ".join(_WAS[k] for k in ev.kinds)
        return f"✅ RECOVERED • {who}: {t} (was {was} for {fmt_duration(max(ev.durations.values()))})"

    parts = []
    for kind in ev.kinds:
        dur = ev.durations[kind]
        long = f" for {fmt_duration(dur)}" if dur >= 60 else ""
        if kind == UNIT_OFF:
            parts.append(f"UNIT OFF{long}")
        elif kind == NO_READING:
            if ev.fault and dur < 86400:
                parts.append(f"sensor fault '{ev.fault}'{long}")
            else:
                parts.append(f"no reading{long}")
        elif kind == HOT:
            parts.append(f"{ev.temp:.1f}°C{long}")
    head = f"{_ICON[ev.severity]} {ev.severity}"
    if ev.type == REMIND:
        head = f"🔁 STILL {ev.severity}"
    return f"{head} • {who}: " + ", ".join(parts)


def due_summary_slot(now: datetime, cfg: Config, last_slot: str) -> str | None:
    """Slot id like '2026-10-03T08:00' if a summary is due and not yet sent.

    A slot more than 2 hours old is skipped (e.g. after a long outage).
    """
    local = now.astimezone(ZoneInfo(cfg.summary_tz))
    best = None
    for hhmm in cfg.summary_times:
        h, m = (int(x) for x in hhmm.split(":"))
        at = local.replace(hour=h, minute=m, second=0, microsecond=0)
        if at <= local and (best is None or at > best):
            best = at
    if best is None or local - best > timedelta(hours=2):
        return None
    slot = best.strftime("%Y-%m-%dT%H:%M")
    return slot if slot > last_slot else None


def format_summary(now: datetime, cfg: Config, rows: list, last_packet_age_s: float | None) -> str:
    """rows: dicts with name, code, status, temp, on, lo, hi (24 h min/max)."""
    local = now.astimezone(ZoneInfo(cfg.summary_tz))
    lines = [f"📋 Coldwatch summary • {local.strftime('%a %-d %b %H:%M')}"]
    if last_packet_age_s is None:
        lines.append("Last packet: none received")
    else:
        lines.append(f"Last packet: {fmt_duration(last_packet_age_s) if last_packet_age_s >= 60 else f'{int(last_packet_age_s)} s'} ago")

    def line(r):
        t = f"{r['temp']:.1f}°C" if r["temp"] is not None else "no reading"
        extra = " • UNIT OFF" if r["on"] is not None and r["on"] < 0.5 else ""
        rng = f" (24 h {r['lo']:.1f} to {r['hi']:.1f})" if r["lo"] is not None else ""
        return f"• {r['name']}: {t}{rng}{extra}"

    def off(r):
        return r["on"] is not None and r["on"] < 0.5

    problems = [r for r in rows if r["status"] in (BAD_S, CRITICAL_S) or off(r)]
    good = [r for r in rows if r["status"] == GOOD_S and r not in problems]
    if problems:
        lines += ["", f"❌ Problems ({len(problems)})"] + [line(r) for r in problems]
    lines += ["", f"✅ Good ({len(good)})"] + [line(r) for r in good]
    return "\n".join(lines)


_STATUS_ICON = {GOOD_S: "✅", BAD_S: "⚠️", CRITICAL_S: "❌"}


def format_status(now: datetime, cfg: Config, rows: list, last_packet_age_s: float | None) -> str:
    """Every room, in sensor order: temperature and GOOD / BAD / CRITICAL."""
    local = now.astimezone(ZoneInfo(cfg.summary_tz))
    lines = ["❄️ **Lamkee Coldwatch Status** ❄️:", f"📆 **{local.strftime('%a %-d %b %H:%M')}**", ""]
    for r in rows:
        t = f"{r['temp']:.1f}°C" if r["temp"] is not None else "no reading"
        off = " • UNIT OFF" if r["on"] is not None and r["on"] < 0.5 else ""
        lines.append(f"{r['name']}: {t} • {_STATUS_ICON[r['status']]} {r['status']}{off}")
    if last_packet_age_s is None:
        age = "none received"
    else:
        age = f"{fmt_duration(last_packet_age_s) if last_packet_age_s >= 60 else f'{int(last_packet_age_s)} s'} ago"
    return "\n".join(lines + ["", f"__Last data: {age}__"])


def format_room(now: datetime, row: dict) -> str:
    """Detail for one room. row: a summary row plus 'age' (seconds since last temp reading)."""
    lines = [f"🧊 {row['name']} ({row['code']})", f"Status: {_STATUS_ICON[row['status']]} {row['status']}"]
    if row["temp"] is not None:
        secs = row["age"]
        ago = fmt_duration(secs) if secs >= 60 else f"{int(secs)} s"
        lines.append(f"Temp: {row['temp']:.1f}°C (updated {ago} ago)")
    else:
        lines.append("Temp: no reading")
    if row["on"] is None:
        lines.append("Unit: unknown")
    else:
        lines.append("Unit: OFF" if row["on"] < 0.5 else "Unit: ON")
    if row["lo"] is not None:
        lines.append(f"Last 24 h: {row['lo']:.1f} to {row['hi']:.1f}°C")
    return "\n".join(lines)


def format_alerts(now: datetime, rooms: dict, states: dict) -> str:
    """Alerts that have been notified and not yet recovered."""
    per_room = {}
    for (code, kind), st in states.items():
        if st.notified_at is not None:
            per_room.setdefault(code, []).append((kind, (now - st.since).total_seconds()))
    if not per_room:
        return "✅ No open alerts"
    lines = [f"🚨 Open alerts ({len(per_room)})"]
    for code in sorted(per_room):
        name = rooms[code].name if code in rooms else code
        what = ", ".join(f"{_WAS[k]} for {fmt_duration(d)}" for k, d in sorted(per_room[code]))
        lines.append(f"• {name} ({code}): {what}")
    return "\n".join(lines)


def find_rooms(rooms: dict, query: str) -> list:
    """Rooms matching a code, a name, or the last word of a name ('8' finds 'Room 8')."""
    q = query.strip().lower()
    if not q:
        return []
    items = list(rooms.values())
    for match in (
        lambda r: q in (r.code.lower(), r.name.lower()),
        lambda r: r.name.lower().split()[-1:] == [q],
        lambda r: q in r.name.lower() or q in r.code.lower(),
    ):
        hits = [r for r in items if match(r)]
        if hits:
            return hits
    return []
