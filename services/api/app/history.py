import datetime as dt
from dataclasses import dataclass

import asyncpg

MAX_POINTS = 1500
NICE_BUCKETS = [
    1, 2, 5, 10, 15, 30,
    60, 120, 300, 600, 900, 1800,
    3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800,
]


@dataclass(frozen=True)
class Resolution:
    source: str  # raw | 1m | 1h
    bucket_s: int


def choose_resolution(start: dt.datetime, end: dt.datetime, max_points: int = MAX_POINTS) -> Resolution:
    """Pick the cheapest table and a 'nice' bucket so each series has <= max_points."""
    span = max((end - start).total_seconds(), 1.0)
    needed = span / max_points
    bucket = next((b for b in NICE_BUCKETS if b >= needed), NICE_BUCKETS[-1])
    if bucket < 60:
        return Resolution("raw", bucket)
    if bucket < 3600:
        return Resolution("1m", bucket)
    return Resolution("1h", bucket)


_QUERIES = {
    "raw": """
        SELECT time_bucket($1::interval, ts) AS t, tag,
               avg(value_num) AS avg, min(value_num) AS min, max(value_num) AS max
        FROM telemetry
        WHERE device_id = $2 AND tag = ANY($3) AND ts >= $4 AND ts < $5 AND value_num IS NOT NULL
        GROUP BY t, tag ORDER BY t
    """,
    "1m": """
        SELECT time_bucket($1::interval, bucket) AS t, tag,
               sum(sum_val) / nullif(sum(cnt), 0) AS avg, min(min_val) AS min, max(max_val) AS max
        FROM telemetry_1m
        WHERE device_id = $2 AND tag = ANY($3) AND bucket >= $4 AND bucket < $5
        GROUP BY t, tag ORDER BY t
    """,
    "1h": """
        SELECT time_bucket($1::interval, bucket) AS t, tag,
               sum(sum_val) / nullif(sum(cnt), 0) AS avg, min(min_val) AS min, max(max_val) AS max
        FROM telemetry_1h
        WHERE device_id = $2 AND tag = ANY($3) AND bucket >= $4 AND bucket < $5
        GROUP BY t, tag ORDER BY t
    """,
}


async def query_history(
    conn: asyncpg.Connection | asyncpg.Pool,
    device_id: str,
    tags: list[str],
    start: dt.datetime,
    end: dt.datetime,
) -> dict:
    res = choose_resolution(start, end)
    rows = await conn.fetch(
        _QUERIES[res.source], dt.timedelta(seconds=res.bucket_s), device_id, tags, start, end
    )
    series: dict[str, list[list]] = {tag: [] for tag in tags}
    for r in rows:
        series[r["tag"]].append([int(r["t"].timestamp() * 1000), r["avg"], r["min"], r["max"]])
    return {
        "resolution": res.source,
        "bucket_s": res.bucket_s,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "series": series,
    }
