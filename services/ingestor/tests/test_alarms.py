import pytest

from ingestor.alarms import AlarmEngine, Rule, decide, format_message
from ingestor.parsing import Reading


def rule(rule_type, threshold=None, deadband=0.0, tag="t", device_id=None, message="", rid=1):
    return Rule(rid, "r", device_id, tag, rule_type, threshold, deadband, "warning", message, None)


def reading(value, quality=0, tag="t"):
    return Reading(tag, "number", value, None, quality)


def test_high_with_deadband_hysteresis():
    r = rule("high", 80, deadband=5)
    assert decide(r, 80, active=False) is None
    assert decide(r, 80.1, active=False) == "raise"
    assert decide(r, 78, active=True) is None       # inside deadband
    assert decide(r, 75, active=True) == "clear"


def test_low():
    r = rule("low", 10, deadband=1)
    assert decide(r, 9.9, active=False) == "raise"
    assert decide(r, 10.5, active=True) is None
    assert decide(r, 11, active=True) == "clear"


def test_equals_fault_offline():
    assert decide(rule("equals", 1), 1.0, active=False) == "raise"
    assert decide(rule("equals", 1), 0.0, active=True) == "clear"
    assert decide(rule("fault", tag=None), "FAULT", active=False) == "raise"
    assert decide(rule("fault", tag=None), "RUN", active=True) == "clear"
    assert decide(rule("offline", 60, tag=None), 61, active=False) == "raise"
    assert decide(rule("offline", 60, tag=None), 30, active=True) is None
    assert decide(rule("offline", 60, tag=None), 0, active=True) == "clear"


def test_engine_tracks_state_and_scope():
    e = AlarmEngine()
    e.load([rule("high", 50, device_id="d1"), rule("fault", tag=None, rid=2)], {})

    assert e.on_readings("d2", [reading(99)], None) == []  # rule 1 scoped to d1
    [tr] = e.on_readings("d1", [reading(99)], None)
    assert (tr.action, tr.rule.id, tr.value) == ("raise", 1, 99)
    e.mark_raised(1, "d1", alarm_id=10)
    assert e.on_readings("d1", [reading(99)], None) == []  # already active
    [tr] = e.on_readings("d1", [reading(10)], "FAULT")[:1]
    assert tr.action == "clear"
    assert e.mark_cleared(1, "d1") == 10


def test_engine_skips_bad_quality_and_missing_tags():
    e = AlarmEngine()
    e.load([rule("high", 50)], {})
    assert e.on_readings("d1", [reading(99, quality=2)], None) == []
    assert e.on_readings("d1", [reading(99, tag="other")], None) == []


def test_offline_rules():
    e = AlarmEngine()
    e.load([rule("offline", 30, tag=None), rule("high", 1)], {})
    assert e.on_offline_time("d1", 10) == []
    [tr] = e.on_offline_time("d1", 31)
    assert tr.action == "raise"
    e.mark_raised(tr.rule.id, "d1", 5)
    [tr] = e.on_offline_time("d1", 0)
    assert tr.action == "clear"


@pytest.mark.parametrize(
    "r, value, expected",
    [
        (rule("high", 80), 91.25, "d1 t high: 91.25 > 80"),
        (rule("offline", 60, tag=None), 61, "d1 offline for more than 60s"),
        (rule("high", 80, message="{device}: {tag}={value} (limit {threshold})"), 81, "d1: t=81 (limit 80)"),
        (rule("fault", tag=None, message="Fault on {device} {unknown}"), "FAULT", "Fault on d1 {unknown}"),
    ],
)
def test_format_message(r, value, expected):
    assert format_message(r, "d1", value) == expected


def test_fault_transition_has_no_numeric_value():
    e = AlarmEngine()
    e.load([rule("fault", tag=None)], {})
    [tr] = e.on_readings("d1", [reading(1)], "FAULT")
    assert tr.action == "raise" and tr.value is None
    assert format_message(tr.rule, "d1", tr.value) == "d1 reported FAULT"


def delayed(rule_type="high", threshold=50.0, suppress=False, **kw):
    return Rule(7, "r", None, "t", rule_type, threshold, 0.0, "warning", "", None,
                on_delay_s=3, off_delay_s=3, suppress_when_stopped=suppress, **kw)


def test_on_and_off_delay_prevent_chattering():
    e = AlarmEngine()
    e.load([delayed()], {})
    assert e.on_readings("d", [reading(60)], None, now=0) == []
    assert e.on_readings("d", [reading(60)], None, now=2) == []          # not yet 3 s
    assert e.on_readings("d", [reading(40)], None, now=2.5) == []        # dipped: timer resets
    assert e.on_readings("d", [reading(60)], None, now=3) == []
    [tr] = e.on_readings("d", [reading(61)], None, now=6)               # held 3 s -> raise
    assert tr.action == "raise"
    e.mark_raised(7, "d", 1)
    assert e.on_readings("d", [reading(40)], None, now=7) == []          # clear needs 3 s too
    [tr] = e.on_readings("d", [reading(40)], None, now=10)
    assert tr.action == "clear"


def test_suppressed_while_stopped():
    e = AlarmEngine()
    e.load([delayed(suppress=True)], {})
    for t in range(0, 10):
        assert e.on_readings("d", [reading(99)], "STOP", now=t, inhibited=True) == []
    e.mark_raised(7, "d", 1)  # an alarm that was already active clears (after the off delay)
    e.on_readings("d", [reading(99)], "STOP", now=20, inhibited=True)
    [tr] = e.on_readings("d", [reading(99)], "STOP", now=23, inhibited=True)
    assert tr.action == "clear"


def test_stopped_rule_type():
    e = AlarmEngine()
    e.load([rule("stopped", tag=None)], {})
    [tr] = e.on_readings("d", [], "STOP")
    assert tr.action == "raise" and format_message(tr.rule, "d", None) == "d machine stopped"
