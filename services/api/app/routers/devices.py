import csv
import datetime as dt
import io
import re
import secrets
from typing import AsyncIterator, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from .. import db
from .. import limits, mqtt_accounts, register_map
from ..config import settings
from ..deps import CurrentUser, admin, viewer
from ..history import query_history

router = APIRouter(prefix="/devices", tags=["devices"])

ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
EXPORT_ROW_LIMIT = 1_000_000
QUALITY = {0: "GOOD", 1: "UNCERTAIN", 2: "BAD"}

DEVICE_COLUMNS = """
    d.device_id, d.name, d.site, d.line, d.description, d.expected_interval_s, d.enabled,
    d.has_credentials, d.online, d.status, d.last_seen, d.last_seq, d.seq_gaps, d.msg_count, d.created_at,
    d.simulated, d.asset_type, d.asset_config,
    (SELECT count(*) FROM alarms a WHERE a.device_id = d.device_id AND a.cleared_at IS NULL) AS active_alarms,
    (SELECT a.severity FROM alarms a WHERE a.device_id = d.device_id AND a.cleared_at IS NULL
      ORDER BY CASE a.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END LIMIT 1) AS top_severity
"""

TAG_COLUMNS = """
    t.device_id, t.tag, t.display_name, t.unit, t.data_type, t.value_scale, t.value_offset,
    t.min_value, t.max_value, t.decimals, t.pinned, t.configured, t.value_labels, t.valid_min, t.valid_max,
    t.limit_dir, t.warn_limit, t.crit_limit, t.role, t.suppress_when_stopped, t.guidance,
    l.ts, l.value_num, l.value_text, l.quality
"""


# ---------------------------------------------------------------- models


class DeviceCreate(BaseModel):
    device_id: str = Field(pattern=ID_PATTERN)
    name: str = Field(default="", max_length=120)
    site: str = Field(default="", max_length=64)
    line: str = Field(default="", max_length=64)
    description: str = Field(default="", max_length=500)
    expected_interval_s: float = Field(default=1, gt=0, le=86400)


class DeviceUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    site: str | None = Field(default=None, max_length=64)
    line: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=500)
    expected_interval_s: float | None = Field(default=None, gt=0, le=86400)
    enabled: bool | None = None
    asset_type: str | None = Field(default=None, max_length=80)
    asset_config: dict | None = None  # connection details shown in Asset configuration


class TagUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    unit: str | None = Field(default=None, max_length=32)
    data_type: Literal["number", "boolean", "string"] | None = None
    value_scale: float | None = None
    value_offset: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    decimals: int | None = Field(default=None, ge=0, le=6)
    pinned: bool | None = None
    valid_min: float | None = None
    valid_max: float | None = None


# ---------------------------------------------------------------- helpers


def tag_value(row) -> bool | float | str | None:
    if row["ts"] is None:
        return None
    if row["data_type"] == "string":
        return row["value_text"]
    if row["value_num"] is None:
        return row["value_text"]
    if row["data_type"] == "boolean":
        return bool(row["value_num"])
    return row["value_num"]


def tag_dict(row) -> dict:
    return {
        "tag": row["tag"],
        "display_name": row["display_name"] or row["tag"],
        "unit": row["unit"],
        "data_type": row["data_type"],
        "value_scale": row["value_scale"],
        "value_offset": row["value_offset"],
        "min_value": row["min_value"],
        "max_value": row["max_value"],
        "decimals": row["decimals"],
        "pinned": row["pinned"],
        "configured": row["configured"],
        "value_labels": row["value_labels"],
        "valid_min": row["valid_min"],
        "valid_max": row["valid_max"],
        "limit_dir": row["limit_dir"],
        "warn_limit": row["warn_limit"],
        "crit_limit": row["crit_limit"],
        "role": row["role"],
        "suppress_when_stopped": row["suppress_when_stopped"],
        "guidance": row["guidance"],
        "value": tag_value(row),
        "quality": QUALITY.get(row["quality"]) if row["quality"] is not None else None,
        "ts": row["ts"].isoformat() if row["ts"] else None,
    }


def device_dict(row) -> dict:
    d = dict(row)
    for key in ("last_seen", "created_at"):
        d[key] = d[key].isoformat() if d[key] else None
    return d


def connection_info(device_id: str, site: str, line: str) -> dict:
    site, line = site or "site", line or "line"
    base = f"plc/{site}/{line}/{device_id}"
    return {
        "host": settings.mqtt_public_host,
        "port": settings.mqtt_public_tls_port,
        "tls": True,
        "ca_certificate_url": "/api/v1/broker/ca.crt",
        "username": device_id,
        "client_id": device_id,
        "telemetry_topic": f"{base}/telemetry",
        "status_topic": f"{base}/status",
        "qos": 1,
        "last_will": {"topic": f"{base}/status", "payload": "offline", "qos": 1, "retain": True},
    }


async def notify_config_changed() -> None:
    await db.pool().execute("NOTIFY config_changed")


async def get_device_row(device_id: str):
    row = await db.pool().fetchrow(f"SELECT {DEVICE_COLUMNS} FROM devices d WHERE d.device_id = $1", device_id)
    if row is None:
        raise HTTPException(404, "Device not found")
    return row


def parse_range(start: dt.datetime | None, end: dt.datetime | None, default_hours: float = 1) -> tuple:
    end = end or dt.datetime.now(dt.timezone.utc)
    start = start or end - dt.timedelta(hours=default_hours)
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt.timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=dt.timezone.utc)
    if start >= end:
        raise HTTPException(400, "'from' must be before 'to'")
    if end - start > dt.timedelta(days=5 * 366):
        raise HTTPException(400, "Range too large")
    return start, end


def parse_tags(tags: str) -> list[str]:
    names = [t for t in (s.strip() for s in tags.split(",")) if t]
    if not names or len(names) > 20 or any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", t) for t in names):
        raise HTTPException(400, "Provide 1-20 valid tag names")
    return names


async def provision_credentials(device_id: str) -> str:
    password = secrets.token_urlsafe(18)
    async with db.pool().acquire() as conn:
        await mqtt_accounts.set_device_password(conn, device_id, password)
    return password


# ---------------------------------------------------------------- routes


@router.get("")
async def list_devices(_: CurrentUser = Depends(viewer)) -> list[dict]:
    devices = await db.pool().fetch(
        f"SELECT {DEVICE_COLUMNS} FROM devices d ORDER BY d.site, d.line, d.device_id"
    )
    tags = await db.pool().fetch(
        f"SELECT {TAG_COLUMNS} FROM tags t LEFT JOIN tag_latest l USING (device_id, tag) "
        "LEFT JOIN register_map rm USING (device_id, tag) "
        "ORDER BY t.device_id, t.pinned DESC, rm.address NULLS LAST, t.tag"
    )
    by_device: dict[str, list[dict]] = {}
    for row in tags:
        by_device.setdefault(row["device_id"], []).append(tag_dict(row))

    result = []
    for row in devices:
        d = device_dict(row)
        all_tags = by_device.get(d["device_id"], [])
        pinned = [t for t in all_tags if t["pinned"]]
        d["tag_count"] = len(all_tags)
        preview = (pinned or [t for t in all_tags if t["data_type"] != "string"])[:6]
        # tags with an analytics role (speed, moisture, ...) are always included for the asset cards
        preview += [t for t in all_tags if t.get("role") and t not in preview]
        d["preview_tags"] = preview
        result.append(d)
    return result


@router.post("", status_code=201)
async def create_device(body: DeviceCreate, user: CurrentUser = Depends(admin)) -> dict:
    existing = await db.pool().fetchrow(
        "SELECT has_credentials, simulated FROM devices WHERE device_id = $1", body.device_id
    )
    if existing and existing["simulated"]:
        raise HTTPException(409, "A simulated device uses this ID; remove it in the simulator first")
    if existing and existing["has_credentials"]:
        raise HTTPException(409, "Device already exists (use rotate credentials to get a new password)")

    password = await provision_credentials(body.device_id)
    await db.pool().execute(
        """
        INSERT INTO devices (device_id, name, site, line, description, expected_interval_s, has_credentials)
        VALUES ($1, $2, $3, $4, $5, $6, true)
        ON CONFLICT (device_id) DO UPDATE SET
            name = EXCLUDED.name, site = EXCLUDED.site, line = EXCLUDED.line,
            description = EXCLUDED.description, expected_interval_s = EXCLUDED.expected_interval_s,
            has_credentials = true
        """,
        body.device_id, body.name, body.site, body.line, body.description, body.expected_interval_s,
    )
    await notify_config_changed()
    await db.audit(user.email, "device.create", body.device_id, body.model_dump())
    return {
        "device": device_dict(await get_device_row(body.device_id)),
        "credentials": {"username": body.device_id, "password": password},
        "connection": connection_info(body.device_id, body.site, body.line),
    }


@router.get("/{device_id}")
async def get_device(device_id: str, _: CurrentUser = Depends(viewer)) -> dict:
    d = device_dict(await get_device_row(device_id))
    rows = await db.pool().fetch(
        f"SELECT {TAG_COLUMNS} FROM tags t LEFT JOIN tag_latest l USING (device_id, tag) "
        "LEFT JOIN register_map rm USING (device_id, tag) "
        "WHERE t.device_id = $1 ORDER BY t.pinned DESC, rm.address NULLS LAST, t.tag",
        device_id,
    )
    d["tags"] = [tag_dict(r) for r in rows]
    d["connection"] = connection_info(device_id, d["site"], d["line"])
    return d


@router.get("/{device_id}/latest")
async def get_latest(device_id: str, _: CurrentUser = Depends(viewer)) -> list[dict]:
    await get_device_row(device_id)
    rows = await db.pool().fetch(
        f"SELECT {TAG_COLUMNS} FROM tags t LEFT JOIN tag_latest l USING (device_id, tag) "
        "WHERE t.device_id = $1 ORDER BY t.tag",
        device_id,
    )
    return [tag_dict(r) for r in rows]


@router.patch("/{device_id}")
async def update_device(device_id: str, body: DeviceUpdate, user: CurrentUser = Depends(admin)) -> dict:
    row = await get_device_row(device_id)
    changes = body.model_dump(exclude_none=True)
    if not changes:
        return device_dict(row)
    if "enabled" in changes:
        async with db.pool().acquire() as conn:
            await mqtt_accounts.set_enabled(conn, device_id, changes["enabled"])
    assignments = ", ".join(f"{col} = ${i}" for i, col in enumerate(changes, start=2))
    await db.pool().execute(
        f"UPDATE devices SET {assignments} WHERE device_id = $1", device_id, *changes.values()
    )
    await notify_config_changed()
    await db.audit(user.email, "device.update", device_id, changes)
    return device_dict(await get_device_row(device_id))


@router.delete("/{device_id}", status_code=204)
async def delete_device(
    device_id: str,
    purge_data: bool = Query(False, description="Also delete stored telemetry"),
    user: CurrentUser = Depends(admin),
) -> None:
    await get_device_row(device_id)
    async with db.pool().acquire() as conn, conn.transaction():
        await mqtt_accounts.delete_account(conn, device_id)
        await conn.execute(
            "UPDATE alarms SET cleared_at = now() WHERE device_id = $1 AND cleared_at IS NULL", device_id
        )
        await conn.execute("DELETE FROM tag_latest WHERE device_id = $1", device_id)
        await conn.execute("DELETE FROM devices WHERE device_id = $1", device_id)
        if purge_data:
            await conn.execute("DELETE FROM telemetry WHERE device_id = $1", device_id)
    await notify_config_changed()
    await db.audit(user.email, "device.delete", device_id, {"purge_data": purge_data})


@router.post("/{device_id}/credentials")
async def rotate_credentials(device_id: str, user: CurrentUser = Depends(admin)) -> dict:
    row = await get_device_row(device_id)
    password = await provision_credentials(device_id)
    await db.pool().execute("UPDATE devices SET has_credentials = true WHERE device_id = $1", device_id)
    await db.audit(user.email, "device.credentials.rotate", device_id)
    return {
        "credentials": {"username": device_id, "password": password},
        "connection": connection_info(device_id, row["site"], row["line"]),
    }


@router.patch("/{device_id}/tags/{tag}")
async def update_tag(device_id: str, tag: str, body: TagUpdate, user: CurrentUser = Depends(admin)) -> dict:
    changes = body.model_dump(exclude_unset=True)
    for key in ("display_name", "unit", "value_scale", "value_offset", "decimals", "pinned", "data_type"):
        if key in changes and changes[key] is None:
            del changes[key]  # these columns are NOT NULL; null means "leave unchanged"
    changes["configured"] = True
    assignments = ", ".join(f"{col} = ${i}" for i, col in enumerate(changes, start=3))
    result = await db.pool().execute(
        f"UPDATE tags SET {assignments} WHERE device_id = $1 AND tag = $2", device_id, tag, *changes.values()
    )
    if result == "UPDATE 0":
        raise HTTPException(404, "Tag not found")
    async with db.pool().acquire() as conn:
        await limits.sync_limit_rules(conn, device_id)
    await db.audit(user.email, "tag.update", f"{device_id}/{tag}", body.model_dump(exclude_unset=True))
    row = await db.pool().fetchrow(
        f"SELECT {TAG_COLUMNS} FROM tags t LEFT JOIN tag_latest l USING (device_id, tag) "
        "WHERE t.device_id = $1 AND t.tag = $2",
        device_id, tag,
    )
    return tag_dict(row)


@router.delete("/{device_id}/tags/{tag}", status_code=204)
async def delete_tag(device_id: str, tag: str, user: CurrentUser = Depends(admin)) -> None:
    async with db.pool().acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM tag_latest WHERE device_id = $1 AND tag = $2", device_id, tag)
        await conn.execute("DELETE FROM tags WHERE device_id = $1 AND tag = $2", device_id, tag)
    await notify_config_changed()
    await db.audit(user.email, "tag.delete", f"{device_id}/{tag}")


# ---------------------------------------------------------------- Modbus register map


class RegisterIn(BaseModel):
    address: int = Field(gt=0, le=499999)
    tag: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    data_type: Literal["int16", "uint16", "int32", "uint32", "float32", "float64", "bool"]
    value_labels: dict[str, str] | None = None
    is_status: bool = False
    unit: str = Field(default="", max_length=32)
    display_name: str = Field(default="", max_length=120)
    valid_min: float | None = None
    valid_max: float | None = None


class RegisterMapIn(BaseModel):
    registers: list[RegisterIn] = Field(max_length=500)


class MappingText(BaseModel):
    text: str = Field(max_length=20000)


@router.get("/{device_id}/register-map")
async def get_register_map(device_id: str, _: CurrentUser = Depends(viewer)) -> list[dict]:
    rows = await db.pool().fetch(
        """
        SELECT r.address, r.tag, r.data_type, r.is_status, t.value_labels, t.valid_min, t.valid_max,
               coalesce(t.unit, '') AS unit, coalesce(t.display_name, '') AS display_name
        FROM register_map r LEFT JOIN tags t USING (device_id, tag)
        WHERE r.device_id = $1 ORDER BY r.address
        """,
        device_id,
    )
    return [dict(r) for r in rows]


@router.post("/{device_id}/register-map/parse")
async def parse_register_map(device_id: str, body: MappingText, _: CurrentUser = Depends(admin)) -> dict:
    """Preview: turn a pasted mapping list into registers (nothing is saved)."""
    return register_map.parse_mapping(body.text).as_dict()


@router.put("/{device_id}/register-map")
async def put_register_map(device_id: str, body: RegisterMapIn, user: CurrentUser = Depends(admin)) -> list[dict]:
    await get_device_row(device_id)
    regs = sorted(body.registers, key=lambda r: r.address)
    check = register_map.ParseResult(registers=[
        register_map.Register(r.address, r.tag, r.data_type) for r in regs
    ])
    register_map.check_layout(check)
    if check.errors:
        raise HTTPException(400, "; ".join(check.errors))
    for r in regs:
        if r.valid_min is not None and r.valid_max is not None and r.valid_min > r.valid_max:
            raise HTTPException(400, f"{r.tag}: valid minimum is above valid maximum")
    if sum(r.is_status for r in regs) > 1:
        raise HTTPException(400, "Only one register can drive the device status")

    async with db.pool().acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM register_map WHERE device_id = $1", device_id)
        for r in regs:
            await conn.execute(
                "INSERT INTO register_map (device_id, address, tag, data_type, is_status) VALUES ($1, $2, $3, $4, $5)",
                device_id, r.address, r.tag, r.data_type, r.is_status,
            )
            integer = r.data_type in ("int16", "uint16", "int32", "uint32")
            await conn.execute(
                """
                INSERT INTO tags (device_id, tag, data_type, decimals, value_labels, unit, display_name,
                                  valid_min, valid_max, configured)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, true)
                ON CONFLICT (device_id, tag) DO UPDATE SET
                    data_type = EXCLUDED.data_type, value_labels = EXCLUDED.value_labels,
                    valid_min = EXCLUDED.valid_min, valid_max = EXCLUDED.valid_max,
                    decimals = CASE WHEN tags.configured THEN tags.decimals ELSE EXCLUDED.decimals END,
                    unit = CASE WHEN EXCLUDED.unit <> '' THEN EXCLUDED.unit ELSE tags.unit END,
                    display_name = CASE WHEN EXCLUDED.display_name <> '' THEN EXCLUDED.display_name ELSE tags.display_name END,
                    configured = true
                """,
                device_id, r.tag, "boolean" if r.data_type == "bool" else "number", 0 if integer else 2,
                r.value_labels, r.unit, r.display_name, r.valid_min, r.valid_max,
            )
        await conn.execute("NOTIFY config_changed")
    await db.audit(user.email, "device.register_map", device_id, {"registers": len(regs)})
    return await get_register_map(device_id, user)


# ---------------------------------------------------------------- tag settings (Asset configuration)

ROLE_NAMES = Literal["machine_status", "speed", "motor_current", "steam_pressure", "moisture", "vibration"]


class TagSettingIn(BaseModel):
    tag: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    display_name: str | None = Field(default=None, max_length=120)
    unit: str | None = Field(default=None, max_length=32)
    min_value: float | None = None  # normal range
    max_value: float | None = None
    limit_dir: Literal["high", "low"] | None = None
    warn_limit: float | None = None
    crit_limit: float | None = None
    role: ROLE_NAMES | None = None
    suppress_when_stopped: bool = True
    guidance: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def check(self) -> "TagSettingIn":
        if self.min_value is not None and self.max_value is not None and self.min_value > self.max_value:
            raise ValueError(f"{self.tag}: normal minimum is above the maximum")
        if self.limit_dir and self.warn_limit is not None and self.crit_limit is not None:
            if self.limit_dir == "high" and self.crit_limit < self.warn_limit:
                raise ValueError(f"{self.tag}: for a high limit, critical must be above warning")
            if self.limit_dir == "low" and self.crit_limit > self.warn_limit:
                raise ValueError(f"{self.tag}: for a low limit, critical must be below warning")
        if not self.limit_dir and (self.warn_limit is not None or self.crit_limit is not None):
            raise ValueError(f"{self.tag}: choose high or low for the limits")
        return self


class TagSettingsIn(BaseModel):
    tags: list[TagSettingIn] = Field(max_length=500)


@router.put("/{device_id}/tag-settings")
async def put_tag_settings(device_id: str, body: TagSettingsIn, user: CurrentUser = Depends(admin)) -> list[dict]:
    """Save names, units, normal range, warning/critical limits and roles for many tags at once.
    Limits become alarm rules (app/limits.py)."""
    await get_device_row(device_id)
    roles = [t.role for t in body.tags if t.role]
    if len(roles) != len(set(roles)):
        raise HTTPException(400, "Each role can be assigned to one tag only")
    async with db.pool().acquire() as conn, conn.transaction():
        if roles:  # a role moves to the tag it is assigned to now
            await conn.execute("UPDATE tags SET role = NULL WHERE device_id = $1 AND role = ANY($2)", device_id, roles)
        for t in body.tags:
            res = await conn.execute(
                """
                UPDATE tags SET display_name = coalesce($3, display_name), unit = coalesce($4, unit),
                    min_value = $5, max_value = $6, limit_dir = $7, warn_limit = $8, crit_limit = $9, role = $10,
                    suppress_when_stopped = $11, guidance = $12, configured = true
                WHERE device_id = $1 AND tag = $2
                """,
                device_id, t.tag, t.display_name, t.unit, t.min_value, t.max_value, t.limit_dir,
                t.warn_limit, t.crit_limit, t.role, t.suppress_when_stopped, t.guidance,
            )
            if res == "UPDATE 0":
                raise HTTPException(404, f"Tag {t.tag} not found")
        await limits.sync_limit_rules(conn, device_id)
    await db.audit(user.email, "tag.settings", device_id, {"tags": len(body.tags)})
    rows = await db.pool().fetch(
        f"SELECT {TAG_COLUMNS} FROM tags t LEFT JOIN tag_latest l USING (device_id, tag) WHERE t.device_id = $1 ORDER BY t.tag",
        device_id,
    )
    return [tag_dict(r) for r in rows]


@router.get("/{device_id}/history")
async def history(
    device_id: str,
    tags: str = Query(..., description="Comma-separated tag names"),
    start: dt.datetime | None = Query(None, alias="from"),
    end: dt.datetime | None = Query(None, alias="to"),
    _: CurrentUser = Depends(viewer),
) -> dict:
    start, end = parse_range(start, end)
    return await query_history(db.pool(), device_id, parse_tags(tags), start, end)


@router.get("/{device_id}/export.csv")
async def export_csv(
    device_id: str,
    tags: str = Query(...),
    start: dt.datetime | None = Query(None, alias="from"),
    end: dt.datetime | None = Query(None, alias="to"),
    user: CurrentUser = Depends(viewer),
) -> StreamingResponse:
    start, end = parse_range(start, end)
    tag_list = parse_tags(tags)
    await db.audit(user.email, "export.csv", device_id, {"tags": tag_list, "from": str(start), "to": str(end)})

    async def rows() -> AsyncIterator[str]:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["ts", "device_id", "tag", "value", "quality"])
        async with db.pool().acquire() as conn, conn.transaction():
            cursor = conn.cursor(
                "SELECT ts, tag, value_num, value_text, quality FROM telemetry "
                "WHERE device_id = $1 AND tag = ANY($2) AND ts >= $3 AND ts < $4 ORDER BY ts LIMIT $5",
                device_id, tag_list, start, end, EXPORT_ROW_LIMIT,
            )
            n = 0
            async for r in cursor:
                value = r["value_text"] if r["value_text"] is not None else r["value_num"]
                writer.writerow([r["ts"].isoformat(), device_id, r["tag"], value, QUALITY.get(r["quality"], "")])
                n += 1
                if n % 1000 == 0:
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate()
        yield buf.getvalue()

    filename = f"{device_id}_{start:%Y%m%dT%H%M}_{end:%Y%m%dT%H%M}.csv"
    return StreamingResponse(
        rows(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )
