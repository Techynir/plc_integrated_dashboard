"""The PM-01 history generator follows the PLC guide (Step 6 checklist) and produces messages the
live decoder handles exactly like the real gateway's."""

import datetime as dt
import json

import pytest

from ingestor.history import (
    PM01,
    Message,
    Scenario,
    d1_payload,
    d2_payload,
    f32,
    generate,
    plan_scenarios,
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
    assert min(r.vibration for r in regs) >= 0.2


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
