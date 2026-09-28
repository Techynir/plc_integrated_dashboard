"""Web simulator: an admin-only test site that publishes messages to the broker.

    uvicorn app.sim.main:app

The admin chooses the topic. The ``simulator`` MQTT account may publish under ``sim/``
(simulated PLCs: flagged, badged on the dashboard, purged once idle), ``plc/`` (acts
exactly like a real PLC, for end-to-end tests) or ``test/`` (ignored by the dashboard,
for other MQTT consumers).
"""

import asyncio
import datetime as dt
import itertools
import logging
import re
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import aiomqtt
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import db
from ..config import settings
from ..deps import CurrentUser, user_from_cookie
from ..main import configure_logging
from ..security import SESSION_COOKIE, clear_session_cookie, role_allows
from .templating import Renderer, TemplateError, validate

log = logging.getLogger("simulator")

ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
DEFAULT_TOPIC = "sim/{{site}}/{{line}}/{{device}}/telemetry"
TOPIC_ROOTS = ("sim", "plc", "test")
DEVICE_TOPIC = re.compile(r"^(sim|plc)/([A-Za-z0-9_-]{1,64})/([A-Za-z0-9_-]{1,64})/([A-Za-z0-9_-]{1,64})/(telemetry|status)$")
MAX_RUNNING_JOBS = 10
MAX_RATE_PER_S = 200  # across one job
CONTINUOUS_LIMIT_S = 24 * 3600
STATIC = Path(__file__).parent / "static"


# ---------------------------------------------------------------- MQTT publisher


class Publisher:
    def __init__(self) -> None:
        self.client: aiomqtt.Client | None = None
        self.task: asyncio.Task | None = None
        self.ready = asyncio.Event()

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=settings.mqtt_host,
                    port=settings.mqtt_port,
                    username="simulator",
                    password=settings.simulator_mqtt_password,
                    identifier=f"web-simulator-{uuid.uuid4().hex[:6]}",
                    keepalive=30,
                ) as client:
                    self.client = client
                    self.ready.set()
                    backoff = 1.0
                    log.info("connected to broker")
                    # Nothing is subscribed, but iterating the message stream is what raises
                    # MqttError when the broker drops the connection, so we notice and reconnect.
                    async for _ in client.messages:
                        pass
            except aiomqtt.MqttError as exc:
                log.warning("broker connection error: %s (retry in %.0fs)", exc, backoff)
            finally:
                self.client = None
                self.ready.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def publish(self, topic: str, payload: str) -> None:
        try:
            await asyncio.wait_for(self.ready.wait(), 5)
        except asyncio.TimeoutError as exc:
            raise HTTPException(503, "Simulator is not connected to the broker") from exc
        try:
            await self.client.publish(topic, payload, qos=1)
        except (aiomqtt.MqttError, AttributeError) as exc:  # AttributeError: client dropped meanwhile
            self.ready.clear()
            raise HTTPException(503, "Lost the broker connection; reconnecting — try again in a few seconds") from exc


publisher = Publisher()
sent_log: deque = deque(maxlen=100)


# ---------------------------------------------------------------- jobs


@dataclass
class Job:
    id: int
    user: str
    site: str
    line: str
    device_ids: list[str]
    topics: list[str]  # one per device
    template: str
    count: int  # messages per device; 0 = until stopped
    interval_ms: int
    started: float = field(default_factory=time.time)
    sent: int = 0
    status: str = "running"  # running | done | stopped | error
    error: str = ""
    task: asyncio.Task | None = None

    def public(self) -> dict:
        return {
            "id": self.id, "user": self.user, "site": self.site, "line": self.line,
            "device_ids": self.device_ids, "topics": self.topics, "template": self.template, "count": self.count,
            "interval_ms": self.interval_ms, "sent": self.sent, "status": self.status, "error": self.error,
            "started": dt.datetime.fromtimestamp(self.started, dt.timezone.utc).isoformat(),
            "total": self.count * len(self.device_ids) if self.count else None,
        }


jobs: dict[int, Job] = {}
_job_ids = itertools.count(1)


def running_devices() -> set[str]:
    return {d for j in jobs.values() if j.status == "running" for d in j.device_ids}


def render_topic(template: str, site: str, line: str, device_id: str) -> str:
    """Topic with {{site}}, {{line}}, {{device}} filled in; raises HTTPException if not allowed."""
    topic = template.strip()
    for key, value in (("site", site), ("line", line), ("device", device_id)):
        topic = re.sub(r"\{\{\s*" + key + r"\s*\}\}", value, topic)
    if "{{" in topic or "}}" in topic:
        raise HTTPException(400, "Topic placeholders: only {{site}}, {{line}} and {{device}} are supported")
    if not topic or len(topic) > 256 or any(c in topic for c in "+#\0") or topic.startswith("/"):
        raise HTTPException(400, "Topic must be 1-256 characters without wildcards (+, #) or a leading /")
    if topic.split("/")[0] not in TOPIC_ROOTS:
        raise HTTPException(400, "Topic must start with sim/, plc/ or test/")
    return topic


async def send_one(renderer: Renderer, topic: str) -> dict:
    payload = renderer.render()
    await publisher.publish(topic, payload)
    entry = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "topic": topic, "payload": payload}
    sent_log.appendleft(entry)
    return entry


async def run_job(job: Job) -> None:
    renderers = {d: Renderer(job.template, d, job.site, job.line) for d in job.device_ids}
    loop = asyncio.get_running_loop()
    next_at = loop.time()
    try:
        for i in itertools.count():
            if job.count and i >= job.count:
                break
            if not job.count and time.time() - job.started > CONTINUOUS_LIMIT_S:
                job.error = "stopped automatically after 24 hours"
                break
            for device_id, topic in zip(job.device_ids, job.topics):
                await send_one(renderers[device_id], topic)
                job.sent += 1
            next_at += job.interval_ms / 1000
            await asyncio.sleep(max(0.0, next_at - loop.time()))
        job.status = "done"
    except asyncio.CancelledError:
        job.status = "stopped"
        raise
    except Exception as exc:  # broker down, template error mid-run
        job.status, job.error = "error", str(exc)
        log.exception("job %s failed", job.id)


# ---------------------------------------------------------------- devices


async def register_devices(topics: list[str], interval_s: float) -> None:
    """sim/ device topics: pre-register the device as simulated (refused for real PLC IDs).
    plc/ device topics: refused for simulated IDs (the ingestor would reject them)."""
    async with db.pool().acquire() as conn, conn.transaction():
        for topic in topics:
            m = DEVICE_TOPIC.match(topic)
            if not m:
                continue  # test/... or a non-device plc/ path: nothing to register
            root, site, line, device_id, _ = m.groups()
            if root == "plc":
                if await conn.fetchval("SELECT simulated FROM devices WHERE device_id = $1", device_id):
                    raise HTTPException(409, f"'{device_id}' is a simulated device; use a sim/ topic for it")
                continue
            ok = await conn.fetchval(
                """
                INSERT INTO devices (device_id, name, site, line, simulated, expected_interval_s)
                VALUES ($1, $1, $2, $3, true, $4)
                ON CONFLICT (device_id) DO UPDATE SET
                    site = EXCLUDED.site, line = EXCLUDED.line, expected_interval_s = EXCLUDED.expected_interval_s
                WHERE devices.simulated
                RETURNING true
                """,
                device_id, site, line, interval_s,
            )
            if not ok:
                raise HTTPException(409, f"'{device_id}' is a real PLC. Choose another device ID.")
        await conn.execute("NOTIFY config_changed")


async def purge_devices(device_ids: list[str]) -> int:
    """Delete simulated devices and everything recorded for them."""
    if not device_ids:
        return 0
    async with db.pool().acquire() as conn, conn.transaction():
        ids = await conn.fetch(
            "SELECT device_id FROM devices WHERE simulated AND device_id = ANY($1)", device_ids
        )
        ids = [r["device_id"] for r in ids]
        if ids:
            for table in ("telemetry", "tag_latest", "raw_messages", "ingest_errors", "alarms"):
                await conn.execute(f"DELETE FROM {table} WHERE device_id = ANY($1)", ids)
            await conn.execute("DELETE FROM devices WHERE device_id = ANY($1)", ids)  # cascades tags, rules
            await conn.execute("NOTIFY config_changed")
    return len(ids)


async def cleanup_loop() -> None:
    """Remove simulated devices that have been idle longer than SIM_DEVICE_TTL_MIN."""
    while True:
        await asyncio.sleep(60)
        try:
            idle = await db.pool().fetch(
                "SELECT device_id FROM devices WHERE simulated "
                "AND coalesce(last_seen, created_at) < now() - make_interval(mins => $1)",
                settings.sim_device_ttl_min,
            )
            stale = [r["device_id"] for r in idle if r["device_id"] not in running_devices()]
            if stale:
                n = await purge_devices(stale)
                log.info("removed %d idle simulated devices: %s", n, ", ".join(stale))
        except Exception:
            log.exception("cleanup failed")


# ---------------------------------------------------------------- app


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    await db.connect()
    publisher.task = asyncio.create_task(publisher.run())
    cleanup = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        for job in jobs.values():
            if job.task:
                job.task.cancel()
        cleanup.cancel()
        publisher.task.cancel()
        await db.close()


app = FastAPI(title="PLC Simulator", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


async def operator_user(request: Request) -> CurrentUser:
    """Admin only: the simulator is a test tool that can publish as any PLC."""
    # Same session cookie as the dashboard (shared across <domain> and sim.<domain>).
    user = await user_from_cookie(request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise HTTPException(401, "Not authenticated")
    if not role_allows(user.role, "admin"):
        raise HTTPException(403, "The simulator is available to admins only")
    return user


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    # no-store: after sign-out, "Back" must not show a cached page.
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/healthz", include_in_schema=False)
async def healthz() -> dict:
    return {"ok": True, "broker": publisher.ready.is_set()}


@app.get("/api/config")
async def public_config() -> dict:
    """Unauthenticated: where to send the browser to sign in / after signing out."""
    return {"dashboard_url": settings.dashboard_url or None}


@app.post("/api/logout")
async def logout(response: Response) -> dict:
    """Signs out of the dashboard and the simulator (they share one session)."""
    clear_session_cookie(response)
    return {"ok": True}


@app.get("/api/me")
async def me(user: CurrentUser = Depends(operator_user)) -> dict:
    return {
        **user.as_dict(),
        "dashboard_url": settings.dashboard_url or None,
        "device_ttl_min": settings.sim_device_ttl_min,
        "broker_connected": publisher.ready.is_set(),
    }


class PreviewIn(BaseModel):
    template: str = Field(max_length=65536)


@app.post("/api/preview")
async def preview(body: PreviewIn, _: CurrentUser = Depends(operator_user)) -> dict:
    try:
        return {"payload": validate(body.template)}
    except TemplateError as exc:
        raise HTTPException(400, str(exc)) from exc


class SendIn(BaseModel):
    site: str = Field(pattern=ID_PATTERN)
    line: str = Field(pattern=ID_PATTERN)
    device_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,60}$")
    devices: int = Field(default=1, ge=1, le=20, description="simulate N devices: <id>-01 .. <id>-NN")
    template: str = Field(min_length=1, max_length=65536)
    topic: str = Field(default=DEFAULT_TOPIC, max_length=512,
                       description="may use {{site}}, {{line}}, {{device}}; must start with sim/, plc/ or test/")
    count: int = Field(default=1, ge=0, le=100_000, description="messages per device; 0 = until stopped")
    interval_ms: int = Field(default=1000, ge=100, le=3_600_000)


@app.post("/api/send")
async def send(body: SendIn, user: CurrentUser = Depends(operator_user)) -> dict:
    try:
        validate(body.template)
    except TemplateError as exc:
        raise HTTPException(400, str(exc)) from exc
    device_ids = (
        [body.device_id] if body.devices == 1 else [f"{body.device_id}-{i:02d}" for i in range(1, body.devices + 1)]
    )
    topics = [render_topic(body.topic, body.site, body.line, d) for d in device_ids]
    single = body.count == 1 and body.devices == 1
    if not single and body.devices * 1000 / body.interval_ms > MAX_RATE_PER_S:
        raise HTTPException(400, f"Too fast: at most {MAX_RATE_PER_S} messages per second per run")
    if not single and sum(j.status == "running" for j in jobs.values()) >= MAX_RUNNING_JOBS:
        raise HTTPException(429, f"At most {MAX_RUNNING_JOBS} runs at a time; stop one first")

    await register_devices(topics, max(1.0, body.interval_ms / 1000))

    if single:
        entry = await send_one(Renderer(body.template, body.device_id, body.site, body.line), topics[0])
        return {"sent": [entry]}

    job = Job(next(_job_ids), user.email, body.site, body.line, device_ids, topics, body.template,
              body.count, body.interval_ms)
    job.task = asyncio.create_task(run_job(job))
    jobs[job.id] = job
    for old in sorted((j for j in jobs.values() if j.status != "running"), key=lambda j: j.started)[:-30]:
        jobs.pop(old.id, None)  # keep the list short
    await db.audit(user.email, "sim.run", ",".join(topics)[:500], {"count": body.count, "interval_ms": body.interval_ms})
    return {"job": job.public()}


@app.get("/api/jobs")
async def list_jobs(_: CurrentUser = Depends(operator_user)) -> list[dict]:
    return [j.public() for j in sorted(jobs.values(), key=lambda j: -j.started)]


@app.delete("/api/jobs/{job_id}")
async def stop_job(job_id: int, _: CurrentUser = Depends(operator_user)) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Run not found")
    if job.task and not job.task.done():
        job.task.cancel()
        try:
            await job.task
        except asyncio.CancelledError:
            pass
    return job.public()


@app.get("/api/log")
async def recent(_: CurrentUser = Depends(operator_user)) -> list[dict]:
    return list(sent_log)[:50]


@app.get("/api/devices")
async def devices(_: CurrentUser = Depends(operator_user)) -> list[dict]:
    rows = await db.pool().fetch(
        "SELECT device_id, site, line, online, last_seen, msg_count, created_at FROM devices "
        "WHERE simulated ORDER BY site, line, device_id"
    )
    running = running_devices()
    return [
        {
            **dict(r),
            "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            "created_at": r["created_at"].isoformat(),
            "running": r["device_id"] in running,
        }
        for r in rows
    ]


async def _stop_jobs_for(device_ids: set[str]) -> None:
    for job in jobs.values():
        if job.status == "running" and device_ids & set(job.device_ids) and job.task:
            job.task.cancel()


@app.delete("/api/devices/{device_id}")
async def remove_device(device_id: str, user: CurrentUser = Depends(operator_user)) -> dict:
    await _stop_jobs_for({device_id})
    removed = await purge_devices([device_id])
    if not removed:
        raise HTTPException(404, "Simulated device not found")
    await db.audit(user.email, "sim.remove", device_id)
    return {"removed": removed}


@app.delete("/api/devices")
async def remove_all(user: CurrentUser = Depends(operator_user)) -> dict:
    ids = [r["device_id"] for r in await db.pool().fetch("SELECT device_id FROM devices WHERE simulated")]
    await _stop_jobs_for(set(ids))
    removed = await purge_devices(ids)
    await db.audit(user.email, "sim.remove_all", details={"removed": removed})
    return {"removed": removed}
