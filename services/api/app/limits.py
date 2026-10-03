"""Tag limits -> alarm rules.

Each tag with a direction (high/low) and warning/critical limits gets up to two alarm rules,
owned by the tag (managed_by = "limit:<device>:<tag>:<warn|crit>"). Editing the limits in Asset
configuration rewrites those rules, so alarms use new limits immediately (the ingestor reloads
rules on NOTIFY config_changed). Rules created by hand in Admin -> Alarm rules are never touched.
"""

import asyncpg

ON_OFF_DELAY_S = 3.0  # against chattering around a limit


def managed_key(device_id: str, tag: str, level: str) -> str:
    return f"limit:{device_id}:{tag}:{level}"


async def sync_limit_rules(conn: asyncpg.Connection, device_id: str) -> None:
    tags = await conn.fetch(
        """
        SELECT tag, coalesce(nullif(display_name, ''), tag) AS label, unit, limit_dir, warn_limit, crit_limit,
               suppress_when_stopped, guidance
        FROM tags WHERE device_id = $1
        """,
        device_id,
    )
    wanted: dict[str, dict] = {}
    for t in tags:
        if t["limit_dir"] not in ("high", "low"):
            continue
        sign = ">" if t["limit_dir"] == "high" else "<"
        for level, limit, severity in (("warn", t["warn_limit"], "warning"), ("crit", t["crit_limit"], "critical")):
            if limit is None:
                continue
            unit = f" {t['unit']}" if t["unit"] else ""
            wanted[managed_key(device_id, t["tag"], level)] = {
                "name": f"{t['label']} {t['limit_dir']} ({sign} {limit:g}{unit})",
                "tag": t["tag"],
                "rule_type": t["limit_dir"],
                "threshold": float(limit),
                "severity": severity,
                "message": f"{t['label']} {t['limit_dir']}: {{value}}{unit} ({sign} {limit:g}{unit})",
                "suppress": t["suppress_when_stopped"],
                "guidance": t["guidance"],
            }

    existing = {
        r["managed_by"]: r["id"]
        for r in await conn.fetch(
            "SELECT id, managed_by FROM alarm_rules WHERE managed_by LIKE $1", f"limit:{device_id}:%"
        )
    }
    for key, rule_id in existing.items():
        if key not in wanted:
            await conn.execute("UPDATE alarms SET cleared_at = now() WHERE rule_id = $1 AND cleared_at IS NULL", rule_id)
            await conn.execute("DELETE FROM alarm_rules WHERE id = $1", rule_id)
    for key, r in wanted.items():
        await conn.execute(
            """
            INSERT INTO alarm_rules (name, device_id, tag, rule_type, threshold, deadband, severity, message,
                                     on_delay_s, off_delay_s, suppress_when_stopped, guidance, managed_by)
            VALUES ($1, $2, $3, $4, $5, 0, $6, $7, $8, $8, $9, $10, $11)
            ON CONFLICT (managed_by) WHERE managed_by IS NOT NULL DO UPDATE SET
                name = EXCLUDED.name, tag = EXCLUDED.tag, rule_type = EXCLUDED.rule_type,
                threshold = EXCLUDED.threshold, severity = EXCLUDED.severity, message = EXCLUDED.message,
                on_delay_s = EXCLUDED.on_delay_s, off_delay_s = EXCLUDED.off_delay_s,
                suppress_when_stopped = EXCLUDED.suppress_when_stopped, guidance = EXCLUDED.guidance,
                enabled = true
            """,
            r["name"], device_id, r["tag"], r["rule_type"], r["threshold"], r["severity"], r["message"],
            ON_OFF_DELAY_S, r["suppress"], r["guidance"], key,
        )
    await conn.execute("NOTIFY config_changed")
