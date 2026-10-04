"""TimescaleDB access for the ingestor."""

import datetime as dt
import json
from dataclasses import dataclass

import asyncpg

from .alarms import Rule
from .parsing import Reading, RegisterDef, TagConfig, Topic


@dataclass
class DeviceState:
    device_id: str
    expected_interval_s: float
    enabled: bool
    online: bool
    last_seen: dt.datetime | None
    status: str | None
    simulated: bool = False
    run_since: dt.datetime | None = None  # when the machine last went to RUN (restart grace)
    poll_s: float = 1.0  # gateway poll interval (asset_config.poll_interval_ms), spaces bursts

    @property
    def offline_after_s(self) -> float:
        return max(15.0, 3 * self.expected_interval_s)


@dataclass
class DeviceUpdate:
    last_seen: dt.datetime
    status: str | None = None
    msgs: int = 0
    last_seq: int | None = None
    gaps: int = 0


def _rows_affected(status: str) -> int:
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0


class Store:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # ---------------------------------------------------------------- config

    async def load_devices(self) -> dict[str, DeviceState]:
        rows = await self.pool.fetch(
            "SELECT device_id, expected_interval_s, enabled, online, last_seen, status, simulated, "
            "asset_config->'poll_interval_ms' AS poll_ms FROM devices"
        )
        out = {}
        for r in rows:
            row = dict(r)
            raw = row.pop("poll_ms")
            poll_ms = json.loads(raw) if raw is not None else None
            row["poll_s"] = (poll_ms / 1000.0 if isinstance(poll_ms, (int, float)) and poll_ms > 0
                             else float(row["expected_interval_s"] or 1.0))
            out[r["device_id"]] = DeviceState(**row)
        return out

    async def load_tag_configs(self) -> dict[tuple[str, str], TagConfig]:
        rows = await self.pool.fetch(
            "SELECT device_id, tag, data_type, value_scale, value_offset, value_labels, valid_min, valid_max FROM tags"
        )
        return {
            (r["device_id"], r["tag"]): TagConfig(
                r["data_type"], r["value_scale"], r["value_offset"],
                json.loads(r["value_labels"]) if isinstance(r["value_labels"], str) else r["value_labels"],
                r["valid_min"], r["valid_max"],
            )
            for r in rows
        }

    async def load_register_maps(self) -> tuple[dict[str, dict[int, RegisterDef]], dict[str, str]]:
        """Returns ({device: {address: RegisterDef}}, {device: status tag})."""
        maps: dict[str, dict[int, RegisterDef]] = {}
        status_tags: dict[str, str] = {}
        for r in await self.pool.fetch("SELECT device_id, address, tag, data_type, is_status FROM register_map"):
            maps.setdefault(r["device_id"], {})[r["address"]] = RegisterDef(r["address"], r["tag"], r["data_type"])
            if r["is_status"]:
                status_tags[r["device_id"]] = r["tag"]
        return maps, status_tags

    async def load_rules(self) -> list[Rule]:
        rows = await self.pool.fetch(
            "SELECT id, name, device_id, tag, rule_type, threshold, deadband, severity, message, webhook_url, "
            "on_delay_s, off_delay_s, suppress_when_stopped, guidance "
            "FROM alarm_rules WHERE enabled"
        )
        return [Rule(**dict(r)) for r in rows]

    async def load_active_alarms(self) -> dict[tuple[int, str], int]:
        rows = await self.pool.fetch(
            "SELECT id, rule_id, device_id FROM alarms WHERE cleared_at IS NULL AND rule_id IS NOT NULL"
        )
        return {(r["rule_id"], r["device_id"]): r["id"] for r in rows}

    async def register_device(self, topic: Topic) -> DeviceState:
        row = await self.pool.fetchrow(
            """
            INSERT INTO devices (device_id, name, site, line, simulated) VALUES ($1, $1, $2, $3, $4)
            ON CONFLICT (device_id) DO UPDATE SET
                site = CASE WHEN devices.site = '' THEN EXCLUDED.site ELSE devices.site END,
                line = CASE WHEN devices.line = '' THEN EXCLUDED.line ELSE devices.line END
            RETURNING device_id, expected_interval_s, enabled, online, last_seen, status, simulated
            """,
            topic.device_id, topic.site, topic.line, topic.simulated,
        )
        return DeviceState(**dict(row))

    async def register_tags(self, device_id: str, readings: list[Reading]) -> None:
        await self.pool.execute(
            """
            INSERT INTO tags (device_id, tag, data_type, decimals)
            SELECT $1, t.tag, t.data_type, t.decimals
            FROM unnest($2::text[], $3::text[], $4::int2[]) AS t(tag, data_type, decimals)
            ON CONFLICT DO NOTHING
            """,
            device_id,
            [r.tag for r in readings],
            [r.data_type for r in readings],
            [2 if r.data_type == "number" and not r.integral else 0 for r in readings],
        )

    # ---------------------------------------------------------------- telemetry

    async def write_batch(
        self,
        rows: list[tuple],
        latest: dict[tuple[str, str], tuple],
        devices: dict[str, DeviceUpdate],
        online: dict[str, bool],
    ) -> int:
        """rows: (ts, device_id, tag, value_num, value_text, quality). Returns rows inserted."""
        inserted = 0
        async with self.pool.acquire() as conn, conn.transaction():
            if rows:
                cols = list(zip(*rows))
                status = await conn.execute(
                    """
                    INSERT INTO telemetry (ts, device_id, tag, value_num, value_text, quality)
                    SELECT * FROM unnest($1::timestamptz[], $2::text[], $3::text[],
                                         $4::float8[], $5::text[], $6::int2[])
                    ON CONFLICT DO NOTHING
                    """,
                    *cols,
                )
                inserted = _rows_affected(status)
            if latest:
                keys, values = list(latest.keys()), list(latest.values())
                await conn.execute(
                    """
                    INSERT INTO tag_latest (device_id, tag, ts, value_num, value_text, quality)
                    SELECT * FROM unnest($1::text[], $2::text[], $3::timestamptz[],
                                         $4::float8[], $5::text[], $6::int2[])
                    ON CONFLICT (device_id, tag) DO UPDATE SET
                        ts = EXCLUDED.ts, value_num = EXCLUDED.value_num,
                        value_text = EXCLUDED.value_text, quality = EXCLUDED.quality
                    WHERE tag_latest.ts <= EXCLUDED.ts
                    """,
                    [k[0] for k in keys], [k[1] for k in keys],
                    [v[0] for v in values], [v[1] for v in values], [v[2] for v in values], [v[3] for v in values],
                )
            if devices:
                ids = list(devices)
                await conn.execute(
                    """
                    UPDATE devices d SET
                        last_seen = greatest(d.last_seen, u.last_seen),
                        status = coalesce(u.status, d.status),
                        online = u.online,
                        msg_count = d.msg_count + u.msgs,
                        last_seq = coalesce(u.last_seq, d.last_seq),
                        seq_gaps = d.seq_gaps + u.gaps
                    FROM unnest($1::text[], $2::timestamptz[], $3::text[], $4::int8[], $5::int8[], $6::int8[],
                                $7::bool[])
                         AS u(device_id, last_seen, status, msgs, last_seq, gaps, online)
                    WHERE d.device_id = u.device_id
                    """,
                    ids,
                    [devices[i].last_seen for i in ids],
                    [devices[i].status for i in ids],
                    [devices[i].msgs for i in ids],
                    [devices[i].last_seq for i in ids],
                    [devices[i].gaps for i in ids],
                    [online.get(i, True) for i in ids],
                )
        return inserted

    async def write_raw(self, rows: list[tuple]) -> None:
        """rows: (ts, device_id, topic, payload, status, detail)"""
        await self.pool.execute(
            """
            INSERT INTO raw_messages (ts, device_id, topic, payload, status, detail)
            SELECT * FROM unnest($1::timestamptz[], $2::text[], $3::text[], $4::text[], $5::text[], $6::text[])
            """,
            *zip(*rows),
        )

    async def set_online(self, device_id: str, online: bool) -> None:
        await self.pool.execute("UPDATE devices SET online = $2 WHERE device_id = $1", device_id, online)

    async def record_error(self, topic: str, device_id: str | None, reason: str, payload: bytes) -> None:
        await self.pool.execute(
            "INSERT INTO ingest_errors (topic, device_id, reason, payload) VALUES ($1, $2, $3, $4)",
            topic, device_id, reason[:500], payload[:4000].decode(errors="replace"),
        )

    # ---------------------------------------------------------------- alarms

    ALARM_COLUMNS = (
        "id, rule_id, rule_name, device_id, tag, severity, message, trigger_value, context, "
        "raised_at, cleared_at, acked_at, acked_by, ack_comment"
    )

    async def raise_alarm(self, rule: Rule, device_id: str, message: str, value: float | None) -> tuple[dict, bool]:
        """Returns (alarm, created). If an alarm is already active for the rule/device it is returned as-is."""
        row = await self.pool.fetchrow(
            f"""
            INSERT INTO alarms (rule_id, rule_name, device_id, tag, severity, message, trigger_value, context)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (rule_id, device_id) WHERE cleared_at IS NULL DO NOTHING
            RETURNING {self.ALARM_COLUMNS}
            """,
            rule.id, rule.name, device_id, rule.tag, rule.severity, message, value, rule.guidance,
        )
        if row is not None:
            return self._alarm_dict(row), True
        row = await self.pool.fetchrow(
            f"SELECT {self.ALARM_COLUMNS} FROM alarms WHERE rule_id = $1 AND device_id = $2 AND cleared_at IS NULL",
            rule.id, device_id,
        )
        return self._alarm_dict(row), False

    async def clear_alarm(self, alarm_id: int) -> dict | None:
        row = await self.pool.fetchrow(
            f"UPDATE alarms SET cleared_at = now() WHERE id = $1 AND cleared_at IS NULL RETURNING {self.ALARM_COLUMNS}",
            alarm_id,
        )
        return self._alarm_dict(row) if row else None

    @staticmethod
    def _alarm_dict(row) -> dict:
        d = dict(row)
        for key in ("raised_at", "cleared_at", "acked_at"):
            d[key] = d[key].isoformat() if d[key] else None
        d["active"] = row["cleared_at"] is None
        return d
