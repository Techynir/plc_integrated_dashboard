"""Re-decode stored raw messages (last 7 days) for one device with its current register map
and tag configuration. Use after setting up or changing a register map:

    docker compose exec ingestor python -m ingestor.backfill conveyer-plc-line-01 [--replace]

Values are written with the time each message was received. Without --replace, existing rows
for the same tag and time are left alone (running it twice is harmless); with --replace, the
device's stored values for the re-decoded period are deleted first (use after changing the map
or valid ranges). The 1-minute and 1-hour rollups are rebuilt for that period.
"""

import asyncio
import datetime as dt
import os
import sys

import asyncpg

from .main import _default_schema_path
from .parsing import (
    InvalidMessage,
    apply_tag_config,
    decode_telemetry,
    load_validator,
    parse_topic,
    reject_bad_reads,
    status_from_label,
)
from .store import Store

BATCH = 5000


async def backfill(device_id: str, replace_existing: bool = False) -> None:
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    store = Store(pool)
    validator = load_validator(_default_schema_path())
    maps, status_tags = await store.load_register_maps()
    tag_cfg = await store.load_tag_configs()
    register_map = maps.get(device_id)
    status_tag = status_tags.get(device_id)

    raw = await pool.fetch(
        "SELECT ts, topic, payload FROM raw_messages WHERE device_id = $1 AND status IN ('ok', 'rejected') ORDER BY ts",
        device_id,
    )
    print(f"{device_id}: {len(raw)} stored messages, register map: {len(register_map or {})} registers")
    if replace_existing and raw:
        deleted = await pool.execute(
            "DELETE FROM telemetry WHERE device_id = $1 AND ts BETWEEN $2 AND $3", device_id, raw[0]["ts"], raw[-1]["ts"]
        )
        print(f"--replace: {deleted.split()[-1]} stored values removed for {raw[0]['ts']:%Y-%m-%d %H:%M} .. {raw[-1]['ts']:%H:%M} UTC")
    rows, latest, decoded, skipped, last_status, bad_reads = [], {}, 0, 0, None, 0
    for r in raw:
        topic = parse_topic(r["topic"])
        if topic is None:
            skipped += 1
            continue
        try:
            msg = decode_telemetry(topic, r["payload"].encode(), r["ts"], validator, register_map)
        except InvalidMessage:
            skipped += 1
            continue
        if msg.format != "modbus" and register_map:
            skipped += 1  # only gateway messages are affected by the register map
            continue
        decoded += 1
        readings = [apply_tag_config(x, tag_cfg.get((device_id, x.tag))) for x in msg.readings]
        readings, reasons = reject_bad_reads(readings, lambda tag: tag_cfg.get((device_id, tag)))
        bad_reads += bool(reasons)
        for x in readings:
            rows.append((msg.ts, device_id, x.tag, x.value_num, x.value_text, x.quality))
            if x.rejected:
                continue
            latest[x.tag] = (msg.ts, x.value_num, x.value_text, x.quality)
            if x.tag == status_tag and x.value_num is not None:
                cfg = tag_cfg.get((device_id, x.tag))
                labels = (cfg.value_labels if cfg else None) or {}
                last_status = status_from_label(labels.get(f"{x.value_num:g}")) or last_status
        if len(rows) >= BATCH:
            await store.write_batch(rows, {}, {}, {})
            rows = []
    if rows:
        await store.write_batch(rows, {}, {}, {})
    if latest:
        await store.write_batch([], {(device_id, t): v for t, v in latest.items()}, {}, {})
    if last_status:
        await pool.execute("UPDATE devices SET status = $2 WHERE device_id = $1", device_id, last_status)
    if raw:
        # Rollups only refresh the last few hours by themselves; rebuild them for this period.
        start = raw[0]["ts"].replace(second=0, microsecond=0, minute=0)
        end = raw[-1]["ts"] + dt.timedelta(hours=1)
        async with pool.acquire() as conn:
            await conn.execute("CALL refresh_continuous_aggregate('telemetry_1m', $1::timestamptz, $2::timestamptz)", start, end)
            await conn.execute("CALL refresh_continuous_aggregate('telemetry_1h', $1::timestamptz, $2::timestamptz)", start, end)
        print("rebuilt 1-minute and 1-hour rollups")
    print(f"bad reads rejected: {bad_reads}")
    await pool.close()
    print(f"re-decoded {decoded} messages ({skipped} skipped); latest values: "
          + ", ".join(f"{t}={v[1] if v[1] is not None else v[2]}" for t, v in sorted(latest.items()))
          + (f"; status {last_status}" if last_status else ""))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--replace"]
    if len(args) != 1:
        sys.exit("usage: python -m ingestor.backfill <device_id> [--replace]")
    asyncio.run(backfill(args[0], replace_existing="--replace" in sys.argv))
