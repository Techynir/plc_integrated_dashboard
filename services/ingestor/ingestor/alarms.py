"""Alarm rule evaluation. Pure state machine; persistence lives in store.py."""

from dataclasses import dataclass

from .parsing import QUALITY_BAD, Reading


@dataclass(frozen=True)
class Rule:
    id: int
    name: str
    device_id: str | None  # None = all devices
    tag: str | None
    rule_type: str  # high | low | equals | fault | offline
    threshold: float | None
    deadband: float
    severity: str
    message: str
    webhook_url: str | None

    def applies_to(self, device_id: str) -> bool:
        return self.device_id is None or self.device_id == device_id


@dataclass(frozen=True)
class Transition:
    action: str  # raise | clear
    rule: Rule
    device_id: str
    value: float | None


def decide(rule: Rule, value, active: bool) -> str | None:
    """Return 'raise', 'clear' or None for one observation. Deadband gives hysteresis on clear."""
    th, db = rule.threshold, rule.deadband
    if rule.rule_type == "high":
        if not active and value > th:
            return "raise"
        if active and value <= th - db:
            return "clear"
    elif rule.rule_type == "low":
        if not active and value < th:
            return "raise"
        if active and value >= th + db:
            return "clear"
    elif rule.rule_type in ("equals", "fault"):
        matched = value == "FAULT" if rule.rule_type == "fault" else abs(value - th) < 1e-9
        if matched and not active:
            return "raise"
        if not matched and active:
            return "clear"
    elif rule.rule_type == "offline":
        # value = seconds since the device went silent; 0 means it is online again.
        if not active and value > th:
            return "raise"
        if active and value == 0:
            return "clear"
    return None


def _fmt(value) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def format_message(rule: Rule, device_id: str, value) -> str:
    if rule.message:
        text = rule.message
        for key, val in {
            "{device}": device_id,
            "{tag}": rule.tag or "",
            "{value}": _fmt(value) if value is not None else "",
            "{threshold}": _fmt(rule.threshold) if rule.threshold is not None else "",
        }.items():
            text = text.replace(key, val)
        return text
    th = _fmt(rule.threshold) if rule.threshold is not None else ""
    return {
        "high": f"{device_id} {rule.tag} high: {_fmt(value)} > {th}",
        "low": f"{device_id} {rule.tag} low: {_fmt(value)} < {th}",
        "equals": f"{device_id} {rule.tag} = {_fmt(value)}",
        "fault": f"{device_id} reported FAULT",
        "offline": f"{device_id} offline for more than {th}s",
    }[rule.rule_type]


class AlarmEngine:
    def __init__(self) -> None:
        self.rules: list[Rule] = []
        self.active: dict[tuple[int, str], int] = {}  # (rule_id, device_id) -> alarm id

    def load(self, rules: list[Rule], active: dict[tuple[int, str], int]) -> None:
        self.rules = rules
        self.active = dict(active)

    def is_active(self, rule_id: int, device_id: str) -> bool:
        return (rule_id, device_id) in self.active

    def on_readings(self, device_id: str, readings: list[Reading], status: str | None) -> list[Transition]:
        by_tag = {r.tag: r for r in readings}
        out = []
        for rule in self.rules:
            if not rule.applies_to(device_id):
                continue
            if rule.rule_type == "fault":
                if status is None:
                    continue
                value = status
            elif rule.rule_type in ("high", "low", "equals"):
                reading = by_tag.get(rule.tag)
                if reading is None or reading.value_num is None or reading.quality == QUALITY_BAD:
                    continue
                value = reading.value_num
            else:
                continue
            action = decide(rule, value, self.is_active(rule.id, device_id))
            if action:
                # trigger_value is numeric; a FAULT status is described by the message instead.
                numeric = isinstance(value, (int, float)) and action == "raise"
                out.append(Transition(action, rule, device_id, value if numeric else None))
        return out

    def on_offline_time(self, device_id: str, offline_for_s: float) -> list[Transition]:
        out = []
        for rule in self.rules:
            if rule.rule_type != "offline" or not rule.applies_to(device_id):
                continue
            action = decide(rule, offline_for_s, self.is_active(rule.id, device_id))
            if action:
                out.append(Transition(action, rule, device_id, offline_for_s if action == "raise" else None))
        return out

    def mark_raised(self, rule_id: int, device_id: str, alarm_id: int) -> None:
        self.active[(rule_id, device_id)] = alarm_id

    def mark_cleared(self, rule_id: int, device_id: str) -> int | None:
        return self.active.pop((rule_id, device_id), None)
