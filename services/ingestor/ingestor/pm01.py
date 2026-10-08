"""PM-01 paper machine model and its gateway, shared by the stored history (history.py) and the
always-on live simulator (live_sim.py).

The model runs one step per second of real time from a fixed starting point (ANCHOR), so the
state at any second is the same whether it is reached by generating history or by the live
simulator: history and live data form one continuous run. A Plant can be saved and loaded, so the
live simulator resumes where the history (or its own last checkpoint) ended.

What it contains:

1. A mill day (plan_day). Every day (IST) gets a random plan, shaped on the mill's own sample of
   lost time ("Paper_data_Oct.xlsx": paper tears about 25 % of lost time, breakdowns, power /
   steam / pulp and planned stops about 18 % each, GSM changes and restart losses about 10 %):
   - paper tears, 9-15 a day, 8-15 min (more on a steam-problem day);
   - breakdowns, 1-2 a day, 30-90 min (a motor or a bearing; one of 2.5-3.5 h on a bad day; more
     the day after the weekly shutdown);
   - power cuts, steam supply and pulp problems, 2-4 a day (6-8 on a power-cut day), 15-60 min;
   - planned stops: a daily cleaning (45-75 min) and a 5.5-6.5 h shutdown every Wednesday;
   - GSM (grade) changes, 3-5 a day: the machine keeps running while speed and steam move to the
     new grade and the moisture settles;
   - after every stop the tail is threaded at crawl speed before the paper is on the reel again
     (restart loss), and the crew brings the speed back gradually (90-95 % of standard, back to
     standard over 10-30 min);
   - operators set the speed around the grade's standard: above it when things go well (+2-5 %
     for 1-4 h), below it when they don't (-4-8 % for 1-3 h on a bad day, about 3 % slower for an
     hour after repeated tears), well below it when pulp runs short without stopping (-8-14 %),
     and the night shift (B, 18:00-06:00) runs about 1 % slower. So performance (speed while
     running against the rated speed) moves around 100 %, above and below.
   The day type (normal, good, power-cut, steam-problem, breakdown) is drawn at random, so no two
   days are alike; on average the machine makes paper about 65 % of the time (62-69 % on most days).
2. How each kind of stop looks in the six registers (status, speed, current, steam, moisture,
   vibration): a tear keeps the machine turning at crawl speed with the web load gone; a breakdown
   or a power cut stops it abruptly with the drive off; a planned or pulp stop ramps it down. Some
   stops show what is coming: a bearing breakdown follows a vibration rise, a motor breakdown an
   overload, a steam stop a falling steam pressure, a pulp stop a slowdown. Steam goes to standby
   during a stop and takes minutes to come back, so the paper is wet just after a restart.
3. The PLC guide's program (Step 5, its RND function and FB_EVENT problem events 1-9 h apart for
   speed, steam, current and vibration) runs on top, and its links stay: current and vibration
   follow speed, moisture follows steam a minute later, a wet sheet can tear, a critical overload
   can trip the motor, a worn or vibrating bearing adds friction current.
4. Realistic signals: controlled values hold steady, every value is rounded to its sensor's
   resolution, and the bearing follows its life (bearing_baseline): slow wear over 150 days and a
   14-day grease cycle.
5. Gateway: two Modbus blocks per poll exactly like the real gateway ({"PM3032_DATA":[...]},
   float32 values printed as "%.6f"), once a second. Bad reads 3-5 times a day. No data during the
   weekly scheduled maintenance of the data system (Sunday 18:00-19:00 IST); the PLC and gateway
   are on a UPS, so a mill power cut does not interrupt the data.
"""

import datetime as dt
import functools
import json
import pickle
import random
import struct
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

SEED = 12345  # the guide's default start number
ANCHOR = int(dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc).timestamp())  # the model starts here
GATEWAY_IP = "192.168.18.2"
IST = ZoneInfo("Asia/Kolkata")
IST_OFFSET_S = 19800

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

# grades: name, speed (m/min), steam pressure (bar), share of the time. Moisture follows the steam
# (moisture_for), so every grade sits on the same steam -> moisture line.
GRADES = [("80 GSM", 276.5, 4.17, 0.40), ("90 GSM", 270.0, 4.22, 0.25),
          ("70 GSM", 280.0, 4.12, 0.20), ("85 GSM", 273.0, 4.19, 0.15)]
RATED_SPEED = 276.5
CRAWL_SPEED = 100.0  # m/min, tail threading after a tear or a restart
STANDBY_STEAM = 3.0  # bar, steam pressure held while the machine is stopped
SHUTDOWN_WEEKDAY = 2  # Wednesday

# what an operator would log as the reason of each kind of stop (Performance -> stoppage log)
REASONS = {"tear": "Web break", "mechanical": "Mechanical fault", "electrical": "Electrical trip",
           "power": "Steam / utility issue", "steam": "Steam / utility issue", "pulp": "No material",
           "cleaning": "Planned maintenance", "shutdown": "Planned maintenance"}
LOGGED_SHARE = 0.85  # operators log a reason for most stops, not all


def moisture_for(steam: float) -> float:
    """Moisture the dryers settle at for a steam pressure (guide, Step 2)."""
    return 6.14 + 1.6 * (4.17 - steam)


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


# ---------------------------------------------------------------- the mill's day


@dataclass
class Planned:
    t: int  # earliest start (epoch seconds); it waits while the machine is stopped
    kind: str  # tear | breakdown | utility | cleaning | shutdown | gsm
    duration: int = 0  # seconds stopped
    sub: str = ""  # breakdown: mechanical | electrical; utility: power | steam | pulp


DAY_TYPES = (("normal", 0.50), ("good", 0.15), ("power", 0.10), ("steam", 0.15), ("breakdown", 0.10))


def mill_day(t: float) -> int:
    """Index of the IST day that contains t."""
    return int((t + IST_OFFSET_S) // 86400)


def plan_day(day: int, seed: int = SEED) -> tuple[str, list[Planned]]:
    """A random but repeatable plan for one IST day: (day type, events in time order)."""
    rng = random.Random(seed * 7907 + day)
    start = day * 86400 - IST_OFFSET_S  # IST midnight
    weekday = dt.datetime.fromtimestamp(start + 3600, IST).weekday()
    kind = rng.choices([k for k, _ in DAY_TYPES], [w for _, w in DAY_TYPES])[0]
    after_shutdown = weekday == SHUTDOWN_WEEKDAY + 1
    at = lambda lo_h=0.0, hi_h=24.0: int(start + rng.uniform(lo_h, hi_h) * 3600)  # noqa: E731
    ev: list[Planned] = []
    shutdown = weekday == SHUTDOWN_WEEKDAY
    hours = 0.75 if shutdown else 1.0  # a shutdown day has fewer hours to tear or break down in

    tears = round((rng.randint(9, 12) + (rng.randint(3, 5) if kind == "steam" else 0) ) * hours)
    ev += [Planned(at(), "tear", rng.randint(480, 900)) for _ in range(tears)]

    n_break = {"good": rng.choice([0, 1, 1]), "breakdown": 0}.get(kind, rng.choice([1, 1, 2]) if kind == "normal" else rng.choice([1, 2])) + (1 if after_shutdown else 0)
    for _ in range(n_break):
        ev.append(Planned(at(), "breakdown", rng.randint(2400, 4800), rng.choice(["mechanical", "electrical"])))
    if kind == "breakdown":  # one long one: a motor or a bearing
        ev.append(Planned(at(6, 20), "breakdown", rng.randint(9000, 12600), rng.choice(["mechanical", "electrical"])))

    n_util = rng.randint(5, 7) if kind == "power" else rng.randint(1, 3) if kind == "good" or shutdown else rng.randint(3, 4) if kind == "normal" else rng.randint(2, 4)
    for _ in range(n_util):
        sub = "power" if kind == "power" else rng.choices(["power", "steam", "pulp"], [0.4, 0.35, 0.25])[0]
        if kind == "steam" and rng.random() < 0.5:
            sub = "steam"
        ev.append(Planned(at(), "utility", rng.randint(1200, 3000) if sub != "power" else rng.randint(900, 2400), sub))

    if shutdown:
        ev.append(Planned(at(6, 8), "shutdown", rng.randint(19800, 23400)))
    else:
        ev.append(Planned(at(6, 14), "cleaning", rng.randint(2700, 4500)))

    ev += [Planned(at(), "gsm") for _ in range(rng.randint(3, 5))]

    # how the crew runs the machine: above standard on a good stretch, below it on a bad one
    pushes = {"good": rng.randint(2, 3), "normal": rng.choice([0, 1, 1, 2])}.get(kind, rng.choice([0, 0, 1]))
    lo, hi = (0.04, 0.08) if kind == "good" else (0.02, 0.05)
    ev += [Planned(at(), "speed", rng.randint(10800, 28800) if kind == "good" else rng.randint(3600, 14400),
                   f"{rng.uniform(lo, hi):.4f}") for _ in range(pushes)]
    slow = {"good": 0, "normal": rng.choice([0, 1])}.get(kind, rng.randint(2, 3))
    ev += [Planned(at(), "speed", rng.randint(7200, 14400), f"{-rng.uniform(0.05, 0.10):.4f}") for _ in range(slow)]
    if rng.random() < 0.5:  # pulp runs short: the machine keeps going, much slower
        ev.append(Planned(at(), "speed", rng.randint(2700, 7200), f"{-rng.uniform(0.08, 0.14):.4f}"))
    # each shift's crew has its own pace that day (the night crew, B, a little slower)
    ev.append(Planned(start + 6 * 3600, "crew", 0, f"{rng.uniform(-0.015, 0.025):.4f}"))
    ev.append(Planned(start + 18 * 3600, "crew", 0, f"{rng.uniform(-0.035, 0.005):.4f}"))
    ev.sort(key=lambda e: e.t)
    return ("after shutdown" if after_shutdown and kind == "normal" else kind), ev


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


@dataclass
class Stop:
    kind: str  # tear | mechanical | electrical | power | steam | pulp | cleaning | shutdown
    remaining: float
    started: int

    @property
    def abrupt(self) -> bool:  # the drive trips or loses power: the machine coasts down at once
        return self.kind in ("mechanical", "electrical", "power")

    @property
    def drive_on(self) -> bool:  # a tear keeps the machine turning while the tail is threaded
        return self.kind == "tear"


@dataclass
class Warning_:
    """What a stop looks like before it happens (vibration rise, overload, steam loss, slowdown)."""

    kind: str
    remaining: float
    total: float
    duration: int


class PM01:
    """The machine, one step per second: the mill's day plan, the guide's program and its links."""

    WET_TRIP = (7.3, 45.0, 0.3)  # moisture %, sustained seconds, chance that such an episode tears the sheet
    OVERLOAD_TRIP = (60.0, 0.6)  # seconds into a critical overload, chance that the motor trips
    TRIP_HOLDOFF_S = 300.0  # no process trip in the first minutes after a restart (threading)
    WARNING_S = {"mechanical": (480, 900), "electrical": (120, 300), "steam": (180, 360), "pulp": (300, 600)}

    def __init__(self, seed: int = SEED) -> None:
        self.seed = seed
        self.rnd = Rnd(seed)
        self.extra = random.Random(seed * 31 + 7)  # randomness beyond the guide
        # problem events: the warning value crosses the tag's warning limit, the critical value its critical
        # limit (speed below 230 / 190 m/min, steam below 3.6 / 2.9 bar, current above 160 / 185 A,
        # vibration above 4.5 / 7.1 mm/s; moisture follows the steam above 7.0 / 8.0 %)
        self.ev_speed = FbEvent(0.0, -50.0, -95.0, random.Random(seed + 1))  # offsets from the grade's speed
        self.ev_steam = FbEvent(0.0, -0.77, -1.40, random.Random(seed + 2))  # offsets from the grade's steam
        self.ev_current = FbEvent(0.0, 26.0, 50.0, random.Random(seed + 3))
        self.ev_vibration = FbEvent(3.05, 4.8, 7.4, random.Random(seed + 4))
        self.grade = 0
        self.speed_now, self.speed_set = GRADES[0][1], GRADES[0][1]
        self.steam = self.steam_set = GRADES[0][2]
        self.moisture_slow, self.pressure_slow = 6.14, 4.17
        self.gsm_bump = 0.0  # moisture disturbance while a new grade settles
        self.trim = 0.0  # operator's speed setting against the grade's standard (+0.03 = 3 % above)
        self.crew = 0.0  # the shift crew's own pace
        self.regime: tuple[float, float] | None = None  # (offset, seconds left) of a planned speed regime
        self.ramp_left = self.ramp_total = self.ramp_depth = 0.0  # gradual speed-up after a restart
        self.tears: deque = deque(maxlen=10)  # start seconds of recent tears
        self.n_speed = self.n_current = self.n_pressure = self.n_moisture = self.n_vib = 0.0
        self.n_moisture_extra, self.n_floor = 0.0, 0.5
        self.on_reel = True
        self.stop: Stop | None = None
        self.threading = 0.0  # seconds of tail threading left after a stop
        self.warning: Warning_ | None = None
        self.run_s = 1e9  # seconds since the paper was last back on the reel
        self.wet_s = self.overload_s = 0.0
        self.wet_decided = self.overload_decided = False
        self.plan_for = -1
        self.planning = True  # False: no day plan (tests of steady running)
        self.queue: deque = deque()
        self.day_type = ""
        self.stops: deque = deque(maxlen=4000)  # (start second, kind) of each stop
        self.last_stop: tuple[int, str] | None = None

    @property
    def running(self) -> bool:
        return self.stop is None and self.threading <= 0 and self.on_reel

    # ---- plan and stops

    def _plan(self, t: int) -> None:
        day = mill_day(t)
        if day != self.plan_for and self.planning:
            self.plan_for = day
            self.day_type, events = plan_day(day, self.seed)
            self.queue.extend(e for e in events if e.t >= t - 1)  # older ones belong to the past

    def start_stop(self, t: int, kind: str, duration: float) -> None:
        if kind == "tear":
            self.tears.append(t)
        self.stop = Stop(kind, duration, t)
        self.warning = None
        self.on_reel = False
        self.stops.append((t, kind))
        self.last_stop = (t, kind)

    MIN_RUN_S = 600  # a planned event that fell due during a stop waits until the machine has run a while

    def _next_planned(self, t: int) -> None:
        if self.queue and self.queue[0].t <= t and self.queue[0].kind in ("speed", "crew"):  # crew decisions, any time
            e = self.queue.popleft()
            if e.kind == "crew":
                self.crew = float(e.sub)
            else:
                self.regime = (float(e.sub), float(e.duration))
            return
        if not self.queue or self.queue[0].t > t or self.run_s < self.MIN_RUN_S:
            return
        e = self.queue.popleft()
        if e.kind == "speed":
            self.regime = (float(e.sub), float(e.duration))
        elif e.kind == "gsm":
            self.grade = self.extra.choice([i for i in range(len(GRADES)) if i != self.grade])
            self.gsm_bump = self.extra.uniform(0.25, 0.45) * self.extra.choice([1, -1])
        elif e.kind in ("breakdown", "utility"):
            sub = e.sub
            if sub in self.WARNING_S:
                n = self.extra.uniform(*self.WARNING_S[sub])
                self.warning = Warning_(sub, n, n, e.duration)
            else:
                self.start_stop(t, sub, e.duration)
        else:
            self.start_stop(t, e.kind, e.duration)

    # ---- one second

    def step(self, t: float, vib_normal: float = VIB_NEW) -> Registers:
        rnd, extra, ti = self.rnd, self.extra, int(t)
        # 1. noise: controlled and averaged signals barely move
        self.n_speed = 0.98 * self.n_speed + (rnd() - 0.5) * 0.025
        self.n_current = 0.95 * self.n_current + (rnd() - 0.5) * 0.3
        self.n_pressure = 0.97 * self.n_pressure + (rnd() - 0.5) * 0.006
        self.n_moisture = 0.8 * self.n_moisture + (rnd() - 0.5) * 0.05
        self.n_vib = 0.995 * self.n_vib + (rnd() - 0.5) * 0.003
        self.n_moisture_extra = 0.95 * self.n_moisture_extra + (extra.random() - 0.5) * 0.03
        self.n_floor = min(1.0, max(0.0, self.n_floor + (extra.random() - 0.5) * 0.02))

        # 2. the day's plan: stops, warnings before them, grade changes
        self._plan(ti)
        if self.warning is None and (self.running or (self.queue and self.queue[0].kind in ("speed", "crew"))):
            self._next_planned(ti)
        if self.regime is not None:
            self.regime = (self.regime[0], self.regime[1] - 1) if self.regime[1] > 1 else None
        if self.warning is not None:
            self.warning.remaining -= 1
            if self.warning.remaining <= 0:
                self.start_stop(ti, self.warning.kind, self.warning.duration)
        if self.stop is not None:
            self.stop.remaining -= 1
            if self.stop.remaining <= 0:  # back up: thread the tail at crawl speed first
                self.threading = extra.uniform(60, 180)
                self.stop = None
                self.ramp_total = self.ramp_left = extra.uniform(900, 2400)  # then speed up gradually
                self.ramp_depth = extra.uniform(0.06, 0.12)
        elif self.threading > 0 and self.speed_now >= CRAWL_SPEED - 3:
            self.threading -= 1

        # 3. the guide's problem events, while running
        running = self.running
        ev_speed = self.ev_speed(running, rnd, t)
        ev_steam = self.ev_steam(running, rnd, t)
        ev_current = self.ev_current(running, rnd, t)
        ev_vib = self.ev_vibration(running, rnd, t, normal=vib_normal)
        w = self.warning
        progress = 1 - w.remaining / w.total if w else 0.0

        # 4. speed: grade speed, crawl while threading, down to zero (or crawl after a tear) when stopped
        g_name, g_speed, g_steam, _ = GRADES[self.grade]
        self.speed_set += max(-0.5, min(0.5, g_speed - self.speed_set))  # a grade change moves gently
        if self.stop is not None:
            target = CRAWL_SPEED if self.stop.drive_on else 0.0
            rate = 20.0 if self.stop.abrupt else 3.0
        elif self.threading > 0:
            target, rate = CRAWL_SPEED, 3.0
        else:
            # the crew's setting: planned regime, caution after repeated tears, night shift, restart ramp
            trim = self.regime[0] if self.regime else 0.0
            if sum(1 for x in self.tears if ti - x < 3600) >= 2:  # repeated tears: the crew backs off
                trim -= 0.03
            trim += self.crew
            if self.ramp_left > 0:
                trim -= self.ramp_depth * self.ramp_left / self.ramp_total
                if self.on_reel:
                    self.ramp_left -= 1
            self.trim += max(-0.0005, min(0.0005, trim - self.trim))  # about 0.15 m/min per second
            target, rate = self.speed_set * (1 + self.trim) + ev_speed, 3.0
            if w and w.kind == "pulp":  # not enough stock: slow down before stopping
                target -= 45.0 * progress
        if self.speed_now < target - rate:
            self.speed_now += rate
        elif self.speed_now > target + rate:
            self.speed_now -= rate
        else:
            self.speed_now = target
        speed = 0.0 if self.speed_now < 1.0 else self.speed_now + self.n_speed
        if self.stop is None and self.threading <= 0 and not self.on_reel and speed >= target - 3:
            self.on_reel, self.run_s = True, 0.0  # the paper is on the reel again

        # 5. steam: the grade's pressure while making paper, standby while stopped
        if self.stop is not None:
            steam_target, tau = (STANDBY_STEAM - 0.4, 400.0) if self.stop.kind == "power" else (STANDBY_STEAM, 90.0)
        else:
            steam_target, tau = g_steam + ev_steam, (120.0 if not self.on_reel else 60.0)
            if w and w.kind == "steam":  # the steam supply is failing
                steam_target -= 1.4 * progress
        self.steam_set += (steam_target - self.steam_set) / tau
        pressure = self.steam_set + self.n_pressure

        # 6. linked values (guide, Step 2)
        if w and w.kind == "mechanical":  # a bearing failing: vibration climbs before the breakdown
            ev_vib += 2.2 * progress
        friction = FRICTION_A_PER_MMS * max(0.0, ev_vib - VIB_NEW) * speed / RATED_SPEED
        overload = 28.0 * min(1.0, 2 * progress) if w and w.kind == "electrical" else 0.0
        if self.stop is not None and not self.stop.drive_on and speed == 0.0:
            current = 0.0  # drive off
        elif not self.on_reel:  # turning without the web (tear, threading): no web load
            current = 11.0 + 0.30 * speed + self.n_current
        else:
            current = (11.0 + 0.458 * speed + (ev_current + overload) * min(1.0, speed / RATED_SPEED)
                       + friction + self.n_current)
        floor = 0.1 + 0.25 * self.n_floor  # vibration sensor noise floor at standstill
        vibration = max(floor, ev_vib * speed / RATED_SPEED + self.n_vib)
        self.pressure_slow += (pressure - self.pressure_slow) / 180.0
        overdry = 0.5 * max(0.0, pressure - self.pressure_slow)
        self.gsm_bump *= 1 - 1 / 300.0
        if self.on_reel:  # the scanner is off the sheet otherwise: it holds its last value
            target_m = moisture_for(pressure) - overdry + self.gsm_bump
            self.moisture_slow += (target_m - self.moisture_slow) / 60.0
        moisture = self.moisture_slow + self.n_moisture + self.n_moisture_extra

        # 7. process trips: a wet sheet tears, an overloaded motor trips (each episode decided once)
        if self.running:
            self.run_s += 1.0
            wet_pct, wet_hold, wet_p = self.WET_TRIP
            self.wet_s = self.wet_s + 1.0 if moisture > wet_pct else 0.0
            if self.wet_s == 0.0:
                self.wet_decided = False
            ovl_hold, ovl_p = self.OVERLOAD_TRIP
            self.overload_s = self.overload_s + 1.0 if self.ev_current.is_critical else 0.0
            if self.overload_s == 0.0:
                self.overload_decided = False
            if self.run_s >= self.TRIP_HOLDOFF_S:
                if self.wet_s >= wet_hold and not self.wet_decided:
                    self.wet_decided = True
                    if extra.random() < wet_p:
                        self.start_stop(ti, "tear", extra.uniform(480, 900))
                elif self.overload_s >= ovl_hold and not self.overload_decided:
                    self.overload_decided = True
                    if extra.random() < ovl_p:
                        self.start_stop(ti, "electrical", extra.uniform(1200, 2700))

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


@dataclass
class Plant:
    """The PLC and its gateway from ANCHOR on. `t` is the next second to run."""

    seed: int = SEED
    anchor: int = ANCHOR
    maintenance: dict | None = field(default_factory=lambda: dict(MAINTENANCE))

    def __post_init__(self) -> None:
        self.plc, self.t = PM01(self.seed), self.anchor
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
