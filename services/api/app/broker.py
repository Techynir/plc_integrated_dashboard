"""MQTT side of the API.

* ``Hub`` fans ingestor events (``app/#``) out to connected WebSocket clients.
* ``BrokerLink`` owns the long-lived connection (as the "admin" superuser) and feeds the hub.

MQTT logins themselves live in PostgreSQL (see mqtt_accounts.py), not in the broker.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field

import aiomqtt

from .config import settings

log = logging.getLogger("api.broker")

SYS_TOPICS = {
    "$SYS/broker/clients/connected": "clients_connected",
    "$SYS/broker/load/messages/received/1min": "msgs_received_1min",
    "$SYS/broker/uptime": "uptime",
}


@dataclass(eq=False)
class Subscriber:
    devices: set[str] | None = None  # None = all devices
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=1000))
    dropped: int = 0

    def wants(self, device_id: str | None) -> bool:
        return device_id is None or self.devices is None or device_id in self.devices

    def offer(self, text: str) -> None:
        try:
            self.queue.put_nowait(text)
        except asyncio.QueueFull:
            self.dropped += 1


class Hub:
    def __init__(self) -> None:
        self.subscribers: set[Subscriber] = set()
        self.ingestor_stats: dict | None = None
        self.broker_sys: dict[str, str] = {}

    def add(self, sub: Subscriber) -> None:
        self.subscribers.add(sub)

    def remove(self, sub: Subscriber) -> None:
        self.subscribers.discard(sub)

    def broadcast(self, text: str, device_id: str | None) -> None:
        for sub in self.subscribers:
            if sub.wants(device_id):
                sub.offer(text)

    def handle_app_message(self, topic: str, payload: bytes) -> None:
        parts = topic.split("/")
        text = payload.decode()
        if parts[1] == "stats":
            self.ingestor_stats = json.loads(text)
        device_id = parts[2] if len(parts) > 2 and parts[1] in {"live", "status"} else None
        self.broadcast(text, device_id)


class BrokerLink:
    def __init__(self) -> None:
        self.hub = Hub()
        self.connected = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="broker-link")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=settings.mqtt_host,
                    port=settings.mqtt_port,
                    username=settings.mqtt_admin_user,
                    password=settings.mqtt_admin_password,
                    identifier=f"api-{uuid.uuid4().hex[:8]}",
                    keepalive=30,
                ) as client:
                    await client.subscribe("app/#", qos=0)
                    for topic in SYS_TOPICS:
                        await client.subscribe(topic, qos=0)
                    self.connected = True
                    backoff = 1.0
                    log.info("connected to broker %s:%s", settings.mqtt_host, settings.mqtt_port)
                    async for message in client.messages:
                        self._dispatch(message)
            except aiomqtt.MqttError as exc:
                log.warning("broker connection error: %s (retry in %.0fs)", exc, backoff)
            finally:
                self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def _dispatch(self, message: aiomqtt.Message) -> None:
        topic = message.topic.value
        payload = message.payload if isinstance(message.payload, bytes) else str(message.payload).encode()
        try:
            if topic.startswith("app/"):
                self.hub.handle_app_message(topic, payload)
            elif topic in SYS_TOPICS:
                self.hub.broker_sys[SYS_TOPICS[topic]] = payload.decode()
        except Exception:  # never let one bad message kill the link
            log.exception("failed to handle message on %s", topic)


link = BrokerLink()
