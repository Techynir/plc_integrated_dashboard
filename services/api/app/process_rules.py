"""Process rules -> alarm rules.

The rules themselves run in the ingestor (ingestor/process_rules.py), which looks them up by
managed_by = "process:<device>:<code>". This module creates those rows for the rules an asset can
use (it has the tag roles they need), and removes them when a role is unassigned. They are kept
in step with the tag roles like the limit rules (app/limits.py), and cannot be edited by hand.
"""

import asyncpg

# code, name, severity, roles needed, on delay s, off delay s, screen, condition (Admin -> Alarm rules)
CATALOG = [
    ("status_speed", "Status and speed disagree", "warning", ("machine_status", "speed"), 60, 10, "health",
     "status Running but nearly no speed, or status Stopped at full speed, for 1 min"),
    ("load_high", "Motor working too hard for its speed", "warning", ("motor_current", "speed"), 120, 30, "health",
     "current more than 10 A above the current expected for the speed, for 2 min"),
    ("load_drop", "Motor load dropped", "warning", ("motor_current", "speed"), 10, 30, "health",
     "current more than 10 A below the current expected for the speed, for 10 s"),
    ("vib_low_speed", "Bearing shakes too much for its speed", "warning", ("vibration", "speed"), 120, 30, "health",
     "below full speed, vibration corrected to full speed is above the warning limit, for 2 min"),
    ("vib_rise", "Bearing shaking more than usual", "warning", ("vibration", "speed"), 120, 60, "health",
     "vibration corrected for speed is more than 25 % of the warning limit above its usual level, for 2 min"),
    ("steam_wet", "Low steam: paper getting wet", "warning", ("steam_pressure", "moisture"), 10, 30, "quality",
     "steam pressure low enough that moisture will pass its warning limit, for 10 s"),
    ("wet_other", "Paper wet while steam is normal", "warning", ("moisture",), 60, 30, "quality",
     "moisture above its warning limit with no low steam in the last 10 min, for 1 min"),
    ("wet_restart", "Paper wet after restart", "warning", ("moisture",), 30, 30, "quality",
     "in the first 15 min after a restart, moisture 0.5 above its normal range, for 30 s"),
]
SCREEN = {code: screen for code, *_rest, screen, _cond in CATALOG}
NAMES = {code: name for code, name, *_ in CATALOG}


def managed_key(device_id: str, code: str) -> str:
    return f"process:{device_id}:{code}"


def code_of(managed_by: str | None) -> str | None:
    return managed_by.rsplit(":", 1)[-1] if managed_by and managed_by.startswith("process:") else None


async def sync_process_rules(conn: asyncpg.Connection, device_id: str) -> None:
    roles = {r["role"] for r in await conn.fetch("SELECT role FROM tags WHERE device_id = $1 AND role IS NOT NULL", device_id)}
    wanted = {managed_key(device_id, c[0]): c for c in CATALOG if set(c[3]) <= roles}
    existing = {
        r["managed_by"]: r["id"]
        for r in await conn.fetch("SELECT id, managed_by FROM alarm_rules WHERE managed_by LIKE $1", f"process:{device_id}:%")
    }
    for key, rule_id in existing.items():
        if key not in wanted:
            await conn.execute("UPDATE alarms SET cleared_at = now() WHERE rule_id = $1 AND cleared_at IS NULL", rule_id)
            await conn.execute("DELETE FROM alarm_rules WHERE id = $1", rule_id)
    for key, (code, name, severity, _roles, on_s, off_s, _screen, condition) in wanted.items():
        await conn.execute(
            """
            INSERT INTO alarm_rules (name, device_id, rule_type, severity, message, on_delay_s, off_delay_s, managed_by)
            VALUES ($1, $2, 'process', $3, $4, $5, $6, $7)
            ON CONFLICT (managed_by) WHERE managed_by IS NOT NULL DO UPDATE SET
                name = EXCLUDED.name, severity = EXCLUDED.severity, message = EXCLUDED.message,
                on_delay_s = EXCLUDED.on_delay_s, off_delay_s = EXCLUDED.off_delay_s
            """,
            name, device_id, severity, condition, float(on_s), float(off_s), key,
        )
    await conn.execute("NOTIFY config_changed")


async def sync_all(conn: asyncpg.Connection) -> None:
    for r in await conn.fetch("SELECT device_id FROM devices"):
        await sync_process_rules(conn, r["device_id"])
