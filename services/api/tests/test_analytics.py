import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from app import analytics as a

IST = ZoneInfo("Asia/Kolkata")


def test_state_segments_cover_window_and_mark_gaps_as_comms():
    # running 0..10 s (1 s samples), gap 10..40, stopped 40..50
    samples = [(t, True) for t in range(0, 10)] + [(t, False) for t in range(40, 50)]
    segs = a.state_segments(samples, start=0, end=60, gap_s=5)
    assert [(s.state, s.start, s.end) for s in segs] == [
        ("run", 0, 14), ("comms", 14, 40), ("stop", 40, 54), ("comms", 54, 60)]
    tot = a.totals(segs)
    assert tot == {"run": 14, "stop": 14, "comms": 32}
    assert a.availability(tot) == 0.5
    assert a.stop_events(segs, 60) == [{"start": 40, "end": 54, "seconds": 14}]


def test_window_start_before_first_sample_is_comms():
    segs = a.state_segments([(100, True), (101, True)], start=0, end=102, gap_s=5)
    assert [(s.state, s.start, s.end) for s in segs] == [("comms", 0, 100), ("run", 100, 102)]


def test_running_codes_from_labels():
    assert a.running_codes({"0": "Stopped", "1": "Running"}) == {1.0}
    assert a.running_codes(None) is None
    assert a.is_running(1.0, {1.0}) and not a.is_running(0.0, {1.0}) and a.is_running(5, None)


def test_in_states_and_minute_means():
    segs = [a.Segment("run", 0, 60), a.Segment("stop", 60, 120), a.Segment("run", 120, 180)]
    series = [(t, float(t)) for t in range(0, 180, 30)]
    assert a.in_states(series, segs, {"run"}) == [(0, 0.0), (30, 30.0), (120, 120.0), (150, 150.0)]
    assert a.minute_means([(0, 1.0), (30, 3.0), (61, 5.0)]) == [(0.0, 2.0), (60.0, 5.0)]


def test_two_twelve_hour_shifts_in_plant_time():
    t = dt.datetime(2026, 9, 28, 17, 59, tzinfo=IST).timestamp()
    assert a.shift_of(t, IST) == "A" and a.shift_of(t + 60, IST) == "B"
    assert a.shift_of(dt.datetime(2026, 9, 28, 23, 0, tzinfo=IST).timestamp(), IST) == "B"
    assert a.shift_of(dt.datetime(2026, 9, 28, 3, 0, tzinfo=IST).timestamp(), IST) == "B"
    assert a.shift_of(dt.datetime(2026, 9, 28, 6, 0, tzinfo=IST).timestamp(), IST) == "A"
    start = dt.datetime(2026, 9, 28, 5, 0, tzinfo=IST).timestamp()
    cuts = a.shift_boundaries(start, start + 24 * 3600, IST)
    assert [dt.datetime.fromtimestamp(c, IST).hour for c in cuts] == [6, 18]
    pieces = a.split_segments([a.Segment("run", start, start + 3 * 3600)], cuts)
    assert [(p.end - p.start) / 3600 for p in pieces] == [1.0, 2.0]


def test_spc_limits_flags_and_capability():
    vals = [6.1, 6.2, 6.1, 6.15, 6.2, 6.1]
    lim = a.imr_limits(vals)
    assert lim["cl"] == pytest.approx(6.125) and lim["ucl"] > 6.2 > lim["lcl"]
    flags = a.spc_flags([6.2] * 8 + [9.9], 6.0, 6.5, 5.5)
    assert flags[:7] == [0] * 7 and flags[7] == 1 and flags[8] == 2
    cap = a.capability([6.0, 6.1, 6.2, 6.1, 6.15, 6.05], 5.9, 6.4)
    assert cap["cp"] > 1 and 0 < cap["cpk"] <= cap["cp"] and cap["in_spec"] == 1.0
    assert a.capability([6.0], 5.9, 6.4)["cp"] is None


def test_histogram_and_regression():
    h = a.histogram([1, 1.5, 2, 3.9, 4], 1, 4, bins=3)
    assert [b["count"] for b in h] == [2, 1, 2]
    r = a.linreg([1, 2, 3, 4], [2, 4, 6, 8.1])
    assert r["m"] == pytest.approx(2.03, abs=0.01) and r["r"] > 0.99
    assert a.linreg([1, 1, 1], [1, 2, 3]) is None


def test_limit_scores_and_overall():
    vib = {"limit_dir": "high", "warn_limit": 4.5, "crit_limit": 5.5, "max_value": 3.3}
    assert a.limit_score(3.0, vib) == 100 and a.limit_score(4.5, vib) == 60 and a.limit_score(5.5, vib) == 20
    steam = {"limit_dir": "low", "warn_limit": 3.5, "crit_limit": 3.1, "min_value": 4.1}
    assert a.limit_score(4.2, steam) == 100 and a.limit_score(3.5, steam) == pytest.approx(60)
    assert a.limit_score(3.1, steam) == pytest.approx(20) and a.limit_score(2.0, steam) == 0
    assert a.limit_score(1.0, {"limit_dir": None}) is None
    assert a.residual_score(0.5, 1.0) == 100 and a.residual_score(3, 1.0) == 60
    # weakest part caps the overall score
    assert a.overall_score({"bearing": (20, 0.4), "drive": (100, 0.3), "dryer": (100, 0.3)}) == 40
    assert a.overall_score({"x": (None, 1)}) is None
    assert a.health_state(85) == "good" and a.health_state(60) == "watch" and a.health_state(10) == "act"


def test_outage_in_the_maintenance_window_is_planned():
    w = {"weekday": "Sunday", "start": "18:00", "end": "19:00", "tz": "Asia/Kolkata"}
    sunday_18 = dt.datetime(2026, 10, 4, 18, 0, tzinfo=IST).timestamp()
    assert a.in_maintenance(sunday_18 + 60, w) and not a.in_maintenance(sunday_18 - 60, w)
    assert a.planned_outage(sunday_18 - 5, sunday_18 + 3605, w)  # the whole hour, a few seconds either side
    assert not a.planned_outage(sunday_18 - 7200, sunday_18 + 60, w)  # mostly outside: a real outage
    assert not a.planned_outage(sunday_18 + 86400, sunday_18 + 86400 + 3600, w)  # Monday
    assert not a.planned_outage(sunday_18, sunday_18 + 3600, None)
