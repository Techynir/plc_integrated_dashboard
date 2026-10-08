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


DAY0 = int(dt.datetime(2026, 9, 7, tzinfo=IST).timestamp())  # a Monday, 00:00 IST


@pytest.fixture(scope="module")
def two_weeks():
    """Two weeks from a Monday: registers per second and the machine's stop log."""
    plant = Plant()
    plant.advance_to(DAY0)
    regs = [plant.poll()[1] for _ in range(14 * DAY)]
    stops = [(t, k) for t, k in plant.plc.stops if t >= DAY0 + 900]  # inside the window
    return stops, regs


def test_status_is_one_only_while_paper_is_made(two_weeks):
    stops, regs = two_weeks
    assert {r.status for r in regs} == {0, 1}
    assert all(r.speed > 0 for r in regs if r.status == 1)
    stopped = [r for r in regs if r.status == 0]
    assert max(r.current for r in stopped if r.speed == 0) < 1  # drive off at standstill
    standstill = [r.vibration for r in stopped if r.speed == 0]
    assert 0.1 <= min(standstill) and max(standstill) < 0.45 and len(set(standstill)) > 5  # sensor noise floor


def test_availability_and_lost_time_like_the_mill(two_weeks):
    """Paper on the reel 62-69 % of the time on average; tears are the biggest loss."""
    stops, regs = two_weeks
    days = [sum(r.status for r in regs[d * DAY:(d + 1) * DAY]) / DAY for d in range(14)]
    assert 0.58 <= sum(days) / 14 <= 0.70
    assert max(days) - min(days) > 0.08  # no two days alike
    kinds = [k for _t, k in stops]
    per_day = len(kinds) / 14
    assert 13 <= per_day <= 28
    assert 9 <= kinds.count("tear") / 14 <= 18
    assert {"tear", "mechanical", "electrical", "cleaning", "shutdown"} <= set(kinds)
    assert {"power", "steam", "pulp"} & set(kinds)


def test_weekly_shutdown_on_wednesday(two_weeks):
    stops, _ = two_weeks
    shutdowns = [t for t, k in stops if k == "shutdown"]
    assert len(shutdowns) == 2
    assert all(dt.datetime.fromtimestamp(t, IST).weekday() == 2 for t in shutdowns)


def _stop_window(stops, regs, kind):
    t0 = next(t for t, k in stops if k == kind)
    i = int(t0 - DAY0)
    return regs[i - 5:i + 120]


def test_a_tear_keeps_the_machine_turning_without_the_web(two_weeks):
    stops, regs = two_weeks
    w = _stop_window(stops, regs, "tear")
    before, after = w[0], w[-1]
    assert before.status == 1 and after.status == 0
    assert 95 <= after.speed <= 105  # crawl speed for threading
    assert after.current < 11 + 0.30 * 105 + 2  # web load gone


def test_a_breakdown_stops_the_machine_at_once_with_the_drive_off(two_weeks):
    stops, regs = two_weeks
    w = _stop_window(stops, regs, "mechanical")
    assert w[0].status == 1 and w[-1].speed == 0.0 and w[-1].current == 0.0
    assert next(k for k, r in enumerate(w) if r.speed == 0.0) < 25  # coasts down in seconds, not minutes


def test_a_bearing_breakdown_shows_rising_vibration_first(two_weeks):
    stops, regs = two_weeks
    t0 = next(t for t, k in stops if k == "mechanical")
    i = int(t0 - DAY0)
    before = [r.vibration for r in regs[i - 900:i - 600]]
    just_before = [r.vibration for r in regs[i - 30:i - 5]]
    assert max(just_before) - min(before) > 1.0


def test_restart_threads_at_crawl_before_the_paper_is_on_the_reel(two_weeks):
    stops, regs = two_weeks
    i = next(k for k in range(1, len(regs)) if regs[k - 1].status == 0 and regs[k].status == 1)
    crawl = [r for r in regs[i - 600:i] if 95 <= r.speed <= 105]
    assert len(crawl) >= 50  # about 1-3 minutes of threading
    assert 225 < regs[i].speed < 285  # on the reel at reduced speed ...
    later = [r.speed for r in regs[i + 2400:i + 2700] if r.status == 1]
    if later:  # ... and back up over the next 10-40 min (unless the next stop came first)
        assert sum(later) / len(later) > regs[i].speed


def test_performance_goes_above_and_below_rated(two_weeks):
    """Speed while running against the rated 276.5 m/min, per 12-hour shift: crews push and hold back."""
    _stops, regs = two_weeks
    perf = []
    for k in range(28):
        run = [r.speed for r in regs[k * 43200:(k + 1) * 43200] if r.status == 1]
        perf.append(sum(run) / len(run) / 276.5)
    assert 0.92 <= sum(perf) / len(perf) <= 0.99
    assert min(perf) < 0.95 and max(perf) > 1.0


def test_grade_changes_keep_the_machine_running(two_weeks):
    _stops, regs = two_weeks
    speeds = {round(r.speed) for r in regs if r.status == 1 and r.speed > 262}
    assert {270, 273, 276, 277, 280} & speeds and max(speeds) - min(speeds) >= 9  # several grades


def test_signals_are_steady_at_steady_speed():
    """A steady hour with no plan: controlled and averaged values hold still."""
    plc = PM01()
    plc.planning = False
    regs = [plc.step(DAY0 + s) for s in range(3600)][600:]
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


def test_day_plans_are_random_but_repeatable():
    from ingestor.pm01 import plan_day
    days = [plan_day(d) for d in range(20700, 20714)]
    assert len({k for k, _ in days}) >= 3  # several kinds of day
    assert plan_day(20705) == plan_day(20705)
    counts = [sum(1 for e in ev if e.kind == "tear") for _, ev in days]
    assert min(counts) < max(counts)


def test_vibration_adds_friction_current(two_weeks):
    _, regs = two_weeks
    full = [r for r in regs if r.status == 1 and abs(r.speed - 276.5) < 2]
    calm = [r.current for r in full if r.vibration < 3.4 and r.current < 145]
    shaky = [r.current for r in full if r.vibration > 4.6 and r.current < 145]
    assert 1.0 < sum(shaky) / len(shaky) - sum(calm) / len(calm) < 6


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
    regs = PM01().step(DAY0)
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
    regs = PM01().step(DAY0)
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


def test_problem_events_reach_the_warning_and_critical_limits(two_weeks):
    """Warning and critical limits are far apart; the data still reaches both, for every signal."""
    _stops, regs = two_weeks
    run = [r for r in regs if r.status == 1]
    speed = [r.speed for r in run]
    assert min(speed) < 230 and sum(v < 190 for v in speed) > 30
    current = [r.current for r in run]
    assert max(current) > 160 and sum(v > 185 for v in current) > 30
    steam = [r.pressure for r in run]
    assert min(steam) < 3.6 and sum(v < 2.9 for v in steam) > 30
    moisture = [r.moisture for r in run]
    assert max(moisture) > 7.0 and sum(v > 8.0 for v in moisture) > 30
    vibration = [r.vibration for r in run]
    assert max(vibration) > 4.5 and sum(v > 7.1 for v in vibration) > 30


def test_live_operator_logs_most_stops_once():
    from types import SimpleNamespace

    from ingestor.live_sim import LOG_DELAY_S, OperatorLog

    op = OperatorLog("pm")
    plant = SimpleNamespace(plc=SimpleNamespace(last_stop=None))
    for k in range(400):
        plant.plc.last_stop = (1_800_000_000 + k * 3000, "tear")
        op.note(plant)
        op.note(plant)  # the same stop seen again: logged once
    assert 0.78 * 400 <= len(op.pending) <= 0.92 * 400  # about 85 % get a reason
    assert all(LOG_DELAY_S[0] <= at - t0 <= LOG_DELAY_S[1] and kind == "tear" for at, t0, kind in op.pending)
    again = OperatorLog("pm")
    plant.plc.last_stop = (1_800_000_000, "tear")
    again.note(plant)
    assert again.pending[:1] == [p for p in op.pending if p[1] == 1_800_000_000]  # repeatable
