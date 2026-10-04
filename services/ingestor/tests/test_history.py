"""The PM-01 history generator follows the PLC guide (Step 6 checklist) and produces messages the
live decoder handles exactly like the real gateway's."""

import datetime as dt
import json

import pytest

from ingestor.bursts import BurstSpacer, Held, block_key
from ingestor.history import (
    PM01,
    VIB_NEW,
    Message,
    Scenario,
    d1_payload,
    d2_payload,
    f32,
    generate,
    plan_scenarios,
    vibration_baseline,
)
from ingestor.parsing import InvalidMessage, RegisterDef, TagConfig, decode_telemetry, parse_topic, reject_bad_reads

DAY = 86400
TOPIC = parse_topic("plc/Bijnor/Line1/conveyer-plc-line-01/telemetry")
REGISTER_MAP = {
    400001: RegisterDef(400001, "Machine_Status", "int16"),
    400002: RegisterDef(400002, "Machine_Speed", "float32"),
    400004: RegisterDef(400004, "Main_Motor_Current", "float32"),
    400006: RegisterDef(400006, "Dryer_Steam_Pressure", "float32"),
    400008: RegisterDef(400008, "Paper_Moisture", "float32"),
    400010: RegisterDef(400010, "Main_Bearing_Vibration", "float32"),
}
TAG_CFG = {
    "Machine_Status": TagConfig("number", value_labels={"0": "Stopped", "1": "Running"}),
    "Machine_Speed": TagConfig("number", valid_min=0, valid_max=2000),
    "Main_Motor_Current": TagConfig("number", valid_min=0, valid_max=5000),
    "Dryer_Steam_Pressure": TagConfig("number", valid_min=0, valid_max=50),
    "Paper_Moisture": TagConfig("number", valid_min=0, valid_max=100),
    "Main_Bearing_Vibration": TagConfig("number", valid_min=0, valid_max=100),
}


@pytest.fixture(scope="module")
def two_days():
    plc = PM01()
    regs = [plc.step(s) for s in range(2 * DAY)]
    return plc, regs


def test_status_is_one_while_running_and_zero_only_in_stops(two_days):
    plc, regs = two_days
    assert regs[0].status == 1
    assert {r.status for r in regs} == {0, 1}
    assert all(r.speed > 0 for r in regs if r.status == 1)
    stopped = [r for r in regs if r.status == 0]
    assert min(r.speed for r in stopped) == 0.0
    assert max(r.current for r in stopped if r.speed == 0) < 13  # ~11 A at standstill
    standstill = [r.vibration for r in stopped if r.speed == 0]
    assert 0.1 <= min(standstill) and max(standstill) < 0.45  # sensor noise floor, never one fixed value
    assert len({round(v, 3) for v in standstill}) > 50


def test_stops_per_day_and_speed_never_jumps(two_days):
    plc, regs = two_days
    assert 10 <= len(plc.stops) <= 24  # 8-9 a day on average (random)
    jumps = [abs(b.speed - a.speed) for a, b in zip(regs, regs[1:]) if a.speed and b.speed]
    assert max(jumps) < 5  # 3 m/min per second ramp plus noise


def test_every_driver_has_problem_events(two_days):
    plc, regs = two_days
    for ev in (plc.ev_speed, plc.ev_steam, plc.ev_current, plc.ev_vibration):
        assert len(ev.events) >= 6  # 4-5 a day
    running = [r for r in regs if r.status == 1]
    assert min(r.speed for r in running) < 260  # speed warning
    assert max(r.current for r in running) > 155  # current warning
    assert min(r.pressure for r in regs) < 3.5  # steam warning
    assert max(r.moisture for r in regs) > 7.0  # moisture follows steam
    assert max(r.vibration for r in running) > 4.5  # vibration warning


def test_normal_running_stays_in_normal_range():
    plc = PM01()
    regs = [plc.step(s) for s in range(3600)]  # first hour: no stop (first after 90 min), no event (4-6 h)
    assert all(274 <= r.speed <= 279 for r in regs)
    assert all(134 <= r.current <= 142 for r in regs)
    assert all(4.10 <= r.pressure <= 4.25 for r in regs)
    assert all(5.9 <= r.moisture <= 6.4 for r in regs)
    assert all(2.8 <= r.vibration <= 3.3 for r in regs)


def test_payload_matches_the_real_gateway_and_decodes():
    d1 = d1_payload([275.8, 138.5, 4.17, 6.14, 3.05])
    assert d1.decode() == (
        '{"PM3032_DATA":[{"server_id":1,"addr":2,"full_addr":"400002","size":50,'
        '"data":"[275.799988,138.500000,4.170000,6.140000,3.050000]","ip":"192.168.18.2","name":"D1"}]}'
    )
    assert d2_payload(1).decode() == (
        '{"PM3032_DATA":[{"server_id":1,"addr":1,"full_addr":"400001","size":3,"data":"[1]",'
        '"ip":"192.168.18.2","name":"D2"}]}'
    )
    now = dt.datetime.now(dt.timezone.utc)
    msg = decode_telemetry(TOPIC, d1, now, None, REGISTER_MAP)
    assert [r.tag for r in msg.readings] == ["Machine_Speed", "Main_Motor_Current", "Dryer_Steam_Pressure",
                                             "Paper_Moisture", "Main_Bearing_Vibration"]
    assert msg.readings[0].value_num == pytest.approx(f32(275.8))


def _first(kind: str):
    sc = Scenario(glitches={7: kind})
    for item in generate(0.0, 30, 12345, sc):
        if isinstance(item, Message):
            doc = json.loads(item.payload)
            if doc["PM3032_DATA"][0]["full_addr"] == ("400001" if kind == "unknown_status" else "400002"):
                yield item


@pytest.mark.parametrize("kind", ["negative_current", "garbage_speed", "unknown_status"])
def test_bad_reads_are_rejected_like_live(kind):
    now = dt.datetime.now(dt.timezone.utc)
    outcomes = []
    for item in _first(kind):
        msg = decode_telemetry(TOPIC, item.payload, now, None, REGISTER_MAP)
        _, reasons = reject_bad_reads(msg.readings, TAG_CFG.get)
        outcomes.append(bool(reasons))
    assert outcomes.count(True) == 1  # exactly the glitched poll, the rest are good


def test_nan_read_rejects_the_whole_message():
    now = dt.datetime.now(dt.timezone.utc)
    bad = 0
    for item in _first("nan"):
        try:
            decode_telemetry(TOPIC, item.payload, now, None, REGISTER_MAP)
        except InvalidMessage:
            bad += 1
    assert bad == 1


def test_gateway_bursts_and_outages():
    sc = Scenario(outages=[(100.0, 160.0, "test")])
    items = list(generate(1_000_000.0, 600, 12345, sc))
    stats, msgs = items[-1], items[:-1]
    assert stats["lost_polls"] == 65 and stats["polls"] == 535  # 95-99 were buffered when the link dropped
    times = [m.received_at - 1_000_000.0 for m in msgs]
    assert times == sorted(times)
    assert not any(100 < t < 160 for t in times)
    gaps = sorted(b - a for a, b in zip(times, times[1:]))
    assert gaps[len(gaps) // 2] < 0.01 and max(gaps) > 60  # bursts every 5 s; one outage


def test_scenarios_cover_outages_and_bad_reads():
    sc = plan_scenarios(7 * DAY, 1)
    kinds = {why for _, _, why in sc.outages}
    assert {"network drop", "gateway reboot", "network failure"} <= kinds
    assert any(b - a > 60 for a, b, _ in sc.outages if why_is(sc, a) == "network drop")
    assert 20 <= len(sc.glitches) <= 36
    assert set(sc.glitches.values()) <= {"negative_current", "garbage_speed", "unknown_status", "nan"}


def why_is(sc, start):
    return next(w for a, _, w in sc.outages if a == start)


@pytest.fixture(scope="module")
def week():
    plc = PM01()
    regs = [plc.step(s) for s in range(7 * DAY)]
    return plc, regs


def test_events_are_irregular(week):
    plc, _ = week
    for ev in (plc.ev_speed, plc.ev_steam, plc.ev_current, plc.ev_vibration):
        starts = [t for _, t in ev.events]
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        assert min(gaps) < 3 * 3600 and max(gaps) > 7 * 3600  # 1-9 h apart, not always 4-6 h
    per_day = [sum(1 for _, t in plc.ev_steam.events if d * DAY <= t < (d + 1) * DAY) for d in range(7)]
    assert max(per_day) - min(per_day) >= 2


def test_some_stops_are_caused_by_the_process(week):
    plc, _ = week
    assert len(plc.causes) == len(plc.stops)
    trips = [c for c in plc.causes if c != "random"]
    assert 4 <= len(trips) <= 20  # about 1-2 a day
    assert {"wet sheet break", "drive overload trip"} <= set(plc.causes)
    assert 6 * 7 <= len(plc.stops) <= 11 * 7  # still about 8 stops a day in total


def test_moisture_varies_and_over_dries_after_steam_recovers(week):
    _, regs = week
    running = [r.moisture for r in regs if r.status == 1 and r.pressure > 4.1]
    assert min(running) < 6.0
    first_hour = [r.moisture for r in regs[:3600]]
    assert max(first_hour) - min(first_hour) > 0.15


def test_bearing_wear_accelerates_and_varies():
    offsets = [0.0, 0.03, -0.02, 0.01, 0.0, -0.03, 0.02, 0.0, 0.0]
    base = [vibration_baseline(d * DAY, offsets) for d in range(8)]
    assert base[0] == VIB_NEW
    rises = [b - a for a, b in zip(base, base[1:])]
    assert any(r < 0 for r in rises)  # some days the baseline even drops a little
    smooth = [vibration_baseline(d * DAY, [0.0] * 9) for d in range(8)]
    assert smooth[7] - smooth[6] > 2 * (smooth[1] - smooth[0])  # wear speeds up


def test_vibration_adds_friction_current(week):
    _, regs = week
    full = [r for r in regs if r.status == 1 and abs(r.speed - 276.5) < 2]
    calm = [r.current for r in full if r.vibration < 3.4 and r.current < 145]
    shaky = [r.current for r in full if r.vibration > 4.6 and r.current < 145]
    gap = sum(shaky) / len(shaky) - sum(calm) / len(calm)
    assert 1.5 < gap < 5  # +1.5 A per mm/s: noticeable, but stays below the 155 A warning


def _held(msg: Message) -> Held:
    when = dt.datetime.fromtimestamp(msg.received_at, dt.timezone.utc)
    return Held("t", TOPIC, msg.payload, when, block_key(msg.payload))


def test_burst_spacing_gives_each_poll_its_own_second():
    msgs = [m for m in generate(1_000_000.0, 60, 12345, Scenario()) if isinstance(m, Message)]
    spacer = BurstSpacer()
    stamped = []
    for m in msgs:
        h = _held(m)
        for burst in spacer.due(h.received_at, {}):
            stamped += burst
        spacer.add(h)
    for burst in spacer.due(_held(msgs[-1]).received_at, {}, force=True):
        stamped += burst
    assert len(stamped) == len(msgs)
    d1 = [ts.timestamp() for h, ts in stamped if h.block == "400002"]
    d2 = [ts.timestamp() for h, ts in stamped if h.block == "400001"]
    assert d1 == d2  # status and values of one poll share its time
    steps = [round(b - a, 2) for a, b in zip(d1, d1[1:])]
    assert all(0.85 <= x <= 1.15 for x in steps) and steps.count(1.0) > 0.7 * len(steps)  # 1 s apart, ± send jitter
    assert all(ts <= h.received_at for h, ts in stamped)  # never later than arrival
    arrivals = [h.received_at.timestamp() for h, _ in stamped if h.block == "400002"]
    assert max(b - a for a, b in zip(arrivals, arrivals[1:])) > 4  # they did arrive in bursts


def test_other_formats_are_not_held():
    assert block_key(b'{"schema_version":1,"ts":"2026-01-01T00:00:00Z","values":{}}') is None
    assert block_key(b"not json") is None
    assert block_key(d2_payload(1)) == "400001"
