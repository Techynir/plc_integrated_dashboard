from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from .. import db
from ..broker import link
from ..config import settings
from ..deps import CurrentUser, admin, viewer

router = APIRouter(tags=["system"])


@router.get("/system/stats")
async def stats(_: CurrentUser = Depends(viewer)) -> dict:
    row = await db.pool().fetchrow(
        """
        SELECT
            (SELECT count(*) FROM devices) AS devices_total,
            (SELECT count(*) FROM devices WHERE online) AS devices_online,
            (SELECT count(*) FROM alarms WHERE cleared_at IS NULL) AS alarms_active,
            (SELECT count(*) FROM ingest_errors WHERE ts > now() - interval '1 hour') AS ingest_errors_1h,
            pg_database_size(current_database()) AS db_size_bytes,
            approximate_row_count('telemetry') AS telemetry_rows
        """
    )
    return {
        **dict(row),
        "ingestor": link.hub.ingestor_stats,
        "broker": {"connected": link.connected, **link.hub.broker_sys},
        "websocket_clients": len(link.hub.subscribers),
    }


@router.get("/system/ingest-errors")
async def ingest_errors(limit: int = Query(100, le=500), _: CurrentUser = Depends(admin)) -> list[dict]:
    rows = await db.pool().fetch(
        "SELECT ts, topic, device_id, reason, left(payload, 2000) AS payload "
        "FROM ingest_errors ORDER BY ts DESC LIMIT $1",
        limit,
    )
    return [{**dict(r), "ts": r["ts"].isoformat()} for r in rows]


@router.get("/raw-messages")
async def raw_messages(
    device_id: str | None = None,
    status: Literal["ok", "rejected", "duplicate", "ignored"] | None = None,
    limit: int = Query(100, ge=1, le=1000),
    _: CurrentUser = Depends(viewer),
) -> list[dict]:
    """Telemetry exactly as received over MQTT (last 7 days), newest first, with the parse outcome."""
    clauses, args = ["ts > now() - interval '7 days'"], []
    if device_id:
        args.append(device_id)
        clauses.append(f"device_id = ${len(args)}")
    if status:
        args.append(status)
        clauses.append(f"status = ${len(args)}")
    args.append(limit)
    rows = await db.pool().fetch(
        f"SELECT ts, device_id, topic, payload, status, detail FROM raw_messages "
        f"WHERE {' AND '.join(clauses)} ORDER BY ts DESC LIMIT ${len(args)}",
        *args,
    )
    return [{**dict(r), "ts": r["ts"].isoformat()} for r in rows]


@router.get("/system/audit")
async def audit_log(limit: int = Query(200, le=1000), _: CurrentUser = Depends(admin)) -> list[dict]:
    rows = await db.pool().fetch(
        "SELECT id, ts, actor, action, target, details FROM audit_log ORDER BY ts DESC LIMIT $1", limit
    )
    return [{**dict(r), "ts": r["ts"].isoformat()} for r in rows]


@router.get("/system/links")
async def links(user: CurrentUser = Depends(viewer)) -> dict:
    # The simulator is an admin-only test tool; others do not get its address.
    return {"simulator_url": (settings.simulator_url or None) if user.role == "admin" else None}


@router.get("/broker/info")
async def broker_info(_: CurrentUser = Depends(viewer)) -> dict:
    return {
        "host": settings.mqtt_public_host,
        "tls_port": settings.mqtt_public_tls_port,
        "topic_template": "plc/{site}/{line}/{device_id}/telemetry",
        "status_topic_template": "plc/{site}/{line}/{device_id}/status",
        "ca_available": (Path(settings.certs_dir) / "ca.crt").exists(),
    }


@router.get("/broker/ca.crt")
async def ca_certificate(_: CurrentUser = Depends(viewer)) -> FileResponse:
    path = Path(settings.certs_dir) / "ca.crt"
    if not path.exists():
        raise HTTPException(404, "CA certificate not generated")
    return FileResponse(path, media_type="application/x-pem-file", filename="plc-dashboard-ca.crt")
