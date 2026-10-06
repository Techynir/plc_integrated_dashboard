"""The PM-01 model (pm01.py): guide behaviour, steady realistic signals, bearing life, the weekly
maintenance hour, gateway messages the live decoder handles like the real gateway's, and one
continuous run shared by the stored history and the live simulator."""

import datetime as dt
import json
import pickle

import pytest

from ingestor.bursts import BurstSpacer, Held, block_key
from ingestor.history import Message, generate
from ingestor.parsing import InvalidMessage, RegisterDef, TagConfig, decode_telemetry, parse_topic, reject_bad_reads
from ingestor.pm01 import (
    ANCHOR,
    BEARING_INSTALLED,
    BEARING_LIFE_S,
    GREASE_INTERVAL_S,
    GREASED,
    MAINTENANCE,
    PM01,
    Plant,
    bearing_baseline,
    d1_payload,
    d2_payload,
    f32,
    gateway_payloads,
    glitches_of_day,
    in_maintenance,
)

DAY = 86400
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
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


@pytest.fixture(scope="module")
def week():
    plc = PM01()
    regs = [plc.step(s) for s in range(7 * DAY)]
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
    assert 0.1 <= min(standstill) and max(standstill) < 0.45  # sensor noise floor
    assert len(set(standstill)) > 5  # never one fixed value


def test_stops_per_day_and_speed_never_jumps(two_days):
    plc, regs = two_days
    assert 10 <= len(plc.stops) <= 24
    jumps = [abs(b.speed - a.speed) for a, b in zip(regs, regs[1:]) if a.speed and b.speed]
    assert max(jumps) < 3.5  # 3 m/min per second ramp


def test_every_driver_has_problem_events(two_days):
    plc, regs = two_days
    for ev in (plc.ev_speed, plc.ev_steam, plc.ev_current, plc.ev_vibration):
        assert len(ev.events) >= 6
    running = [r for r in regs if r.status == 1]
    assert min(r.speed for r in running) < 260
    assert max(r.current for r in running) > 155
    assert min(r.pressure for r in regs) < 3.5
    assert max(r.moisture for r in regs) > 7.0
    assert max(r.vibration for r in running) > 4.5


def test_signals_are_steady_at_steady_speed():
    """First hour: running at full speed with no event. Controlled and averaged values hold still."""
    plc = PM01()
    regs = [plc.step(s) for s in range(3600)][600:]
    assert max(r.speed for r in regs) - min(r.speed for r in regs) <= 0.4
    assert max(r.pressure for r in regs) - min(r.pressure for r in regs) <= 0.05
    vib = [r.vibration for r in regs]
    assert max(vib) - min(vib) <= 0.06 and len(set(vib)) <= 8
    changes = sum(1 for a, b in zip(vib, vib[1:]) if a != b)
    assert changes < 0.5 * len(vib)  # the same reading repeats more often than it changes
    for r in regs:  # every value at its sensor's resolution
        assert abs(r.vibration * 100 - round(r.vibration * 100)) < 1e-6
        assert abs(r.pressure * 100 - round(r.pressure * 100)) < 1e-6
        assert abs(r.speed * 10 - round(r.speed * 10)) < 1e-6
    assert all(274 <= r.speed <= 279 and 134 <= r.current <= 142 and 4.10 <= r.pressure <= 4.25 for r in regs)


def test_some_stops_are_caused_by_the_process(week):
    plc, _ = week
    trips = [c for c in plc.causes if c != "random"]
    assert 4 <= len(trips) <= 20
    assert 6 * 7 <= len(plc.stops) <= 11 * 7


def test_vibration_adds_friction_current(week):
    _, regs = week
    full = [r for r in regs if r.status == 1 and abs(r.speed - 276.5) < 2]
    calm = [r.current for r in full if r.vibration < 3.4 and r.current < 145]
    shaky = [r.current for r in full if r.vibration > 4.6 and r.current < 145]
    assert 1.5 < sum(shaky) / len(shaky) - sum(calm) / len(calm) < 5


def test_bearing_wears_over_its_life_and_greasing_helps():
    new = BEARING_INSTALLED + 1
    old = BEARING_INSTALLED + BEARING_LIFE_S - DAY
    assert bearing_baseline(old) - bearing_baseline(new) > 0.8  # a worn bearing shakes clearly more
    assert bearing_baseline(old) < 4.3  # replaced before the 4.5 warning
    assert bearing_baseline(old + 2 * DAY) < bearing_baseline(new) + 0.15  # a new bearing again
    before = GREASED + 3 * GREASE_INTERVAL_S - 60
    assert bearing_baseline(before) - bearing_baseline(before + 120) > 0.07  # greasing drops it
    days = [bearing_baseline(ANCHOR + d * DAY) for d in range(30)]
    assert max(abs(b - a) for a, b in zip(days, days[1:])) < 0.12  # changes slowly, day by day


def test_maintenance_is_sunday_evening_india_time():
    sunday = dt.datetime(2026, 10, 4, tzinfo=IST)  # a Sunday
    at = lambda h, m: (sunday + dt.timedelta(hours=h, minutes=m)).timestamp()  # noqa: E731
    assert in_maintenance(at(18, 0)) and in_maintenance(at(18, 59))
    assert not in_maintenance(at(17, 59)) and not in_maintenance(at(19, 0))
    assert not in_maintenance(at(18, 30) + DAY)  # Monday
    assert MAINTENANCE["weekday"] == "Sunday"


def test_plant_sends_every_second_except_during_maintenance():
    start = int(dt.datetime(2026, 10, 4, 17, 0, tzinfo=IST).timestamp())
    plant = Plant()
    plant.advance_to(start)
    sent = [(t, msgs) for t, _r, msgs in (plant.poll() for _ in range(3 * 3600))]
    silent = [t for t, msgs in sent if not msgs]
    assert len(silent) == 3600 and in_maintenance(silent[0]) and in_maintenance(silent[-1])
    assert all(len(msgs) == 2 for _, msgs in sent if msgs)  # values block, then status block


def test_history_and_live_simulator_are_one_run(tmp_path):
    t = ANCHOR + 3 * DAY + 12345
    history = Plant()
    msgs = [m for m in generate(history, t - 600, t) if isinstance(m, Message)]
    assert len(msgs) == 2 * 600 and msgs[0].received_at >= t - 600
    history.save(tmp_path / "pm01.pkl")
    live = Plant.load(tmp_path / "pm01.pkl")
    direct = Plant()
    direct.advance_to(t)
    for _ in range(300):
        assert live.poll() == direct.poll()  # the live simulator continues exactly where history stopped


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
    msg = decode_telemetry(TOPIC, d1, dt.datetime.now(dt.timezone.utc), None, REGISTER_MAP)
    assert msg.readings[0].value_num == pytest.approx(f32(275.8))


def test_bad_reads_are_rejected_like_live():
    regs = PM01().step(0)
    now = dt.datetime.now(dt.timezone.utc)
    for kind in ("negative_current", "garbage_speed", "unknown_status"):
        reasons = []
        for payload in gateway_payloads(regs, kind):
            msg = decode_telemetry(TOPIC, payload, now, None, REGISTER_MAP)
            reasons += reject_bad_reads(msg.readings, TAG_CFG.get)[1]
        assert len(reasons) == 1, kind
    d1, _ = gateway_payloads(regs, "nan")
    with pytest.raises(InvalidMessage):
        decode_telemetry(TOPIC, d1, now, None, REGISTER_MAP)


def test_bad_reads_three_to_five_a_day():
    for day in range(20000, 20010):
        g = glitches_of_day(day)
        assert 3 <= len(g) <= 5 and all(day * DAY <= t < (day + 1) * DAY for t in g)
    assert glitches_of_day(20001) == glitches_of_day(20001)


def _held(at: float, payload: bytes) -> Held:
    return Held("t", TOPIC, payload, dt.datetime.fromtimestamp(at, dt.timezone.utc), block_key(payload))


def test_burst_spacing_gives_each_poll_its_own_second():
    """A gateway that buffers 5 polls and sends them together every 5 s: each poll gets its second."""
    regs = PM01().step(0)
    arrivals = []
    for burst in range(12):
        at = 1_000_000.0 + 5 * burst + 0.07
        for _ in range(5):
            for payload in gateway_payloads(regs, None):
                arrivals.append((at, payload))
                at += 0.001
    spacer, stamped = BurstSpacer(), []
    for at, payload in arrivals:
        h = _held(at, payload)
        for b in spacer.due(h.received_at, {}):
            stamped += b
        spacer.add(h)
    for b in spacer.due(_held(*arrivals[-1]).received_at, {}, force=True):
        stamped += b
    d1 = [ts.timestamp() for h, ts in stamped if h.block == "400002"]
    d2 = [ts.timestamp() for h, ts in stamped if h.block == "400001"]
    assert d1 == d2 and len(d1) == 60
    steps = [round(b - a, 2) for a, b in zip(d1, d1[1:])]
    assert all(0.9 <= x <= 1.1 for x in steps)
    assert all(ts <= h.received_at for h, ts in stamped)


def test_other_formats_are_not_held():
    assert block_key(b'{"schema_version":1,"ts":"2026-01-01T00:00:00Z","values":{}}') is None
    assert block_key(b"not json") is None
    assert block_key(d2_payload(1)) == "400001"


def test_pickled_plant_is_small():
    assert len(pickle.dumps(Plant())) < 100_000
    json.dumps(MAINTENANCE)
