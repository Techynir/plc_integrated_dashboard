"""Payload templates for the web simulator.

A template is the message text exactly as a PLC would send it, optionally with
placeholders that are filled in fresh for every message:

    result: [{{rand:270:280}}, {{rand:130:140:1}}, {{int:0:10}}]
    {"schema_version": 1, "device_id": "{{device}}", "ts": "{{now}}", "tags": {"temp": {{sine:20:30:60}}}}

| Placeholder                          | Value                                                   |
|--------------------------------------|---------------------------------------------------------|
| {{rand:MIN:MAX[:DECIMALS]}}          | random number (default 2 decimals)                      |
| {{int:MIN:MAX}}                      | random integer                                          |
| {{sine:MIN:MAX[:PERIOD_S[:DEC]]}}    | smooth wave between MIN and MAX (default period 60 s)   |
| {{counter[:START[:STEP]]}}           | increases on every message                              |
| {{seq}}                              | message number in this run (1, 2, 3, ...)               |
| {{bool[:P_TRUE]}}                    | true / false                                            |
| {{pick:A|B|C}}                       | one of the options                                      |
| {{now}}                              | current UTC time, RFC 3339                              |
| {{device}}, {{site}}, {{line}}       | target of the message                                   |
"""

import datetime as dt
import math
import random
import re

PLACEHOLDER = re.compile(r"\{\{\s*([a-z]+)((?::[^:{}]*)*)\s*\}\}")
MAX_PAYLOAD_BYTES = 65536


class TemplateError(ValueError):
    pass


def _num(value: str, name: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise TemplateError(f"'{value}' is not a number in {{{{{name}}}}}") from exc


def _dec(value: str | None, default: int) -> int:
    if value in (None, ""):
        return default
    d = int(_num(value, "decimals"))
    if not 0 <= d <= 9:
        raise TemplateError("decimals must be between 0 and 9")
    return d


class Renderer:
    """Renders one template repeatedly for one device; keeps counters between messages."""

    def __init__(self, template: str, device: str, site: str, line: str, rng: random.Random | None = None):
        self.template = template
        self.context = {"device": device, "site": site, "line": line}
        self.rng = rng or random.Random()
        self.seq = 0
        self.counters: dict[int, float] = {}

    def render(self, now: dt.datetime | None = None) -> str:
        now = now or dt.datetime.now(dt.timezone.utc)
        self.seq += 1
        out = PLACEHOLDER.sub(lambda m: self._value(m, now), self.template)
        if len(out.encode()) > MAX_PAYLOAD_BYTES:
            raise TemplateError("rendered message is larger than 64 KB")
        return out

    def _value(self, m: re.Match, now: dt.datetime) -> str:
        name = m.group(1)
        args = m.group(2).split(":")[1:] if m.group(2) else []

        def need(n: int, usage: str) -> None:
            if len(args) < n:
                raise TemplateError(f"{{{{{name}}}}} needs {usage}")

        if name in self.context:
            return self.context[name]
        if name == "now":
            return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        if name == "seq":
            return str(self.seq)
        if name == "rand":
            need(2, "MIN:MAX, e.g. {{rand:0:100}}")
            lo, hi = _num(args[0], name), _num(args[1], name)
            return f"{self.rng.uniform(lo, hi):.{_dec(args[2] if len(args) > 2 else None, 2)}f}"
        if name == "int":
            need(2, "MIN:MAX, e.g. {{int:0:10}}")
            lo, hi = int(_num(args[0], name)), int(_num(args[1], name))
            if lo > hi:
                raise TemplateError("{{int}} MIN must not exceed MAX")
            return str(self.rng.randint(lo, hi))
        if name == "sine":
            need(2, "MIN:MAX[:PERIOD_S], e.g. {{sine:20:30:60}}")
            lo, hi = _num(args[0], name), _num(args[1], name)
            period = _num(args[2], name) if len(args) > 2 and args[2] else 60.0
            if period <= 0:
                raise TemplateError("{{sine}} period must be > 0")
            phase = math.sin(2 * math.pi * now.timestamp() / period)
            return f"{lo + (hi - lo) * (phase + 1) / 2:.{_dec(args[3] if len(args) > 3 else None, 2)}f}"
        if name == "counter":
            start = _num(args[0], name) if args and args[0] else 0.0
            step = _num(args[1], name) if len(args) > 1 and args[1] else 1.0
            value = self.counters.get(m.start(), start - step) + step
            self.counters[m.start()] = value
            return f"{value:g}"
        if name == "bool":
            p_true = _num(args[0], name) if args and args[0] else 0.5
            return "true" if self.rng.random() < p_true else "false"
        if name == "pick":
            need(1, "options, e.g. {{pick:RUN|IDLE|FAULT}}")
            return self.rng.choice(":".join(args).split("|"))
        raise TemplateError(f"unknown placeholder {{{{{name}}}}}")


def validate(template: str) -> str:
    """Render once with dummy context; returns the sample or raises TemplateError."""
    if not template.strip():
        raise TemplateError("message is empty")
    return Renderer(template, "device", "site", "line").render()
