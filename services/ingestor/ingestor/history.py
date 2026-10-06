"""Store PM-01 history as if the real PLC had sent it, then hand over to the live simulator.

    docker compose stop ingestor pm01-sim
    docker compose run --rm --no-deps -e HISTORY_CONFIRM=1 pm01-sim \
      python -m ingestor.history conveyer-plc-line-01 --days 7 --replace --follow
    docker compose up -d ingestor pm01-sim

The model and its gateway live in pm01.py (one continuous run from a fixed start, so history and
live data join without a seam). Every message goes through the live ingestor's own handler
(decoding, bad-read rejection, status, raw data, limit and process-rule alarms with their delays
and suppression, offline detection) with a simulated clock, so stored values, raw messages and
alarms carry historical times.

--follow keeps going past the window end until it has caught up with the clock, then saves the
model to the live simulator's checkpoint (--checkpoint, default $SIM_CHECKPOINT): started right
after, the live simulator continues from the last stored second.

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
import sys
import time
from dataclasses import dataclass

import asyncpg

from . import main as live
from .bursts import QUIET_S
from .main import Ingestor
from .pm01 import (  # noqa: F401  (re-exported for tests and tools)
    PM01,
    SEED,
    VIB_NEW,
    Plant,
    Registers,
    bearing_baseline,
    d1_payload,
    d2_payload,
    f32,
    in_maintenance,
)
from .store import Store


@dataclass
class Message:
    received_at: float  # epoch seconds
    payload: bytes


def generate(plant: Plant, start: int, end: int, follow: bool = False, jitter_seed: int = SEED + 7):
    """Gateway messages from `start` to `end` (epoch seconds) in arrival order, then a stats dict.
    The plant is first run up to `start`. With follow, `end` moves with the clock until caught up."""
    plant.advance_to(start)
    plc = plant.plc
    stops0, events0 = len(plc.stops), {n: len(e.events) for n, e in _events(plc)}
    jitter = random.Random(jitter_seed)
    stats = {"polls": 0, "maintenance_s": 0, "bad_reads": 0, "messages": 0}
    while True:
        if plant.t >= end:
            if not follow or plant.t >= time.time() - 1:
                break
            end = int(time.time())
        t, _regs, payloads = plant.poll()
        if not payloads:
            stats["maintenance_s"] += 1
            continue
        stats["polls"] += 1
        stats["bad_reads"] += plant.glitch(t) is not None
        at = t + jitter.uniform(0.02, 0.12)  # the gateway publishes each poll as soon as it has read it
        for k, payload in enumerate(payloads):
            yield Message(at + k * jitter.uniform(0.0009, 0.0013), payload)
            stats["messages"] += 1
    stats["end"] = plant.t
    causes = list(plc.causes)[len(plc.causes) - (len(plc.stops) - stops0):] if len(plc.stops) > stops0 else []
    stats["stops"] = len(causes)
    stats["stop_causes"] = {c: causes.count(c) for c in sorted(set(causes))}
    stats["events"] = {
        name: {k: sum(1 for kind, _ in list(ev.events)[events0[name]:] if kind == k) for k in ("warning", "critical")}
        for name, ev in _events(plc)
    }
    yield stats


def _events(plc: PM01):
    return (("speed", plc.ev_speed), ("steam", plc.ev_steam), ("current", plc.ev_current),
            ("vibration", plc.ev_vibration))


# ---------------------------------------------------------------- replay through the live ingestor


class HistoryStore(Store):
    """Store whose alarm and error times come from the replay clock instead of now()."""

    def __init__(self, pool: asyncpg.Pool, clock) -> None:
        super().__init__(pool)
        self.clock = clock

    async def raise_alarm(self, rule, device_id, message, value, explain=None):
        row = await self.pool.fetchrow(
            f"""
            INSERT INTO alarms (rule_id, rule_name, device_id, tag, severity, message, trigger_value, context, raised_at,
                                explain)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
            ON CONFLICT (rule_id, device_id) WHERE cleared_at IS NULL DO NOTHING
            RETURNING {self.ALARM_COLUMNS}
            """,
            rule.id, rule.name, device_id, rule.tag, rule.severity, message, value, rule.guidance, self.clock(),
            json.dumps(explain) if explain else None,
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

    async def process_burst(self, burst: list) -> None:
        """Live, a burst is released QUIET_S after its last message; alarms raised then carry that time."""
        now = self.now
        self.now = burst[-1][0].received_at + dt.timedelta(seconds=QUIET_S)
        try:
            await super().process_burst(burst)
        finally:
            self.now = now

    async def housekeeping(self) -> None:
        """One pass of Ingestor.housekeeping_loop at the replay time (offline detection)."""
        await self.check_liveness(self.now)


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
    start = end - dt.timedelta(seconds=int(args.days * 86400))
    plant = Plant(seed=args.seed)
    if start.timestamp() < plant.t:
        sys.exit(f"the model starts at {dt.datetime.fromtimestamp(plant.t, dt.timezone.utc):%Y-%m-%d}; use fewer --days")
    s0, e0 = int(start.timestamp()), int(end.timestamp())
    print(f"window {start:%Y-%m-%d %H:%M:%S} .. {end:%Y-%m-%d %H:%M:%S} UTC ({args.days:g} days), seed {args.seed}"
          f"{', then following the clock' if args.follow else ''}")

    if args.dry_run:
        stats = next(m for m in generate(plant, s0, e0) if isinstance(m, dict))
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
        await delete_window(pool, args.device_id, start, end + dt.timedelta(hours=6))
    await replay.reload_config()
    dev = replay.devices[args.device_id]
    dev.online, dev.last_seen, dev.status, dev.run_since = False, None, None, None
    # only this device's history is replayed: keep other devices' alarms out of the engine
    replay.engine.load([r for r in replay.engine.rules if r.applies_to(args.device_id)], {})

    started = time.monotonic()
    clock = {"next_housekeeping": float(s0), "last_report": 0.0}

    async def replay_all(items) -> dict:
        for item in items:
            if isinstance(item, dict):
                return item
            while clock["next_housekeeping"] <= item.received_at:  # the live loop runs every 5 s
                replay.now = dt.datetime.fromtimestamp(clock["next_housekeeping"], dt.timezone.utc)
                await replay.release_bursts(replay.now)
                await replay.housekeeping()
                clock["next_housekeeping"] += live.HOUSEKEEPING_INTERVAL_S
            replay.now = dt.datetime.fromtimestamp(item.received_at, dt.timezone.utc)
            await replay.handle(topic, item.payload)  # releases earlier bursts, then holds this message
            if time.monotonic() - clock["last_report"] > 15:
                clock["last_report"] = time.monotonic()
                behind = time.time() - item.received_at
                print(f"  {replay.now:%m-%d %H:%M}  {replay.stats['received']:,} messages  "
                      f"({behind / 60:,.0f} min behind the clock)", flush=True)
        return {}

    stats = await replay_all(generate(plant, s0, e0, follow=args.follow))
    await replay.release_bursts(replay.now, force=True)
    await replay.flush()

    print("rebuilding 1-minute and 1-hour rollups")
    async with pool.acquire() as conn:
        a, b = start - dt.timedelta(hours=1), dt.datetime.fromtimestamp(plant.t, dt.timezone.utc) + dt.timedelta(hours=1)
        await conn.execute("CALL refresh_continuous_aggregate('telemetry_1m', $1::timestamptz, $2::timestamptz)", a, b)
        await conn.execute("CALL refresh_continuous_aggregate('telemetry_1h', $1::timestamptz, $2::timestamptz)", a, b)
    if args.follow:  # the refresh took a while: catch up again, then hand over at once
        await replay_all(generate(plant, plant.t, plant.t, follow=True))
        await replay.release_bursts(replay.now, force=True)
        await replay.flush()
        plant.save(args.checkpoint)
        print(f"caught up at {dt.datetime.fromtimestamp(plant.t, dt.timezone.utc):%Y-%m-%d %H:%M:%S} UTC; "
              f"model saved to {args.checkpoint}: start the ingestor and pm01-sim now", flush=True)
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
    p.add_argument("--seed", type=int, default=SEED, help="PLC seed (the guide's default start number)")
    p.add_argument("--replace", action="store_true", help="delete the device's data in the window first")
    p.add_argument("--follow", action="store_true", help="continue until caught up with the clock, then save the model")
    p.add_argument("--checkpoint", default=os.environ.get("SIM_CHECKPOINT", "/simstate/pm01.pkl"),
                   help="where --follow saves the model for the live simulator")
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
