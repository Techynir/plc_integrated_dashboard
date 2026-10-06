"""Process rules: alarms that look at several signals together (docs/PROCESS_RULES.md).

Limit alarms check one tag against one number. These rules compare signals with each other:
current against the current expected for the speed, vibration corrected for speed, moisture
predicted from steam pressure, and so on. Each alarm says, in plain words:

    what  - what is happening
    why   - the numbers behind it
    next  - what to do

The rules exist as alarm_rules rows (rule_type "process", managed_by "process:<device>:<code>",
created by the API from the tag roles), so they have on/off delays, show in Admin -> Alarm rules
and raise ordinary alarms. The same code runs live (main.py) and over stored data (backfill.py
in rules_backfill).

Coefficients (expected current for a speed, moisture for a steam pressure) come from
asset_config.rules; rules_backfill --fit derives them from the stored data. Without them the
rules that need them stay quiet.
"""

from collections import deque
from dataclasses import dataclass, field
from statistics import median

from .alarms import Rule, Transition

SETTLE_S = 180.0  # after a restart or a speed change, the process needs about 3 minutes to settle
SPEED_STEP = 5.0  # a speed change bigger than this within a minute counts as a change
WET_AFTER_RESTART_S = 900.0
BASELINE_MINUTES = 360  # vibration baseline: median of the last 6 running hours
BASELINE_MIN_MINUTES = 60
STEAM_MEMORY_S = 600.0  # moisture high within 10 min of low steam is the steam's fault


@dataclass
class TagInfo:
    tag: str
    unit: str = ""
    min_value: float | None = None
    max_value: float | None = None
    warn: float | None = None
    crit: float | None = None


@dataclass
class Inputs:
    """What the rules know about one asset."""

    roles: dict[str, TagInfo]
    speed_ref: float | None = None  # normal running speed (asset_config.speed_target)
    current_model: tuple[float, float, float] | None = None  # current = a + b * speed, rmse
    moisture_model: tuple[float, float] | None = None  # moisture = a + b * steam pressure


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def inputs_from_config(roles: dict[str, TagInfo], cfg: dict) -> Inputs:
    """asset_config: speed_target, and rules.current_model {a, b, rmse} / rules.moisture_model {a, b}."""
    rules = cfg.get("rules") if isinstance(cfg.get("rules"), dict) else {}
    speed_ref = _num(cfg.get("speed_target"))
    sp = roles.get("speed")
    if speed_ref is None and sp and sp.min_value is not None and sp.max_value is not None:
        speed_ref = (sp.min_value + sp.max_value) / 2
    cm, mm = rules.get("current_model") or {}, rules.get("moisture_model") or {}
    current = (_num(cm.get("a")), _num(cm.get("b")), _num(cm.get("rmse")) or 0.0)
    moisture = (_num(mm.get("a")), _num(mm.get("b")))
    return Inputs(
        roles=roles,
        speed_ref=speed_ref,
        current_model=current if None not in current else None,
        moisture_model=moisture if None not in moisture else None,
    )


@dataclass
class Finding:
    value: float | None
    explain: dict  # {"what", "why", "next"}


DECIMALS = {"speed": 0, "motor_current": 0, "steam_pressure": 2, "moisture": 1, "vibration": 1}


def code_of(rule: Rule) -> str | None:
    """'process:<device>:<code>' -> code."""
    m = rule.managed_by or ""
    return m.rsplit(":", 1)[-1] if m.startswith("process:") else None


@dataclass
class DeviceRules:
    """Evaluates the rules for one asset, one poll at a time (values by role)."""

    inputs: Inputs
    running: bool | None = None
    run_since: float = float("-inf")  # unknown start counts as long settled
    last_speed_change: float = float("-inf")
    last_steam_low: float = float("-inf")
    speeds: deque = field(default_factory=deque)  # (t, speed) over the last minute
    minute: tuple[int, list] | None = None  # (minute, speed-corrected vibration samples)
    vib_minutes: deque = field(default_factory=lambda: deque(maxlen=BASELINE_MINUTES))

    def fmt(self, role: str, v: float) -> str:
        info = self.inputs.roles.get(role)
        unit = f" {info.unit}" if info and info.unit else ""
        return f"{v:.{DECIMALS.get(role, 1)}f}{unit}"

    def normal(self, role: str) -> str:
        info = self.inputs.roles.get(role)
        if not info or info.min_value is None or info.max_value is None:
            return ""
        d = DECIMALS.get(role, 1)
        unit = f" {info.unit}" if info.unit else ""
        return f"Normal is {info.min_value:.{d}f} to {info.max_value:.{d}f}{unit}."

    def vib_baseline(self) -> float | None:
        return median(self.vib_minutes) if len(self.vib_minutes) >= BASELINE_MIN_MINUTES else None

    def step(self, t: float, running: bool, v: dict[str, float]) -> dict[str, Finding]:
        """Rules whose condition holds at time t (code -> finding). Codes not returned are false."""
        inp, out = self.inputs, {}
        speed = v.get("speed")
        if running and self.running is False:
            self.run_since = t
        self.running = running
        if speed is not None:
            self.speeds.append((t, speed))
            while self.speeds and self.speeds[0][0] < t - 60:
                self.speeds.popleft()
            recent = [s for _, s in self.speeds]
            if max(recent) - min(recent) > SPEED_STEP:
                self.last_speed_change = t
        settled = running and t - self.run_since >= SETTLE_S and t - self.last_speed_change >= SETTLE_S
        ref = inp.speed_ref

        # R1: status and speed disagree
        sp_info = inp.roles.get("speed")
        if speed is not None and ref and "machine_status" in inp.roles:
            low_normal = sp_info.min_value if sp_info and sp_info.min_value else 0.9 * ref
            if running and t - self.run_since > 60 and speed < 0.35 * ref:
                out["status_speed"] = Finding(speed, {
                    "what": "Machine says Running, but it is not moving.",
                    "why": f"Status is Running. Speed is {self.fmt('speed', speed)}.",
                    "next": "Check the status signal from the PLC.",
                })
            elif not running and speed >= low_normal:
                out["status_speed"] = Finding(speed, {
                    "what": "Machine says Stopped, but it is running.",
                    "why": f"Status is Stopped. Speed is {self.fmt('speed', speed)}.",
                    "next": "Check the status signal from the PLC.",
                })

        # vibration corrected to the normal speed (vibration grows with speed)
        vib, vib_info = v.get("vibration"), inp.roles.get("vibration")
        v_ref = None
        if vib is not None and speed is not None and ref and speed > 0.35 * ref:
            v_ref = vib * ref / speed
            if settled:
                m = int(t // 60)
                if self.minute is None or self.minute[0] != m:
                    if self.minute and self.minute[1]:
                        self.vib_minutes.append(sum(self.minute[1]) / len(self.minute[1]))
                    self.minute = (m, [])
                self.minute[1].append(v_ref)
        base = self.vib_baseline()
        vib_up = v_ref is not None and base is not None and vib_info and vib_info.warn and v_ref - base > 0.25 * vib_info.warn

        # R2: motor current against the current expected for the speed
        cur = v.get("motor_current")
        if inp.current_model and cur is not None and speed is not None and ref and settled and speed > 0.3 * ref:
            a, b, rmse = inp.current_model
            expected = a + b * speed
            diff = cur - expected
            limit = max(10.0, 4 * rmse)
            shaking = vib_up or (vib is not None and vib_info and vib_info.warn and vib > vib_info.warn)
            if diff > limit:
                out["load_high"] = Finding(cur, {
                    "what": "Motor is working too hard." if diff < 2 * limit else "Motor is working much too hard.",
                    "why": f"Current is {self.fmt('motor_current', cur)}. At this speed it should be about "
                           f"{self.fmt('motor_current', expected)}." + (" The bearing is shaking more too." if shaking else " The bearing is fine."),
                    "next": "Check the main bearing and its grease." if shaking
                            else "Look for drag: web tension, felts, rolls, doctor blades.",
                })
            elif diff < -limit and speed > 0.5 * ref:
                out["load_drop"] = Finding(cur, {
                    "what": "Motor load dropped suddenly.",
                    "why": f"Current is {self.fmt('motor_current', cur)}. At this speed it should be about "
                           f"{self.fmt('motor_current', expected)}.",
                    "next": "Check for a sheet break or a slipping coupling.",
                })

        # R3: bearing vibration read against speed
        if v_ref is not None and vib_info and vib_info.warn and running:
            if vib <= vib_info.warn < v_ref and speed < 0.95 * ref:
                out["vib_low_speed"] = Finding(vib, {
                    "what": "Bearing shakes too much for this speed.",
                    "why": f"Shaking is {self.fmt('vibration', vib)} at {self.fmt('speed', speed)}. "
                           f"At full speed it would be about {self.fmt('vibration', v_ref)}.",
                    "next": "Plan a bearing check before going back to full speed.",
                })
            if vib_up:
                out["vib_rise"] = Finding(vib, {
                    "what": "Bearing is shaking more than usual.",
                    "why": f"Now {self.fmt('vibration', v_ref)}. Usual for this bearing is {self.fmt('vibration', base)}.",
                    "next": "Check the grease. Listen to the bearing. Plan a check.",
                })

        # R4: low steam makes the paper wet about a minute later
        steam, moist = v.get("steam_pressure"), v.get("moisture")
        m_info, s_info = inp.roles.get("moisture"), inp.roles.get("steam_pressure")
        steam_low = False
        if steam is not None and m_info and m_info.warn is not None:
            if inp.moisture_model:
                a, b = inp.moisture_model
                steam_low = a + b * steam > m_info.warn
            elif s_info and s_info.min_value is not None:
                steam_low = steam < s_info.min_value
        if steam_low:
            self.last_steam_low = t
            if running:
                out["steam_wet"] = Finding(steam, {
                    "what": "Paper is getting too wet.",
                    "why": f"Steam pressure is low: {self.fmt('steam_pressure', steam)}. {self.normal('steam_pressure')}".strip(),
                    "next": "Check the steam valve and the steam supply.",
                })

        # R5: paper too wet while steam is fine
        if moist is not None and m_info and m_info.warn is not None and running and t - self.run_since >= SETTLE_S:
            if moist > m_info.warn and t - self.last_steam_low > STEAM_MEMORY_S:
                has_steam = steam is not None
                out["wet_other"] = Finding(moist, {
                    "what": "Paper is too wet.",
                    "why": f"Moisture is {self.fmt('moisture', moist)}." + (" Steam pressure is normal." if has_steam else ""),
                    "next": "Check the press section, the felts and the dryer drains.",
                })

        # R7: paper too wet just after a restart (it breaks easily)
        if (moist is not None and m_info and m_info.max_value is not None and running
                and 0 <= t - self.run_since < WET_AFTER_RESTART_S and moist > m_info.max_value + 0.5):
            out["wet_restart"] = Finding(moist, {
                "what": "Paper is wet after the restart.",
                "why": f"Moisture is {self.fmt('moisture', moist)}. A wet sheet breaks easily.",
                "next": "Bring steam pressure back up first." if steam_low
                        else "Give the dryers a few minutes to heat up. Watch for a sheet break.",
            })
        return out


class ProcessEngine:
    """On/off delays and raise/clear decisions for the process rules of every device."""

    def __init__(self) -> None:
        self.devices: dict[str, DeviceRules] = {}
        self.pending: dict[tuple[int, str], tuple[str, float]] = {}

    def configure(self, device_id: str, inputs: Inputs) -> None:
        cur = self.devices.get(device_id)
        if cur is None:
            self.devices[device_id] = DeviceRules(inputs)
        else:
            cur.inputs = inputs  # keep the running state and the vibration baseline

    def evaluate(self, device_id: str, rules: list[Rule], t: float, running: bool, values: dict[str, float],
                 is_active) -> list[Transition]:
        dev = self.devices.get(device_id)
        if dev is None or not rules:
            return []
        found = dev.step(t, running, values)
        out = []
        for rule in rules:
            code = code_of(rule)
            if code is None:
                continue
            key, hit, active = (rule.id, device_id), found.get(code), is_active(rule.id, device_id)
            action = "raise" if hit and not active else "clear" if not hit and active else None
            if action is None:
                self.pending.pop(key, None)
                continue
            delay = rule.on_delay_s if action == "raise" else rule.off_delay_s
            since = self.pending.get(key)
            if since is None or since[0] != action:
                since = self.pending[key] = (action, t)
            if t - since[1] >= delay:
                self.pending.pop(key, None)
                out.append(Transition(action, rule, device_id, hit.value if hit else None, hit.explain if hit else None))
        return out
