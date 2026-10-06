"""PM-01 paper machine model and its gateway, shared by the stored history (history.py) and the
always-on live simulator (live_sim.py).

The model runs one step per second of real time from a fixed starting point (ANCHOR), so the
state at any second is the same whether it is reached by generating history or by the live
simulator: history and live data form one continuous run. A Plant can be saved and loaded, so the
live simulator resumes where the history (or its own last checkpoint) ended.

What it contains:

1. PLC program: a port of the "PM-01 Demo Data: Simple PLC Guide" Step 5 program (its RND
   function, linked values, about 8 stops a day, problem events per driver via FB_EVENT), with:
   - problem events 1-9 h apart, each with its own ramp rate and length;
   - some stops caused by the process (a wet sheet breaks, a critical overload trips the drive);
   - a worn or vibrating bearing adds friction current; moisture over-dries after a steam recovery.
2. Realistic signals: controlled values hold steady. Speed (drive), steam pressure (controller) and
   vibration (an RMS value, averaged by the sensor) barely move at a steady speed; every value is
   rounded to its sensor's resolution, so a reading repeats for long stretches (vibration 3.51,
   3.51, 3.52 ...), but never freezes completely.
3. Bearing life (bearing_baseline): vibration at full speed follows the bearing, not a random walk:
   a slow, accelerating wear over a 150-day bearing life (then a new bearing), a 14-day grease
   cycle (rises as the grease ages, drops back after greasing), and a small day-to-day variation.
4. Gateway: two Modbus blocks per poll exactly like the real gateway ({"PM3032_DATA":[...]},
   float32 values printed as "%.6f"), once a second. Bad reads 3-5 times a day (negative current,
   garbage speed, unknown status code, an unencodable "nan"). No data during the weekly scheduled
   maintenance hour (Sunday 18:00-19:00 IST); otherwise no communication loss.
"""

import datetime as dt
import functools
import json
import pickle
import random
import struct
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

SEED = 12345  # the guide's default start number
ANCHOR = int(dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc).timestamp())  # the model starts here
GATEWAY_IP = "192.168.18.2"

# scheduled maintenance of the data system: no data, no offline alarm (see asset_config.maintenance)
MAINTENANCE = {"weekday": "Sunday", "start": "18:00", "end": "19:00", "tz": "Asia/Kolkata"}

# bearing life
VIB_NEW = 2.95  # mm/s at full speed, new bearing, fresh grease
BEARING_INSTALLED = int(dt.datetime(2026, 8, 15, tzinfo=dt.timezone.utc).timestamp())
BEARING_LIFE_S = 150 * 86400  # replaced every 150 days, before it reaches the warning level
GREASED = int(dt.datetime(2026, 9, 7, 4, 30, tzinfo=dt.timezone.utc).timestamp())  # a Monday, 10:00 IST
GREASE_INTERVAL_S = 14 * 86400
FRICTION_A_PER_MMS = 1.5  # A of extra drive current per mm/s of vibration above VIB_NEW, at full speed

# sensor resolution: what the PLC registers can show
RESOLUTION = {"speed": 0.1, "current": 0.1, "pressure": 0.01, "moisture": 0.01, "vibration": 0.01}


# ---------------------------------------------------------------- calendar


def _hm(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def in_maintenance(t: float, window: dict | None = MAINTENANCE) -> bool:
    """t (epoch seconds) inside the weekly maintenance window {weekday, start, end, tz}."""
    if not window:
        return False
    local = dt.datetime.fromtimestamp(t, ZoneInfo(window.get("tz", "UTC")))
    if local.strftime("%A") != window.get("weekday"):
        return False
    minute = local.hour * 60 + local.minute
    return _hm(window["start"]) <= minute < _hm(window["end"])


@functools.lru_cache(maxsize=64)
def _day_wobble(day: int, seed: int) -> float:
    return random.Random(seed * 104729 + day).gauss(0.0, 0.015)


def bearing_baseline(t: float, seed: int = SEED) -> float:
    """Vibration at full speed for the bearing's condition at time t (mm/s)."""
    age = ((t - BEARING_INSTALLED) % BEARING_LIFE_S) / BEARING_LIFE_S  # 0 = new bearing, 1 = replaced
    wear = 0.2 * age + 1.0 * age ** 3
    grease = ((t - GREASED) % GREASE_INTERVAL_S) / GREASE_INTERVAL_S  # 0 = just greased
    lube = 0.10 * grease ** 1.5
    d = t / 86400
    i = int(d)
    wobble = _day_wobble(i, seed) + (_day_wobble(i + 1, seed) - _day_wobble(i, seed)) * (d - i)
    return VIB_NEW + wear + lube + wobble


# ---------------------------------------------------------------- PLC program (guide, Step 5)


class Rnd:
    """FUNCTION RND from the guide: Seed := (Seed * 75 + 74) MOD 65537; RND := Seed / 65537."""

    def __init__(self, seed: int) -> None:
        self.seed = seed

    def __call__(self) -> float:
        self.seed = (self.seed * 75 + 74) % 65537
        return self.seed / 65537.0


class FbEvent:
    """FUNCTION_BLOCK FB_EVENT: wait, move to the warning value (critical 1 in 5), hold, move back.
    The guide waits 4-6 h, holds 3-10 min and always moves 1/40 of the distance per second; here
    the wait is 1-9 h and every event draws its own hold (2-15 min) and ramp rate (1/15-1/90)."""

    WAIT = (3600.0, 28800.0)
    LENGTH = (120.0, 780.0)
    RAMP = (15.0, 90.0)  # seconds-scale of the move: value += distance / ramp each second

    def __init__(self, normal: float, warning: float, critical: float, shape: random.Random | None = None) -> None:
        self.normal, self.warning, self.critical = normal, warning, critical
        self.stage, self.countdown, self.target, self.value, self.started = 0, 0.0, normal, normal, False
        self.shape = shape or random.Random(0)
        self.ramp = 40.0
        self.events: deque = deque(maxlen=2000)  # ("warning"|"critical", start second), for summaries

    def __call__(self, running: bool, rnd: Rnd, t: float = 0.0, normal: float | None = None) -> float:
        if normal is not None:
            self.normal = normal
        if not self.started:
            self.value = self.target = self.normal
            self.countdown = self.WAIT[0] + rnd() * self.WAIT[1]
            self.started = True
        self.countdown -= 1.0
        if self.stage == 0:
            self.target = self.normal
            if self.countdown <= 0.0 and running:
                critical = rnd() < 0.2
                self.target = self.critical if critical else self.warning
                self.events.append(("critical" if critical else "warning", t))
                self.countdown = self.LENGTH[0] + rnd() * self.LENGTH[1]
                self.ramp = self.shape.uniform(*self.RAMP)
                self.stage = 1
        elif self.countdown <= 0.0:
            self.target = self.normal
            self.countdown = self.WAIT[0] + rnd() * self.WAIT[1]
            self.stage = 0
        self.value += (self.target - self.value) / self.ramp
        return self.value

    @property
    def is_critical(self) -> bool:
        return self.stage == 1 and self.target == self.critical


@dataclass
class Registers:
    status: int
    speed: float
    current: float
    pressure: float
    moisture: float
    vibration: float


def _q(v: float, step: float) -> float:
    return round(round(v / step) * step, 6)


class PM01:
    """PROGRAM PM01_DEMO, executed once per second."""

    TIMER_STRETCH = 1.25  # random stops a bit rarer, so process trips keep the total near 8 a day
    WET_TRIP = (7.3, 45.0, 0.3)  # moisture %, sustained seconds, chance that such an episode breaks the sheet
    OVERLOAD_TRIP = (60.0, 0.6)  # seconds into a critical overload, chance that the drive trips
    TRIP_HOLDOFF_S = 300.0  # no process trip in the first minutes after a restart (threading)

    def __init__(self, seed: int = SEED) -> None:
        self.rnd = Rnd(seed)
        self.extra = random.Random(seed * 31 + 7)  # randomness beyond the guide
        self.running, self.on_reel = True, True
        self.time_to_stop, self.stop_length = 5400.0, 0.0
        self.ev_speed = FbEvent(276.5, 255.0, 235.0, random.Random(seed + 1))
        self.ev_steam = FbEvent(4.17, 3.40, 3.00, random.Random(seed + 2))
        self.ev_current = FbEvent(0.0, 20.0, 30.0, random.Random(seed + 3))
        self.ev_vibration = FbEvent(3.05, 4.8, 5.8, random.Random(seed + 4))
        self.speed_now, self.moisture_slow, self.pressure_slow = 276.5, 6.14, 4.17
        self.n_speed = self.n_current = self.n_pressure = self.n_moisture = self.n_vib = 0.0
        self.n_moisture_extra, self.n_floor = 0.0, 0.5
        self.run_s = 1e9  # seconds since the last restart
        self.wet_s = self.overload_s = 0.0
        self.wet_decided = self.overload_decided = False
        self.stops: deque = deque(maxlen=2000)  # start second of each stop
        self.causes: deque = deque(maxlen=2000)  # "random", "wet sheet break" or "drive overload trip"

    def trip(self, t: float, cause: str) -> None:
        self.running = False
        self.stop_length = 300.0 + self.extra.random() * 1200.0
        self.stops.append(t)
        self.causes.append(cause)

    def step(self, t: float, vib_normal: float = VIB_NEW) -> Registers:
        rnd = self.rnd
        # 1. noise. Controlled and averaged signals barely move: the drive holds speed within about
        #    0.1 m/min, the steam controller pressure within about 0.01 bar, and the vibration
        #    sensor reports an RMS over a few seconds (about 0.01 mm/s of drift).
        self.n_speed = 0.98 * self.n_speed + (rnd() - 0.5) * 0.025
        self.n_current = 0.95 * self.n_current + (rnd() - 0.5) * 0.3
        self.n_pressure = 0.97 * self.n_pressure + (rnd() - 0.5) * 0.006
        self.n_moisture = 0.8 * self.n_moisture + (rnd() - 0.5) * 0.05
        self.n_vib = 0.995 * self.n_vib + (rnd() - 0.5) * 0.003
        extra = self.extra
        self.n_moisture_extra = 0.95 * self.n_moisture_extra + (extra.random() - 0.5) * 0.03
        self.n_floor = min(1.0, max(0.0, self.n_floor + (extra.random() - 0.5) * 0.02))
        # 2. stops (Step 3)
        if self.running:
            self.time_to_stop -= 1.0
            if self.time_to_stop <= 0.0:
                self.running = False
                self.stop_length = 300.0 + rnd() * 1200.0
                self.stops.append(t)
                self.causes.append("random")
        else:
            self.stop_length -= 1.0
            if self.stop_length <= 0.0:
                self.running = True
                self.run_s = 0.0
                self.time_to_stop = (4200.0 + rnd() * 10200.0) * self.TIMER_STRETCH
        # 3. problem events (Step 4); the vibration event rises from the bearing's condition
        ev_speed = self.ev_speed(self.running, rnd, t)
        ev_steam = self.ev_steam(self.running, rnd, t)
        ev_current = self.ev_current(self.running, rnd, t)
        ev_vib = self.ev_vibration(self.running, rnd, t, normal=vib_normal)
        # 4. speed ramps 3 m/min per second
        target = ev_speed if self.running else 0.0
        if self.speed_now < target - 3.0:
            self.speed_now += 3.0
        elif self.speed_now > target + 3.0:
            self.speed_now -= 3.0
        else:
            self.speed_now = target
        speed = 0.0 if self.speed_now < 1.0 else self.speed_now + self.n_speed
        # 5. is paper being made?
        if not self.running:
            self.on_reel = False
        elif speed > 270.0:
            self.on_reel = True
        # 6. linked values (Step 2)
        friction = FRICTION_A_PER_MMS * max(0.0, ev_vib - VIB_NEW) * speed / 276.5
        # load events are drag on the moving machine: none at standstill (e.g. after an overload trip)
        current = 11.0 + 0.458 * speed + ev_current * min(1.0, speed / 276.5) + friction + self.n_current
        pressure = ev_steam + self.n_pressure
        floor = 0.1 + 0.25 * self.n_floor  # sensor noise floor at standstill
        vibration = max(floor, ev_vib * speed / 276.5 + self.n_vib)
        # the cylinders heat up again more slowly than the steam pressure returns: right after a
        # recovery the sheet over-dries for a few minutes
        self.pressure_slow += (pressure - self.pressure_slow) / 180.0
        overdry = 0.5 * max(0.0, pressure - self.pressure_slow)
        if self.on_reel:
            target_m = 6.14 + 1.6 * (4.17 - pressure) - overdry
            self.moisture_slow += (target_m - self.moisture_slow) / 60.0
        moisture = self.moisture_slow + self.n_moisture + self.n_moisture_extra
        # 7. process trips: a wet sheet breaks, an overloaded drive trips (each episode decided once)
        if self.running:
            self.run_s += 1.0
            wet_pct, wet_hold, wet_p = self.WET_TRIP
            self.wet_s = self.wet_s + 1.0 if self.on_reel and moisture > wet_pct else 0.0
            if self.wet_s == 0.0:
                self.wet_decided = False
            ovl_hold, ovl_p = self.OVERLOAD_TRIP
            self.overload_s = self.overload_s + 1.0 if self.ev_current.is_critical else 0.0
            if self.overload_s == 0.0:
                self.overload_decided = False
            if self.run_s >= self.TRIP_HOLDOFF_S:
                if self.wet_s >= wet_hold and not self.wet_decided:
                    self.wet_decided = True
                    if self.extra.random() < wet_p:
                        self.trip(t, "wet sheet break")
                elif self.overload_s >= ovl_hold and not self.overload_decided:
                    self.overload_decided = True
                    if self.extra.random() < ovl_p:
                        self.trip(t, "drive overload trip")
        # 8. status, values at the sensors' resolution
        r = RESOLUTION
        return Registers(1 if self.on_reel else 0, _q(speed, r["speed"]), _q(current, r["current"]),
                         _q(pressure, r["pressure"]), _q(moisture, r["moisture"]), _q(vibration, r["vibration"]))


# ---------------------------------------------------------------- gateway


def f32(v: float) -> float:
    """The value as the PLC stores it (IEEE float32), which is what the gateway prints."""
    return struct.unpack(">f", struct.pack(">f", v))[0]


def block(addr: int, size: int, name: str, data: str) -> bytes:
    doc = {"PM3032_DATA": [{"server_id": 1, "addr": addr - 400000, "full_addr": str(addr), "size": size,
                            "data": data, "ip": GATEWAY_IP, "name": name}]}
    return json.dumps(doc, separators=(",", ":")).encode()


def d1_payload(values: list[float], raw: list[str] | None = None) -> bytes:
    data = "[" + ",".join(raw or [f"{f32(v):.6f}" for v in values]) + "]"
    return block(400002, 50, "D1", data)


def d2_payload(status: int) -> bytes:
    return block(400001, 3, "D2", f"[{status}]")


GLITCH_KINDS = ("negative_current", "garbage_speed", "unknown_status", "nan")


def glitches_of_day(day: int, seed: int = SEED) -> dict[int, str]:
    """Bad reads of one UTC day (day = epoch // 86400): second -> kind, 3-5 a day."""
    rng = random.Random(seed * 7919 + day)
    return {day * 86400 + rng.randint(300, 86100): rng.choice(GLITCH_KINDS) for _ in range(rng.randint(3, 5))}


def gateway_payloads(regs: Registers, glitch: str | None) -> list[bytes]:
    """The gateway's two messages for one poll: values block D1, then status block D2."""
    values = [regs.speed, regs.current, regs.pressure, regs.moisture, regs.vibration]
    status, raw = regs.status, None
    if glitch == "negative_current":
        values[1] = -12.5  # sign-flipped register: below the valid minimum
    elif glitch == "garbage_speed":
        values[0] = 3.4028230607370965e38  # all bits set except sign/exponent LSB
    elif glitch == "unknown_status":
        status = 65535
    elif glitch == "nan":
        raw = [f"{f32(v):.6f}" for v in values]
        raw[3] = "nan"  # the gateway cannot print a NaN float as JSON
    return [d1_payload(values, raw), d2_payload(status)]


# ---------------------------------------------------------------- the plant: model + gateway over time


class Plant:
    """The PLC and its gateway from ANCHOR on. `t` is the next second to run."""

    def __init__(self, seed: int = SEED, anchor: int = ANCHOR, maintenance: dict | None = MAINTENANCE) -> None:
        self.seed, self.plc, self.t, self.maintenance = seed, PM01(seed), anchor, maintenance
        self._glitch_day: tuple[int, dict] | None = None

    def glitch(self, t: int) -> str | None:
        day = t // 86400
        if self._glitch_day is None or self._glitch_day[0] != day:
            self._glitch_day = (day, glitches_of_day(day, self.seed))
        return self._glitch_day[1].get(t)

    def poll(self) -> tuple[int, Registers, list[bytes]]:
        """Run one second: (time, registers, gateway messages; none during maintenance)."""
        t = self.t
        regs = self.plc.step(t, vib_normal=bearing_baseline(t, self.seed))
        self.t += 1
        if in_maintenance(t, self.maintenance):
            return t, regs, []
        return t, regs, gateway_payloads(regs, self.glitch(t))

    def advance_to(self, t_end: int) -> None:
        """Run up to (not including) t_end without producing messages."""
        plc, seed = self.plc, self.seed
        while self.t < t_end:
            plc.step(self.t, vib_normal=bearing_baseline(self.t, seed))
            self.t += 1

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        self._glitch_day = None
        tmp.write_bytes(pickle.dumps(self))
        tmp.replace(path)

    @staticmethod
    def load(path: str | Path) -> "Plant | None":
        try:
            plant = pickle.loads(Path(path).read_bytes())
        except (OSError, pickle.UnpicklingError, EOFError, AttributeError):
            return None
        return plant if isinstance(plant, Plant) else None
