"""Always-on PM-01 simulator: publishes the gateway's messages once a second, like the real gateway.

Runs as the pm01-sim service. It continues the same model run as the stored history (pm01.py):
on start it loads the checkpoint the history (or its own last run) saved, catches up with the
clock without publishing, and from then on publishes every poll on the device's real topic, so the
data goes through the normal broker -> ingestor path. No data during the weekly scheduled
maintenance hour; bad reads 3-5 times a day, as the real gateway would send them.

Settings (environment):
    SIM_ENABLED          "0" to keep the service idle (e.g. when the real gateway is connected)
    SIM_TOPIC            plc/<site>/<line>/<device>/telemetry
    SIM_CHECKPOINT       model checkpoint file (a volume, so restarts continue the same run)
    SIMULATOR_MQTT_PASSWORD, MQTT_HOST, MQTT_PORT   broker login (the "simulator" account)
"""

import asyncio
import logging
import os
import random
import signal
import time

import aiomqtt
import asyncpg

from .pm01 import LOGGED_SHARE, REASONS, SEED, Plant

log = logging.getLogger("pm01-sim")

CHECKPOINT_EVERY_S = 10  # and on shutdown: a restart continues exactly where it stopped
LOG_DELAY_S = (180, 1800)  # the operator logs a stop's reason a few minutes after it starts
SEND_DELAY_S = 0.05  # the gateway publishes a poll about 50 ms after reading it (history uses 20-120 ms)
MAX_BACKLOG_S = 300  # after a pause up to this long the missed polls are still sent, like the gateway's
#                      buffer (the ingestor gives each its own second); longer pauses are skipped


class OperatorLog:
    """What the operators would type in: a reason for most stops, a little while after each starts. The
    reason is keyed by the stop's first stopped record, exactly as the Performance screen finds stops,
    and never replaces a reason someone already chose."""

    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self.pool: asyncpg.Pool | None = None
        self.pending: list[tuple[float, int, str]] = []  # (log at, stop start second, kind)
        self.seen: int | None = None

    async def open(self) -> None:
        url = os.environ.get("DATABASE_URL")
        if url:
            try:
                self.pool = await asyncpg.create_pool(url, min_size=1, max_size=1)
            except (OSError, asyncpg.PostgresError) as exc:
                log.warning("operator log disabled (database: %s)", exc)

    def note(self, plant: Plant) -> None:
        last = plant.plc.last_stop
        if not last or last[0] == self.seen:
            return
        self.seen = last[0]
        pick = random.Random(SEED * 13 + last[0])
        if pick.random() < LOGGED_SHARE:
            self.pending.append((last[0] + pick.uniform(*LOG_DELAY_S), last[0], last[1]))

    async def flush(self) -> None:
        if not self.pool or not self.pending:
            return
        now = time.time()
        due = [p for p in self.pending if p[0] <= now]
        self.pending = [p for p in self.pending if p[0] > now]
        for _at, t0, kind in due:
            try:
                started = await self.pool.fetchval(
                    "SELECT ts FROM telemetry t JOIN tags g ON g.device_id = t.device_id AND g.tag = t.tag "
                    "WHERE t.device_id = $1 AND g.role = 'machine_status' AND t.value_num = 0 "
                    "AND t.ts >= to_timestamp($2 - 2) AND t.ts < to_timestamp($2 + 30) ORDER BY t.ts LIMIT 1",
                    self.device_id, float(t0),
                )
                if started is not None:  # no record: the stop fell in a data gap, nothing to classify
                    await self.pool.execute(
                        "INSERT INTO stoppage_reasons (device_id, started_at, reason, set_by) VALUES ($1, $2, $3, 'operator log') "
                        "ON CONFLICT DO NOTHING", self.device_id, started, REASONS[kind],
                    )
            except (OSError, asyncpg.PostgresError) as exc:
                log.warning("operator log: %s", exc)


def load_plant(path: str) -> Plant:
    plant = Plant.load(path)
    if plant is None:
        log.info("no checkpoint at %s: starting the model from its anchor", path)
        plant = Plant()
    return plant


async def run() -> None:
    if os.environ.get("SIM_ENABLED", "1") == "0":
        log.info("SIM_ENABLED=0: idle")
        await asyncio.Event().wait()
    topic = os.environ.get("SIM_TOPIC", "plc/Bijnor/Line1/conveyer-plc-line-01/telemetry")
    path = os.environ.get("SIM_CHECKPOINT", "/simstate/pm01.pkl")
    plant = load_plant(path)
    behind = time.time() - plant.t
    if behind > MAX_BACKLOG_S:
        log.info("model is %.0f s behind the clock: catching up", behind)
        plant.advance_to(int(time.time()))
    last_save = time.monotonic()
    backoff = 1.0
    operator = OperatorLog(topic.split("/")[3] if topic.count("/") >= 4 else "conveyer-plc-line-01")
    await operator.open()
    operator.seen = plant.plc.last_stop[0] if plant.plc.last_stop else None  # earlier stops are history's
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    while not stop.is_set():
        try:
            async with aiomqtt.Client(
                hostname=os.environ.get("MQTT_HOST", "localhost"),
                port=int(os.environ.get("MQTT_PORT", "1883")),
                username="simulator",
                password=os.environ.get("SIMULATOR_MQTT_PASSWORD", ""),
                identifier="pm01-sim",
                keepalive=30,
            ) as client:
                log.info("publishing PM-01 to %s once a second", topic)
                backoff = 1.0
                while not stop.is_set():
                    now = time.time()
                    if now - plant.t > MAX_BACKLOG_S:  # e.g. the host was suspended
                        plant.advance_to(int(now))
                    while plant.t <= now - SEND_DELAY_S:  # each poll as soon as its second starts
                        _t, _regs, payloads = plant.poll()
                        for payload in payloads:
                            await client.publish(topic, payload, qos=1)
                        operator.note(plant)
                    await operator.flush()
                    if time.monotonic() - last_save > CHECKPOINT_EVERY_S:
                        await asyncio.to_thread(plant.save, path)
                        last_save = time.monotonic()
                    try:
                        await asyncio.wait_for(stop.wait(), max(0.02, plant.t + SEND_DELAY_S - time.time()))
                    except asyncio.TimeoutError:
                        pass
        except aiomqtt.MqttError as exc:
            log.warning("broker connection error: %s (retry in %.0fs)", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
    plant.save(path)
    log.info("stopped; model saved at %s", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(plant.t)))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
