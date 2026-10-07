import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from .. import db
from ..broker import link
from ..deps import CurrentUser, admin, operator, viewer

router = APIRouter(tags=["alarms"])

RuleType = Literal["high", "low", "equals", "fault", "offline", "stopped"]
Severity = Literal["critical", "warning", "info"]

ALARM_COLUMNS = """
    id, rule_id, rule_name, device_id, tag, severity, message, trigger_value, context, explain,
    raised_at, cleared_at, acked_at, acked_by, ack_comment
"""


def alarm_dict(row) -> dict:
    d = dict(row)
    for key in ("raised_at", "cleared_at", "acked_at"):
        d[key] = d[key].isoformat() if d[key] else None
    d["active"] = row["cleared_at"] is None
    return d


# ---------------------------------------------------------------- alarms


@router.get("/alarms")
async def list_alarms(
    state: Literal["active", "all"] = "active",
    device_id: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    _: CurrentUser = Depends(viewer),
) -> list[dict]:
    clauses, args = [], []
    if state == "active":
        clauses.append("cleared_at IS NULL")
    if device_id:
        args.append(device_id)
        clauses.append(f"device_id = ${len(args)}")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    args.append(limit)
    rows = await db.pool().fetch(
        f"SELECT {ALARM_COLUMNS} FROM alarms {where} ORDER BY raised_at DESC LIMIT ${len(args)}", *args
    )
    return [alarm_dict(r) for r in rows]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@router.get("/alarms/summary")
async def alarm_summary(
    device_id: str | None = None,
    hours: float = Query(24, gt=0, le=24 * 31),
    start: datetime | None = Query(None, alias="from"),
    end: datetime | None = Query(None, alias="to"),
    _: CurrentUser = Depends(viewer),
) -> dict:
    """Tiles, top sources and history for the Alarms screen.

    ``from`` and ``to`` select the event history (a day, a clock range, or a shift).
    Active counts stay current. Without a range, history is the last ``hours``.
    """
    if (start is None) != (end is None):
        raise HTTPException(400, "from and to must be set together")
    span_h = hours
    args: list = [hours]
    dev_filter = ""
    if device_id:
        args.append(device_id)
        dev_filter = "AND device_id = $2"
    row = await db.pool().fetchrow(
        f"""
        SELECT count(*) FILTER (WHERE cleared_at IS NULL AND severity = 'critical') AS active_critical,
               count(*) FILTER (WHERE cleared_at IS NULL AND severity <> 'critical') AS active_warning,
               count(*) FILTER (WHERE acked_at IS NULL AND cleared_at IS NULL) AS unacked_active,
               count(*) FILTER (WHERE acked_at IS NULL) AS unacked
        FROM alarms WHERE (cleared_at IS NULL OR raised_at > now() - make_interval(hours => $1::int)) {dev_filter}
        """,
        int(hours) if hours >= 1 else 1, *args[1:],
    )
    if start is not None and end is not None:
        start, end = _aware(start), _aware(end)
        span_h = (end - start).total_seconds() / 3600
        if span_h <= 0 or span_h > 24 * 31:
            raise HTTPException(400, "range must be between 0 and 31 days")
        range_args: list = [start, end]
        range_filter = ""
        if device_id:
            range_args.append(device_id)
            range_filter = f"AND device_id = ${len(range_args)}"
        recent = await db.pool().fetch(
            f"SELECT {ALARM_COLUMNS} FROM alarms WHERE raised_at >= $1 AND raised_at < $2 {range_filter} "
            f"ORDER BY raised_at DESC LIMIT 2000",
            *range_args,
        )
    else:
        recent = await db.pool().fetch(
            f"SELECT {ALARM_COLUMNS} FROM alarms WHERE raised_at > now() - make_interval(secs => $1 * 3600) {dev_filter} "
            f"ORDER BY raised_at DESC LIMIT 500",
            float(hours), *args[1:],
        )
    per_hour: dict[str, int] = {}
    sources: dict[str, int] = {}
    for r in recent:
        per_hour[r["raised_at"].strftime("%Y-%m-%d %H")] = per_hour.get(r["raised_at"].strftime("%Y-%m-%d %H"), 0) + 1
        sources[r["rule_name"]] = sources.get(r["rule_name"], 0) + 1
    return {
        **dict(row),
        "count": len(recent),
        "per_hour_avg": len(recent) / span_h,
        "peak_hour": max(per_hour.values(), default=0),
        "top_sources": [{"source": k, "count": v} for k, v in sorted(sources.items(), key=lambda kv: -kv[1])[:10]],
        "history": [alarm_dict(r) for r in recent],
    }


class AckAllIn(BaseModel):
    device_id: str | None = None
    comment: str = Field(default="", max_length=500)


@router.post("/alarms/ack-all")
async def ack_all(body: AckAllIn, user: CurrentUser = Depends(operator)) -> dict:
    args: list = [user.email, body.comment]
    dev_filter = ""
    if body.device_id:
        args.append(body.device_id)
        dev_filter = "AND device_id = $3"
    rows = await db.pool().fetch(
        f"UPDATE alarms SET acked_at = now(), acked_by = $1, ack_comment = $2 WHERE acked_at IS NULL {dev_filter} "
        f"RETURNING {ALARM_COLUMNS}",
        *args,
    )
    for r in rows:
        link.hub.broadcast(json.dumps({"type": "alarm", "event": "ack", "alarm": alarm_dict(r)}), None)
    await db.audit(user.email, "alarm.ack_all", body.device_id or "", {"count": len(rows)})
    return {"acknowledged": len(rows)}


class AckIn(BaseModel):
    comment: str = Field(default="", max_length=500)


@router.post("/alarms/{alarm_id}/ack")
async def ack_alarm(alarm_id: int, body: AckIn, user: CurrentUser = Depends(operator)) -> dict:
    row = await db.pool().fetchrow(
        f"UPDATE alarms SET acked_at = now(), acked_by = $2, ack_comment = $3 "
        f"WHERE id = $1 AND acked_at IS NULL RETURNING {ALARM_COLUMNS}",
        alarm_id, user.email, body.comment,
    )
    if row is None:
        raise HTTPException(409, "Alarm not found or already acknowledged")
    alarm = alarm_dict(row)
    await db.audit(user.email, "alarm.ack", str(alarm_id), {"comment": body.comment})
    link.hub.broadcast(json.dumps({"type": "alarm", "event": "ack", "alarm": alarm}), None)
    return alarm


# ---------------------------------------------------------------- rules


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    device_id: str | None = None
    tag: str | None = Field(default=None, max_length=64)
    rule_type: RuleType
    threshold: float | None = None
    deadband: float = Field(default=0, ge=0)
    severity: Severity = "warning"
    message: str = Field(default="", max_length=300)
    webhook_url: str | None = Field(default=None, max_length=500, pattern=r"^https://")
    enabled: bool = True

    @model_validator(mode="after")
    def check_fields(self) -> "RuleIn":
        if self.rule_type in ("high", "low", "equals"):
            if not self.tag or self.threshold is None:
                raise ValueError(f"'{self.rule_type}' rules need a tag and a threshold")
        if self.rule_type == "offline":
            if self.threshold is None:
                self.threshold = 60
            if self.threshold <= 0:
                raise ValueError("offline threshold is a number of seconds > 0")
        if self.rule_type in ("fault", "offline", "stopped"):
            self.tag = None
        self.device_id = self.device_id or None
        self.webhook_url = self.webhook_url or None
        return self


RULE_FIELDS = list(RuleIn.model_fields)


async def _clear_rule_alarms(rule_id: int) -> None:
    await db.pool().execute(
        "UPDATE alarms SET cleared_at = now() WHERE rule_id = $1 AND cleared_at IS NULL", rule_id
    )


@router.get("/alarm-rules")
async def list_rules(_: CurrentUser = Depends(viewer)) -> list[dict]:
    rows = await db.pool().fetch("SELECT * FROM alarm_rules ORDER BY name")
    return [{**dict(r), "created_at": r["created_at"].isoformat()} for r in rows]


@router.post("/alarm-rules", status_code=201)
async def create_rule(body: RuleIn, user: CurrentUser = Depends(admin)) -> dict:
    if body.device_id and not await db.pool().fetchval(
        "SELECT 1 FROM devices WHERE device_id = $1", body.device_id
    ):
        raise HTTPException(400, "Unknown device")
    values = body.model_dump()
    cols = ", ".join(RULE_FIELDS)
    params = ", ".join(f"${i}" for i in range(1, len(RULE_FIELDS) + 1))
    row = await db.pool().fetchrow(
        f"INSERT INTO alarm_rules ({cols}) VALUES ({params}) RETURNING *", *(values[f] for f in RULE_FIELDS)
    )
    await db.pool().execute("NOTIFY config_changed")
    await db.audit(user.email, "rule.create", str(row["id"]), values)
    return {**dict(row), "created_at": row["created_at"].isoformat()}


async def _refuse_managed(rule_id: int) -> None:
    managed = await db.pool().fetchval("SELECT managed_by FROM alarm_rules WHERE id = $1", rule_id)
    if managed and managed.startswith("process:"):
        raise HTTPException(409, "This is a built-in process rule; it follows the tag roles in Asset configuration")
    if managed:
        raise HTTPException(409, "This rule comes from tag limits; change it in Asset configuration")


@router.put("/alarm-rules/{rule_id}")
async def update_rule(rule_id: int, body: RuleIn, user: CurrentUser = Depends(admin)) -> dict:
    await _refuse_managed(rule_id)
    values = body.model_dump()
    assignments = ", ".join(f"{f} = ${i}" for i, f in enumerate(RULE_FIELDS, start=2))
    row = await db.pool().fetchrow(
        f"UPDATE alarm_rules SET {assignments} WHERE id = $1 RETURNING *",
        rule_id, *(values[f] for f in RULE_FIELDS),
    )
    if row is None:
        raise HTTPException(404, "Rule not found")
    # Conditions may have changed; let the ingestor re-raise from fresh state.
    await _clear_rule_alarms(rule_id)
    await db.pool().execute("NOTIFY config_changed")
    await db.audit(user.email, "rule.update", str(rule_id), values)
    return {**dict(row), "created_at": row["created_at"].isoformat()}


@router.delete("/alarm-rules/{rule_id}", status_code=204)
async def delete_rule(rule_id: int, user: CurrentUser = Depends(admin)) -> None:
    await _refuse_managed(rule_id)
    await _clear_rule_alarms(rule_id)
    result = await db.pool().execute("DELETE FROM alarm_rules WHERE id = $1", rule_id)
    if result == "DELETE 0":
        raise HTTPException(404, "Rule not found")
    await db.pool().execute("NOTIFY config_changed")
    await db.audit(user.email, "rule.delete", str(rule_id))
