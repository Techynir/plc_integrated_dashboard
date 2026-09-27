import datetime as dt
import random

import pytest

from app.sim.templating import Renderer, TemplateError, validate

NOW = dt.datetime(2026, 9, 27, 10, 0, 0, tzinfo=dt.timezone.utc)


def render(template, n=1, **kw):
    r = Renderer(template, kw.get("device", "dev1"), "pune", "line1", rng=random.Random(1))
    return [r.render(NOW) for _ in range(n)]


def test_plain_text_passes_through_unchanged():
    assert render("result: [275.500000,138.500000]") == ["result: [275.500000,138.500000]"]


def test_random_values_stay_in_range_and_format():
    for out in render("{{rand:270:280}}|{{rand:1:2:6}}|{{int:0:3}}", n=50):
        a, b, c = out.split("|")
        assert 270 <= float(a) <= 280 and len(a.split(".")[1]) == 2
        assert 1 <= float(b) <= 2 and len(b.split(".")[1]) == 6
        assert c in {"0", "1", "2", "3"}


def test_counter_seq_context_and_time():
    out = render('{"d": "{{device}}", "s": "{{site}}/{{line}}", "n": {{seq}}, "c": {{counter:10:5}}, "t": "{{now}}"}', n=3)
    assert out[2] == '{"d": "dev1", "s": "pune/line1", "n": 3, "c": 20, "t": "2026-09-27T10:00:00.000Z"}'


def test_independent_counters_and_sine_bounds():
    out = render("{{counter}} {{counter:100}}", n=2)
    assert out == ["0 100", "1 101"]
    [value] = render("{{sine:20:30:60}}")
    assert 20 <= float(value) <= 30


def test_bool_and_pick():
    values = set(render("{{bool}} {{pick:RUN|IDLE}}", n=40))
    assert {v.split()[0] for v in values} == {"true", "false"}
    assert {v.split()[1] for v in values} == {"RUN", "IDLE"}


@pytest.mark.parametrize(
    "template, message",
    [
        ("", "empty"),
        ("{{rand:1}}", "needs MIN:MAX"),
        ("{{rand:a:b}}", "not a number"),
        ("{{nope}}", "unknown placeholder"),
        ("{{int:5:1}}", "MIN must not exceed"),
        ("{{sine:0:1:0}}", "period"),
    ],
)
def test_template_errors(template, message):
    with pytest.raises(TemplateError, match=message):
        validate(template)
