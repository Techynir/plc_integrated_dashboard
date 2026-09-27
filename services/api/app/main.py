import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import db
from .broker import link
from .routers import alarms, auth, devices, logs, stream, system, users


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "time": self.formatTime(record),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    await db.connect()
    link.start()
    try:
        yield
    finally:
        await link.stop()
        await db.close()


app = FastAPI(
    title="PLC Dashboard API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
)

for module in (auth, devices, alarms, users, system, logs, stream):
    app.include_router(module.router, prefix="/api/v1")


@app.get("/api/healthz", include_in_schema=False)
async def healthz() -> dict:
    db_ok = await db.pool().fetchval("SELECT 1") == 1
    return {"ok": db_ok, "db": db_ok, "broker": link.connected}
