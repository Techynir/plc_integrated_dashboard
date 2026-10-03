"""Generate realistic PM-01 paper machine history and store it as if the real PLC had sent it.

    docker compose stop ingestor
    docker compose run --rm --no-deps ingestor python -m ingestor.history conveyer-plc-line-01 --days 7 --replace
    docker compose start ingestor

Three layers, so the result is indistinguishable from real data:

1. PLC program: a line-by-line port of the "PM-01 Demo Data: Simple PLC Guide" Step 5 program
   (its RND function, noise, linked values, 8-9 random stops a day, 4-5 problem events a day per
   driver via FB_EVENT). One addition: the bearing slowly wears (vibration baseline drifts up by
   WEAR_PER_DAY), so Asset health has a trend to show.
2. Gateway: polls the registers once a second and publishes the two Modbus blocks exactly like the
   real gateway (topic plc/<site>/<line>/<device>/telemetry, {"PM3032_DATA":[...]} with float32
   values printed as "%.6f"), delivered in bursts every 5 s. On top: communication outages
   (short drops every day, a gateway reboot and one long network failure) and bad reads
   (negative or garbage values, an unknown status code, an unencodable "nan").
3. Replay: every message goes through the live ingestor's own handler (decoding, bad-read
   rejection, status, raw data, alarm engine with its delays and suppression, offline detection)
   with a simulated clock, so stored values, raw messages and alarms carry historical times.

The live ingestor must be stopped while this runs (it keeps its own copy of device state).
With --replace, the device's stored values, raw messages, alarms, stop reasons and ingest errors
inside the window are deleted first. --dry-run only prints what would be generated.
"""

import argparse
import asyncio
import datetime as dt
import json
import os
import random
import struct
import sys
import time
from dataclasses import dataclass, field

import asyncpg

from . import main as live
from .main import Ingestor
from .store import Store

POLL_S = 1.0  # PLC task and gateway poll interval
BURST_S = 5  # the gateway delivers its buffered polls every 5 s
WARMUP_S = 6 * 3600  # run the PLC before the window so stops/events are mid-cycle at the start
WEAR_PER_DAY = 0.03  # mm/s per day added to the vibration baseline (bearing wear)
GATEWAY_IP = "192.168.18.2"


# ---------------------------------------------------------------- PLC program (guide, Step 5)


class Rnd:
    """FUNCTION RND from the guide: Seed := (Seed * 75 + 74) MOD 65537; RND := Seed / 65537."""

    def __init__(self, seed: int) -> None:
        self.seed = seed

    def __call__(self) -> float:
        self.seed = (self.seed * 75 + 74) % 65537
        return self.seed / 65537.0


class FbEvent:
    """FUNCTION_BLOCK FB_EVENT: wait 4-6 h, move to the warning value (critical 1 in 5) for
    3-10 min, move back; values move smoothly (1/40 of the distance per second)."""

    WAIT = (14400.0, 7200.0)
    LENGTH = (180.0, 420.0)

    def __init__(self, normal: float, warning: float, critical: float) -> None:
        self.normal, self.warning, self.critical = normal, warning, critical
        self.stage, self.countdown, self.target, self.value, self.started = 0, 0.0, normal, normal, False
        self.events: list[tuple[str, float]] = []  # ("warning"|"critical", start second) for summaries

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
                self.stage = 1
        elif self.countdown <= 0.0:
            self.target = self.normal
            self.countdown = self.WAIT[0] + rnd() * self.WAIT[1]
            self.stage = 0
        self.value += (self.target - self.value) / 40.0
        return self.value


@dataclass
class Registers:
    status: int
    speed: float
    current: float
    pressure: float
    moisture: float
    vibration: float


class PM01:
    """PROGRAM PM01_DEMO, executed once per second."""

    def __init__(self, seed: int = 12345) -> None:
        self.rnd = Rnd(seed)
        self.running, self.on_reel = True, True
        self.time_to_stop, self.stop_length = 5400.0, 0.0
        self.ev_speed = FbEvent(276.5, 255.0, 235.0)
        self.ev_steam = FbEvent(4.17, 3.40, 3.00)
        self.ev_current = FbEvent(0.0, 20.0, 30.0)
        self.ev_vibration = FbEvent(3.05, 4.8, 5.8)
        self.speed_now, self.moisture_slow = 276.5, 6.14
        self.n_speed = self.n_current = self.n_pressure = self.n_moisture = self.n_vib = 0.0
        self.stops: list[float] = []  # start second of each stop

    def step(self, t: float, vib_normal: float = 3.05) -> Registers:
        rnd = self.rnd
        # 1. noise (Step 1)
        self.n_speed = 0.8 * self.n_speed + (rnd() - 0.5) * 0.7
        self.n_current = 0.8 * self.n_current + (rnd() - 0.5) * 0.8
        self.n_pressure = 0.8 * self.n_pressure + (rnd() - 0.5) * 0.025
        self.n_moisture = 0.8 * self.n_moisture + (rnd() - 0.5) * 0.05
        self.n_vib = 0.8 * self.n_vib + (rnd() - 0.5) * 0.07
        # 2. stops (Step 3)
        if self.running:
            self.time_to_stop -= 1.0
            if self.time_to_stop <= 0.0:
                self.running = False
                self.stop_length = 300.0 + rnd() * 1200.0
                self.stops.append(t)
        else:
            self.stop_length -= 1.0
            if self.stop_length <= 0.0:
                self.running = True
                self.time_to_stop = 4200.0 + rnd() * 10200.0
        # 3. problem events (Step 4)
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
        current = 11.0 + 0.458 * speed + ev_current + self.n_current
        pressure = ev_steam + self.n_pressure
        vibration = max(0.2, ev_vib * speed / 276.5 + self.n_vib)
        if self.on_reel:
            target_m = 6.14 + 1.6 * (4.17 - pressure)
            self.moisture_slow += (target_m - self.moisture_slow) / 60.0
        moisture = self.moisture_slow + self.n_moisture
        # 7. status
        return Registers(1 if self.on_reel else 0, speed, current, pressure, moisture, vibration)


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


@dataclass
class Scenario:
    """Communication outages [start, end) and bad reads (second -> kind), in window seconds."""

    outages: list[tuple[float, float, str]] = field(default_factory=list)
    glitches: dict[int, str] = field(default_factory=dict)

    def offline(self, t: float) -> bool:
        return any(a <= t < b for a, b, _ in self.outages)


GLITCH_KINDS = ("negative_current", "garbage_speed", "unknown_status", "nan")


def plan_scenarios(total_s: int, seed: int) -> Scenario:
    """Network and read problems, independent of the PLC program's random stream."""
    rng = random.Random(seed)
    sc = Scenario()
    days = max(1, total_s // 86400)
    for day in range(days):
        base = day * 86400
        # a few short drops a day: most under the 60 s offline-alarm delay, some above it
        for _ in range(rng.randint(2, 4)):
            start = base + rng.uniform(600, 86400 - 600)
            length = rng.choice([rng.uniform(20, 50), rng.uniform(20, 50), rng.uniform(70, 240)])
            sc.outages.append((start, start + length, "network drop"))
        # bad reads: 3-5 a day
        for _ in range(rng.randint(3, 5)):
            sc.glitches[int(base + rng.uniform(300, 86400 - 300))] = rng.choice(GLITCH_KINDS)
    if days >= 3:  # one gateway reboot (~35 min) and one long network failure (~2 h)
        start = 1 * 86400 + rng.uniform(9, 15) * 3600
        sc.outages.append((start, start + rng.uniform(30, 40) * 60, "gateway reboot"))
        start = (days - 3) * 86400 + rng.uniform(1, 4) * 3600
        sc.outages.append((start, start + rng.uniform(110, 130) * 60, "network failure"))
    sc.outages.sort()
    sc.outages = [o for o in sc.outages if o[1] < total_s - 120]  # end the window with live data
    return sc


@dataclass
class Message:
    received_at: float  # epoch seconds
    payload: bytes


def generate(start: float, total_s: int, seed: int, scenario: Scenario):
    """Yield gateway messages for [start, start + total_s) in arrival order, plus stats at the end."""
    plc = PM01(seed)
    for w in range(WARMUP_S):  # settle the PLC before the window
        plc.step(w - WARMUP_S, vib_normal=3.0)
    plc.stops.clear()
    for ev in (plc.ev_speed, plc.ev_steam, plc.ev_current, plc.ev_vibration):
        ev.events.clear()
    jitter = random.Random(seed + 7)
    buffer: list[tuple[int, Registers]] = []
    stats = {"polls": 0, "lost_polls": 0, "messages": 0}
    for s in range(total_s):
        regs = plc.step(s, vib_normal=3.0 + WEAR_PER_DAY * s / 86400)
        if scenario.offline(s):
            stats["lost_polls"] += 1  # no reply from the gateway: the poll is lost, not delayed
        else:
            buffer.append((s, regs))
            stats["polls"] += 1
        if (s + 1) % BURST_S == 0 and buffer and scenario.offline(s + 1):
            stats["lost_polls"] += len(buffer)  # the link is down when the burst is due: lost too
            stats["polls"] -= len(buffer)
            buffer.clear()
        if (s + 1) % BURST_S == 0 and buffer:
            t = start + s + 1 + jitter.uniform(0.015, 0.12)
            for k, (ps, r) in enumerate(buffer):
                if k and jitter.random() < 0.3:
                    t += jitter.uniform(0.04, 0.1)  # the gateway sometimes pauses inside a burst
                values = [r.speed, r.current, r.pressure, r.moisture, r.vibration]
                status = r.status
                raw = None
                kind = scenario.glitches.get(ps)
                if kind == "negative_current":
                    values[1] = -12.5  # sign-flipped register: below the valid minimum
                elif kind == "garbage_speed":
                    values[0] = 3.4028230607370965e38  # all bits set except sign/exponent LSB
                elif kind == "unknown_status":
                    status = 65535
                elif kind == "nan":
                    raw = [f"{f32(v):.6f}" for v in values]
                    raw[3] = "nan"  # gateway cannot print a NaN float as JSON
                yield Message(t, d1_payload(values, raw))
                t += jitter.uniform(0.0009, 0.0013)
                yield Message(t, d2_payload(status))
                t += jitter.uniform(0.0009, 0.0013)
                stats["messages"] += 2
            buffer.clear()
    stats["stops"] = len(plc.stops)
    stats["events"] = {
        name: {k: sum(1 for kind, _ in ev.events if kind == k) for k in ("warning", "critical")}
        for name, ev in (("speed", plc.ev_speed), ("steam", plc.ev_steam), ("current", plc.ev_current),
                         ("vibration", plc.ev_vibration))
    }
    yield stats


# ---------------------------------------------------------------- replay through the live ingestor


class HistoryStore(Store):
    """Store whose alarm and error times come from the replay clock instead of now()."""

    def __init__(self, pool: asyncpg.Pool, clock) -> None:
        super().__init__(pool)
        self.clock = clock

    async def raise_alarm(self, rule, device_id, message, value):
        row = await self.pool.fetchrow(
            f"""
            INSERT INTO alarms (rule_id, rule_name, device_id, tag, severity, message, trigger_value, context, raised_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (rule_id, device_id) WHERE cleared_at IS NULL DO NOTHING
            RETURNING {self.ALARM_COLUMNS}
            """,
            rule.id, rule.name, device_id, rule.tag, rule.severity, message, value, rule.guidance, self.clock(),
        )
        if row is not None:
            return self._alarm_dict(row), True
        row = await self.pool.fetchrow(
            f"SELECT {self.ALARM_COLUMNS} FROM alarms WHERE rule_id = $1 AND device_id = $2 AND cleared_at IS NULL",
            rule.id, device_id,
        )
        return self._alarm_dict(row), False

    async def clear_alarm(self, alarm_id):
        row = await self.pool.fetchrow(
            f"UPDATE alarms SET cleared_at = $2 WHERE id = $1 AND cleared_at IS NULL RETURNING {self.ALARM_COLUMNS}",
            alarm_id, self.clock(),
        )
        return self._alarm_dict(row) if row else None

    async def record_error(self, topic, device_id, reason, payload):
        await self.pool.execute(
            "INSERT INTO ingest_errors (ts, topic, device_id, reason, payload) VALUES ($1, $2, $3, $4, $5)",
            self.clock(), topic, device_id, reason[:500], payload[:4000].decode(errors="replace"),
        )


class Replay(Ingestor):
    def __init__(self) -> None:
        super().__init__()
        self.now = dt.datetime.now(dt.timezone.utc)
        live.utcnow = lambda: self.now  # the live handler stamps messages with this clock

    async def housekeeping(self) -> None:
        """One pass of Ingestor.housekeeping_loop at the replay time (offline detection)."""
        for dev in list(self.devices.values()):
            if dev.last_seen is None or not dev.enabled:
                continue
            silent_s = (self.now - dev.last_seen).total_seconds()
            if dev.online and silent_s > dev.offline_after_s:
                await self.mark_offline(dev, reason=f"no data for {silent_s:.0f}s")
            if not dev.online:
                for tr in self.engine.on_offline_time(dev.device_id, silent_s):
                    await self.apply_transition(tr)


async def delete_window(pool: asyncpg.Pool, device_id: str, start: dt.datetime, end: dt.datetime) -> None:
    async with pool.acquire() as conn, conn.transaction():
        for sql in (
            "DELETE FROM telemetry WHERE device_id = $1 AND ts >= $2 AND ts < $3",
            "DELETE FROM raw_messages WHERE device_id = $1 AND ts >= $2 AND ts < $3",
            "DELETE FROM alarms WHERE device_id = $1 AND raised_at >= $2 AND raised_at < $3",
            "DELETE FROM stoppage_reasons WHERE device_id = $1 AND started_at >= $2 AND started_at < $3",
            "DELETE FROM ingest_errors WHERE device_id = $1 AND ts >= $2 AND ts < $3",
        ):
            res = await conn.execute(sql, device_id, start, end)
            print(f"  {sql.split()[2]:<18} {res.split()[-1]:>9} rows removed")
        await conn.execute("DELETE FROM tag_latest WHERE device_id = $1", device_id)
        # nothing is known about the device before the window
        await conn.execute(
            "UPDATE devices SET online = false, status = NULL, last_seen = NULL, msg_count = 0, last_seq = NULL, "
            "seq_gaps = 0 WHERE device_id = $1", device_id,
        )


async def run(args: argparse.Namespace) -> None:
    end = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) if args.end is None else args.end
    total_s = int(args.days * 86400)
    start = end - dt.timedelta(seconds=total_s)
    scenario = plan_scenarios(total_s, args.seed + 1)
    print(f"window {start:%Y-%m-%d %H:%M:%S} .. {end:%Y-%m-%d %H:%M:%S} UTC ({args.days:g} days), seed {args.seed}")
    for a, b, why in scenario.outages:
        print(f"  outage   {start + dt.timedelta(seconds=a):%m-%d %H:%M:%S} for {(b - a) / 60:6.1f} min  ({why})")
    for s, kind in sorted(scenario.glitches.items()):
        print(f"  bad read {start + dt.timedelta(seconds=s):%m-%d %H:%M:%S}  {kind}")

    if args.dry_run:
        stats = next(m for m in generate(start.timestamp(), total_s, args.seed, scenario) if isinstance(m, dict))
        print("dry run:", json.dumps(stats))
        return

    replay = Replay()
    pool = await asyncpg.create_pool(replay.database_url, min_size=1, max_size=4)
    replay.store = HistoryStore(pool, lambda: replay.now)
    device = await pool.fetchrow("SELECT device_id, site, line, simulated FROM devices WHERE device_id = $1", args.device_id)
    if device is None:
        sys.exit(f"device {args.device_id} not found")
    if device["simulated"]:
        sys.exit("refusing: this is a simulated device")
    topic = f"plc/{device['site']}/{device['line']}/{args.device_id}/telemetry"
    if args.replace:
        print("removing existing data in the window")
        await delete_window(pool, args.device_id, start, end + dt.timedelta(hours=1))
    await replay.reload_config()
    dev = replay.devices[args.device_id]
    dev.online, dev.last_seen, dev.status, dev.run_since = False, None, None, None
    # only this device's history is replayed: keep other devices' alarms out of the engine
    replay.engine.load([r for r in replay.engine.rules if r.applies_to(args.device_id)], {})

    started = time.monotonic()
    next_housekeeping = start.timestamp()
    last_report = 0.0
    stats = {}
    for item in generate(start.timestamp(), total_s, args.seed, scenario):
        if isinstance(item, dict):
            stats = item
            break
        while next_housekeeping <= item.received_at:  # the live loop runs every 5 s
            replay.now = dt.datetime.fromtimestamp(next_housekeeping, dt.timezone.utc)
            await replay.housekeeping()
            next_housekeeping += live.HOUSEKEEPING_INTERVAL_S
        replay.now = dt.datetime.fromtimestamp(item.received_at, dt.timezone.utc)
        await replay.handle(topic, item.payload)
        if time.monotonic() - last_report > 15:
            last_report = time.monotonic()
            done = (item.received_at - start.timestamp()) / total_s
            print(f"  {done:6.1%}  {replay.now:%m-%d %H:%M}  {replay.stats['received']:,} messages", flush=True)
    await replay.flush()

    print("rebuilding 1-minute and 1-hour rollups")
    async with pool.acquire() as conn:
        a, b = start - dt.timedelta(hours=1), end + dt.timedelta(hours=1)
        await conn.execute("CALL refresh_continuous_aggregate('telemetry_1m', $1::timestamptz, $2::timestamptz)", a, b)
        await conn.execute("CALL refresh_continuous_aggregate('telemetry_1h', $1::timestamptz, $2::timestamptz)", a, b)
    alarms = await pool.fetch(
        "SELECT rule_name, severity, count(*) AS n FROM alarms WHERE device_id = $1 AND raised_at >= $2 "
        "GROUP BY 1, 2 ORDER BY 3 DESC", args.device_id, start,
    )
    await pool.close()
    print(f"done in {time.monotonic() - started:.0f} s: {json.dumps(stats)}")
    print(f"ingestor counters: {json.dumps(replay.stats)}")
    for r in alarms:
        print(f"  alarm {r['n']:>4} × {r['severity']:<8} {r['rule_name']}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("device_id")
    p.add_argument("--days", type=float, default=7)
    p.add_argument("--end", type=dt.datetime.fromisoformat, default=None, help="window end (UTC ISO time); default now")
    p.add_argument("--seed", type=int, default=12345, help="PLC seed (the guide's default start number)")
    p.add_argument("--replace", action="store_true", help="delete the device's data in the window first")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.end is not None and args.end.tzinfo is None:
        args.end = args.end.replace(tzinfo=dt.timezone.utc)
    if not args.dry_run and os.environ.get("HISTORY_CONFIRM") != "1" and sys.stdin.isatty():
        if input("The live ingestor must be stopped. Continue? [y/N] ").strip().lower() != "y":
            sys.exit(1)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
