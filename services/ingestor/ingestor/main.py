"""Telemetry ingestor: MQTT (plc/#) -> validation -> TimescaleDB, live events and alarms."""

import asyncio
import datetime as dt
import json
import logging
import os
import time
from pathlib import Path

import aiomqtt
import asyncpg
import httpx

from .alarms import AlarmEngine, Transition, format_message
from .parsing import (
    InvalidMessage,
    TagConfig,
    Telemetry,
    SeqTracker,
    Topic,
    apply_tag_config,
    decode_status,
    decode_telemetry,
    load_validator,
    parse_topic,
)
from .store import DeviceState, DeviceUpdate, Store

log = logging.getLogger("ingestor")

FLUSH_INTERVAL_S = 0.5
FLUSH_MAX_ROWS = 1000
MAX_BUFFERED_ROWS = 200_000  # back-pressure guard while the database is unavailable
HOUSEKEEPING_INTERVAL_S = 5
STATS_INTERVAL_S = 10
CONFIG_RELOAD_INTERVAL_S = 60


def _default_schema_path() -> Path:
    # /srv/schemas in the container; <repo>/schemas when run from a checkout.
    here = Path(__file__).resolve()
    for base in (Path("/srv"), *here.parents):
        candidate = base / "schemas" / "telemetry.v1.json"
        if candidate.exists():
            return candidate
    raise FileNotFoundError("telemetry.v1.json not found; set SCHEMA_PATH")


MAX_RAW_PAYLOAD = 4000


def _short(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Ingestor:
    def __init__(self) -> None:
        self.database_url = os.environ.get("DATABASE_URL", "postgresql://plc:plc@localhost:5432/plc")
        self.mqtt_host = os.environ.get("MQTT_HOST", "localhost")
        self.mqtt_port = int(os.environ.get("MQTT_PORT", "1883"))
        self.mqtt_password = os.environ.get("INGESTOR_MQTT_PASSWORD", "")
        self.validator = load_validator(Path(os.environ.get("SCHEMA_PATH") or _default_schema_path()))

        self.store: Store | None = None
        self.client: aiomqtt.Client | None = None
        self.engine = AlarmEngine()
        self.seq = SeqTracker()
        self.devices: dict[str, DeviceState] = {}
        self.tag_cfg: dict[tuple[str, str], TagConfig] = {}
        self.config_changed = asyncio.Event()
        self.http = httpx.AsyncClient(timeout=5)
        self._background: set[asyncio.Task] = set()

        # write buffers (swapped atomically by flush)
        self.rows: list[tuple] = []
        self.latest: dict[tuple[str, str], tuple] = {}
        self.dev_updates: dict[str, DeviceUpdate] = {}
        self.raw: list[tuple] = []  # (ts, device_id, topic, payload, status, detail) for the Raw data view

        self.started = time.monotonic()
        self.stats = dict(received=0, invalid=0, duplicates=0, rows_written=0, write_errors=0, dropped_rows=0)
        self._last_stats = (time.monotonic(), 0, 0)
        self.last_flush_ms = 0.0

    # ---------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        pool = await self._connect_db()
        self.store = Store(pool)
        listener = await asyncpg.connect(self.database_url)
        await listener.add_listener("config_changed", lambda *_: self.config_changed.set())
        await self.reload_config()
        log.info("loaded %d devices, %d tags, %d alarm rules", len(self.devices), len(self.tag_cfg), len(self.engine.rules))
        await asyncio.gather(self.mqtt_loop(), self.flush_loop(), self.housekeeping_loop(), self.config_loop())

    async def _connect_db(self) -> asyncpg.Pool:
        while True:
            try:
                return await asyncpg.create_pool(self.database_url, min_size=1, max_size=4)
            except (OSError, asyncpg.PostgresError) as exc:
                log.warning("database not ready: %s", exc)
                await asyncio.sleep(2)

    async def reload_config(self) -> None:
        fresh = await self.store.load_devices()
        for device_id, state in fresh.items():
            current = self.devices.get(device_id)
            if current:  # keep live runtime state, refresh admin-controlled fields
                current.expected_interval_s = state.expected_interval_s
                current.enabled = state.enabled
                current.simulated = state.simulated
            else:
                self.devices[device_id] = state
        for device_id in set(self.devices) - set(fresh):
            del self.devices[device_id]
            self.seq.reset(device_id)
        self.tag_cfg = await self.store.load_tag_configs()
        self.engine.load(await self.store.load_rules(), await self.store.load_active_alarms())

    async def config_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self.config_changed.wait(), CONFIG_RELOAD_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
            self.config_changed.clear()
            try:
                await self.reload_config()
            except Exception:
                log.exception("config reload failed")

    # ---------------------------------------------------------------- MQTT

    async def mqtt_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.mqtt_host,
                    port=self.mqtt_port,
                    username="ingestor",
                    password=self.mqtt_password,
                    identifier="ingestor",
                    clean_session=False,  # broker queues messages while we restart
                    keepalive=30,
                ) as client:
                    for root in ("plc", "sim"):
                        await client.subscribe(f"{root}/+/+/+/telemetry", qos=1)
                        await client.subscribe(f"{root}/+/+/+/status", qos=1)
                    self.client = client
                    backoff = 1.0
                    log.info("connected to broker %s:%s", self.mqtt_host, self.mqtt_port)
                    async for message in client.messages:
                        try:
                            await self.handle(message.topic.value, message.payload)
                        except Exception:
                            log.exception("failed to process message on %s", message.topic.value)
            except aiomqtt.MqttError as exc:
                log.warning("broker connection error: %s (retry in %.0fs)", exc, backoff)
            self.client = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def publish(self, topic: str, payload: dict, retain: bool = False) -> None:
        if self.client is None:
            return
        try:
            await self.client.publish(topic, json.dumps(payload, default=str), qos=0, retain=retain)
        except aiomqtt.MqttError as exc:
            log.warning("publish to %s failed: %s", topic, exc)

    async def handle(self, topic_str: str, payload: bytes) -> None:
        topic = parse_topic(topic_str)
        if topic is None:
            return
        if topic.kind == "status":
            await self.handle_status(topic, payload)
        else:
            await self.handle_telemetry(topic_str, topic, payload)

    async def handle_status(self, topic: Topic, payload: bytes) -> None:
        online = decode_status(payload)
        dev = self.devices.get(topic.device_id)
        if online:
            # A (re)connecting PLC may restart its seq counter. Liveness itself is confirmed by telemetry.
            self.seq.reset(topic.device_id)
        elif online is False and dev is not None and dev.online:
            await self.mark_offline(dev, reason="last will")

    async def handle_telemetry(self, topic_str: str, topic: Topic, payload: bytes) -> None:
        received_at = utcnow()
        self.stats["received"] += 1

        def keep_raw(status: str, detail: str = "") -> None:
            text = payload[:MAX_RAW_PAYLOAD].decode(errors="replace")
            self.raw.append((received_at, topic.device_id, topic_str, text, status, detail[:500]))

        try:
            msg = decode_telemetry(topic, payload, received_at, self.validator)
        except InvalidMessage as exc:
            self.stats["invalid"] += 1
            log.info("rejected message on %s: %s", topic_str, exc)
            keep_raw("rejected", str(exc))
            await self.store.record_error(topic_str, topic.device_id, str(exc), payload)
            return

        dev = self.devices.get(msg.device_id)
        if dev is None:
            dev = self.devices[msg.device_id] = await self.store.register_device(topic)
            log.info("auto-registered device %s", msg.device_id)
        elif not dev.enabled:
            keep_raw("ignored", "device is disabled")
            return
        if dev.simulated != topic.simulated:
            reason = (
                f"'{msg.device_id}' is a real PLC; the simulator cannot publish for it"
                if topic.simulated
                else f"'{msg.device_id}' is a simulated device; real PLC topics cannot be used for it"
            )
            self.stats["invalid"] += 1
            keep_raw("rejected", reason)
            return

        duplicate, gap = self.seq.observe(msg.device_id, msg.seq)
        if duplicate:
            self.stats["duplicates"] += 1
            keep_raw("duplicate", f"seq {msg.seq} already received")
            return

        new = [r for r in msg.readings if (msg.device_id, r.tag) not in self.tag_cfg]
        if new:
            await self.store.register_tags(msg.device_id, new)
            for r in new:
                self.tag_cfg[(msg.device_id, r.tag)] = TagConfig(data_type=r.data_type)

        readings = [apply_tag_config(r, self.tag_cfg.get((msg.device_id, r.tag))) for r in msg.readings]
        self.buffer(msg, readings, received_at, gap)
        keep_raw("ok", f"{msg.format} format → " + ", ".join(f"{r.tag}={_short(r.display_value)}" for r in readings))

        was_offline = not dev.online
        dev.online, dev.last_seen = True, received_at
        dev.status = msg.status or dev.status

        await self.publish(
            f"app/live/{msg.device_id}",
            {
                "type": "live",
                "device_id": msg.device_id,
                "ts": msg.ts.isoformat(),
                "seq": msg.seq,
                "status": dev.status,
                "values": {r.tag: {"v": r.display_value, "q": r.quality} for r in readings},
            },
        )
        transitions = self.engine.on_readings(msg.device_id, readings, msg.status)
        if was_offline:
            await self.publish_status(dev)
            transitions += self.engine.on_offline_time(msg.device_id, 0)
        for tr in transitions:
            await self.apply_transition(tr)

        if len(self.rows) >= FLUSH_MAX_ROWS:
            await self.flush()

    def buffer(self, msg: Telemetry, readings: list, received_at: dt.datetime, gap: int) -> None:
        for r in readings:
            self.rows.append((msg.ts, msg.device_id, r.tag, r.value_num, r.value_text, r.quality))
            key = (msg.device_id, r.tag)
            prev = self.latest.get(key)
            if prev is None or prev[0] <= msg.ts:
                self.latest[key] = (msg.ts, r.value_num, r.value_text, r.quality)
        upd = self.dev_updates.setdefault(msg.device_id, DeviceUpdate(last_seen=received_at))
        upd.last_seen = received_at
        upd.status = msg.status or upd.status
        upd.msgs += 1
        upd.last_seq = msg.seq if msg.seq is not None else upd.last_seq
        upd.gaps += gap

    # ---------------------------------------------------------------- persistence

    async def flush(self) -> None:
        if self.raw:
            raw, self.raw = self.raw, []
            try:
                await self.store.write_raw(raw)
            except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
                log.warning("raw message write failed (%d dropped): %s", len(raw), exc)  # debug data only
        if not (self.rows or self.dev_updates):
            return
        rows, latest, updates = self.rows, self.latest, self.dev_updates
        self.rows, self.latest, self.dev_updates = [], {}, {}
        online = {d: self.devices[d].online for d in updates if d in self.devices}
        started = time.perf_counter()
        try:
            self.stats["rows_written"] += await self.store.write_batch(rows, latest, updates, online)
        except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
            self.stats["write_errors"] += 1
            log.error("batch write failed (%d rows), will retry: %s", len(rows), exc)
            self._requeue(rows, latest, updates)
            return
        self.last_flush_ms = (time.perf_counter() - started) * 1000

    def _requeue(self, rows, latest, updates) -> None:
        self.rows = rows + self.rows
        overflow = len(self.rows) - MAX_BUFFERED_ROWS
        if overflow > 0:
            self.stats["dropped_rows"] += overflow
            del self.rows[:overflow]
        for key, value in latest.items():
            if key not in self.latest or self.latest[key][0] <= value[0]:
                self.latest[key] = value
        for device_id, upd in updates.items():
            newer = self.dev_updates.get(device_id)
            if newer:
                newer.msgs += upd.msgs
                newer.gaps += upd.gaps
            else:
                self.dev_updates[device_id] = upd

    async def flush_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_S)
            try:
                await self.flush()
            except Exception:
                log.exception("flush failed")

    # ---------------------------------------------------------------- liveness, alarms, stats

    async def mark_offline(self, dev: DeviceState, reason: str) -> None:
        dev.online = False
        log.info("device %s offline (%s)", dev.device_id, reason)
        await self.store.set_online(dev.device_id, False)
        await self.publish_status(dev)

    async def publish_status(self, dev: DeviceState) -> None:
        await self.publish(
            f"app/status/{dev.device_id}",
            {
                "type": "status",
                "device_id": dev.device_id,
                "online": dev.online,
                "status": dev.status,
                "last_seen": dev.last_seen.isoformat() if dev.last_seen else None,
            },
        )

    async def housekeeping_loop(self) -> None:
        last_stats = 0.0
        while True:
            await asyncio.sleep(HOUSEKEEPING_INTERVAL_S)
            try:
                now = utcnow()
                for dev in list(self.devices.values()):
                    if dev.last_seen is None or not dev.enabled:
                        continue
                    silent_s = (now - dev.last_seen).total_seconds()
                    if dev.online and silent_s > dev.offline_after_s:
                        await self.mark_offline(dev, reason=f"no data for {silent_s:.0f}s")
                    if not dev.online:
                        for tr in self.engine.on_offline_time(dev.device_id, silent_s):
                            await self.apply_transition(tr)
                if time.monotonic() - last_stats >= STATS_INTERVAL_S:
                    last_stats = time.monotonic()
                    await self.publish_stats()
            except Exception:
                log.exception("housekeeping failed")

    async def apply_transition(self, tr: Transition) -> None:
        if tr.action == "raise":
            message = format_message(tr.rule, tr.device_id, tr.value)
            alarm, created = await self.store.raise_alarm(tr.rule, tr.device_id, message, tr.value)
            self.engine.mark_raised(tr.rule.id, tr.device_id, alarm["id"])
            if created:
                log.info("alarm raised: %s", message)
                await self.publish("app/alarms", {"type": "alarm", "event": "raised", "alarm": alarm})
                if tr.rule.webhook_url:
                    self._spawn(self.send_webhook(tr.rule.webhook_url, alarm))
        else:
            alarm_id = self.engine.mark_cleared(tr.rule.id, tr.device_id)
            if alarm_id is None:
                return
            alarm = await self.store.clear_alarm(alarm_id)
            if alarm:
                log.info("alarm cleared: %s", alarm["message"])
                await self.publish("app/alarms", {"type": "alarm", "event": "cleared", "alarm": alarm})

    async def send_webhook(self, url: str, alarm: dict) -> None:
        text = f"[{alarm['severity'].upper()}] {alarm['message']} (device {alarm['device_id']})"
        try:
            resp = await self.http.post(url, json={"text": text, "alarm": alarm})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("webhook %s failed: %s", url, exc)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def publish_stats(self) -> None:
        now = time.monotonic()
        prev_t, prev_received, prev_rows = self._last_stats
        elapsed = max(now - prev_t, 1e-6)
        self._last_stats = (now, self.stats["received"], self.stats["rows_written"])
        await self.publish(
            "app/stats",
            {
                "type": "stats",
                "ts": utcnow().isoformat(),
                "uptime_s": round(now - self.started),
                **{f"{k}_total": v for k, v in self.stats.items()},
                "msg_rate": round((self.stats["received"] - prev_received) / elapsed, 2),
                "row_rate": round((self.stats["rows_written"] - prev_rows) / elapsed, 2),
                "buffered_rows": len(self.rows),
                "last_flush_ms": round(self.last_flush_ms, 1),
                "devices_online": sum(1 for d in self.devices.values() if d.online),
                "devices_total": len(self.devices),
            },
            retain=True,
        )


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {"severity": record.levelname, "logger": record.name, "message": record.getMessage(),
                 "time": self.formatTime(record)}
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def main() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    asyncio.run(Ingestor().run())


if __name__ == "__main__":
    main()
