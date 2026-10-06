"""Apply the process rules (process_rules.py) to data that is already stored, and raise their
alarms with the times they would have been raised live. The data itself is not changed.

    docker compose stop ingestor
    docker compose run --rm --no-deps ingestor python -m ingestor.rules_backfill conveyer-plc-line-01 --days 7 --fit --replace
    docker compose start ingestor

--fit     derive the coefficients from the stored data and save them in asset_config.rules:
          expected current for a speed (running minutes), and moisture for a steam pressure
          (moisture one minute later, running minutes). Live evaluation uses the same values.
--replace delete the process-rule alarms already stored for the window first.

Stop the live ingestor while this runs; when it starts again it picks up alarms still active.
"""

import argparse
import asyncio
import datetime as dt
import json
import os
import time

import asyncpg

from .parsing import status_from_label
from .process_rules import Inputs, ProcessEngine
from .store import Store


def linreg(xs: list[float], ys: list[float]) -> tuple[float, float, float] | None:
    """(a, b, rmse) of y = a + b x."""
    n = len(xs)
    if n < 30:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    rmse = (sum((y - a - b * x) ** 2 for x, y in zip(xs, ys)) / n) ** 0.5
    return a, b, rmse


def robust_fit(xs: list[float], ys: list[float]) -> tuple[float, float, float, int] | None:
    """Fit, drop points more than 3 rmse off (problem events), fit again."""
    first = linreg(xs, ys)
    if first is None:
        return None
    a, b, rmse = first
    keep = [(x, y) for x, y in zip(xs, ys) if abs(y - a - b * x) <= 3 * max(rmse, 1e-6)]
    second = linreg([x for x, _ in keep], [y for _, y in keep])
    return (*second, len(keep)) if second else (a, b, rmse, len(xs))


async def running_minutes(pool, device_id: str, inputs: Inputs, codes: list[float] | None, start, end) -> dict:
    """{minute: {role: mean}} for minutes the machine ran the whole minute."""
    tags = {info.tag: role for role, info in inputs.roles.items()}
    rows = await pool.fetch(
        "SELECT bucket, tag, sum_val / nullif(cnt, 0) AS v, min_val, max_val FROM telemetry_1m "
        "WHERE device_id = $1 AND tag = ANY($2) AND bucket >= $3 AND bucket < $4",
        device_id, list(tags), start, end,
    )
    by_min: dict = {}
    status_ok: dict = {}
    for r in rows:
        role = tags[r["tag"]]
        if role == "machine_status":
            status_ok[r["bucket"]] = codes is not None and r["min_val"] in codes and r["max_val"] in codes
        elif r["v"] is not None:
            by_min.setdefault(r["bucket"], {})[role] = float(r["v"])
    ref = inputs.speed_ref or 0
    return {
        m: vals for m, vals in by_min.items()
        if status_ok.get(m, "machine_status" not in inputs.roles) and vals.get("speed", 0) > 0.35 * ref
    }


async def fit(pool, device_id: str, inputs: Inputs, codes, start, end) -> dict:
    mins = await running_minutes(pool, device_id, inputs, codes, start, end)
    out = {"fitted_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "fitted_days": round((end - start).total_seconds() / 86400, 1)}
    pts = [(v["speed"], v["motor_current"]) for v in mins.values() if "speed" in v and "motor_current" in v]
    f = robust_fit([p[0] for p in pts], [p[1] for p in pts])
    if f:
        out["current_model"] = {"a": round(f[0], 3), "b": round(f[1], 5), "rmse": round(f[2], 3), "minutes": f[3]}
    one = dt.timedelta(minutes=1)
    pairs = [(v["steam_pressure"], mins[m + one]["moisture"]) for m, v in mins.items()
             if "steam_pressure" in v and m + one in mins and "moisture" in mins[m + one]]
    f = robust_fit([p[0] for p in pairs], [p[1] for p in pairs]) if pairs else None
    if f and f[1] < 0:  # only a physical fit: more steam, drier paper
        out["moisture_model"] = {"a": round(f[0], 3), "b": round(f[1], 4), "rmse": round(f[2], 4), "minutes": f[3]}
    return out


async def run(args) -> None:
    pool = await asyncpg.create_pool(os.environ.get("DATABASE_URL", "postgresql://plc:plc@localhost:5432/plc"),
                                     min_size=1, max_size=2)
    store = Store(pool)
    device_id = args.device_id
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=args.days)
    inputs = (await store.load_process_inputs()).get(device_id)
    if inputs is None:
        raise SystemExit(f"device {device_id} not found")
    status = inputs.roles.get("machine_status")
    codes = None
    if status:
        labels = await pool.fetchval("SELECT value_labels::text FROM tags WHERE device_id = $1 AND tag = $2",
                                     device_id, status.tag)
        labels = json.loads(labels or "{}")
        codes = [float(k) for k, lab in labels.items() if status_from_label(lab) == "RUN"]

    if args.fit:
        rules_cfg = await fit(pool, device_id, inputs, codes, start, end)
        cfg = json.loads(await pool.fetchval("SELECT asset_config::text FROM devices WHERE device_id = $1", device_id))
        cfg["rules"] = rules_cfg
        await pool.execute("UPDATE devices SET asset_config = $2::jsonb WHERE device_id = $1", device_id, json.dumps(cfg))
        await pool.execute("NOTIFY config_changed")
        print("fitted:", json.dumps(rules_cfg))
        inputs = (await store.load_process_inputs())[device_id]

    rules = [r for r in await store.load_rules() if r.rule_type == "process" and r.applies_to(device_id)]
    if not rules:
        raise SystemExit("no process rules for this device (assign tag roles in Asset configuration)")
    print("rules:", ", ".join(r.name for r in rules))
    if args.replace:
        res = await pool.execute("DELETE FROM alarms WHERE device_id = $1 AND rule_id = ANY($2) AND (raised_at >= $3 OR cleared_at IS NULL)",
                                 device_id, [r.id for r in rules], start)
        print("removed", res.split()[-1], "process-rule alarms")

    engine = ProcessEngine()
    engine.configure(device_id, inputs)
    alarms: list[dict] = []
    active: dict[int, dict] = {}
    tags = {info.tag: role for role, info in inputs.roles.items()}
    run_codes = set(codes or [])
    values: dict[str, float] = {}
    running = False
    started = time.monotonic()
    chunk = dt.timedelta(hours=6)
    t0 = start
    while t0 < end:
        t1 = min(t0 + chunk, end)
        rows = await pool.fetch(
            "SELECT extract(epoch FROM ts) AS t, tag, value_num FROM telemetry WHERE device_id = $1 AND tag = ANY($2) "
            "AND ts >= $3 AND ts < $4 AND quality < 2 AND value_num IS NOT NULL ORDER BY ts",
            device_id, list(tags), t0, t1,
        )
        i = 0
        while i < len(rows):
            t = float(rows[i]["t"])
            polled_speed = False
            while i < len(rows) and float(rows[i]["t"]) == t:
                role = tags[rows[i]["tag"]]
                v = float(rows[i]["value_num"])
                if role == "machine_status":
                    running = v in run_codes
                else:
                    values[role] = v
                    polled_speed = polled_speed or role == "speed"
                i += 1
            if not status:
                running = values.get("speed", 0) > 0.35 * (inputs.speed_ref or 0)
            if not polled_speed and "speed" in inputs.roles:
                continue
            for tr in engine.evaluate(device_id, rules, t, running, values, lambda rid, _d: rid in active):
                when = dt.datetime.fromtimestamp(t, dt.timezone.utc)
                if tr.action == "raise":
                    active[tr.rule.id] = a = {"rule": tr.rule, "raised_at": when, "cleared_at": None,
                                              "value": tr.value, "explain": tr.explain}
                    alarms.append(a)
                else:
                    active.pop(tr.rule.id)["cleared_at"] = when
        print(f"  {t1:%m-%d %H:%M}  {len(alarms)} alarms", flush=True)
        t0 = t1

    await pool.executemany(
        "INSERT INTO alarms (rule_id, rule_name, device_id, tag, severity, message, trigger_value, context, explain, "
        "raised_at, cleared_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11)",
        [(a["rule"].id, a["rule"].name, device_id, a["rule"].tag, a["rule"].severity, a["explain"]["what"], a["value"],
          a["rule"].guidance, json.dumps(a["explain"]), a["raised_at"], a["cleared_at"]) for a in alarms],
    )
    counts: dict[str, int] = {}
    for a in alarms:
        counts[a["rule"].name] = counts.get(a["rule"].name, 0) + 1
    print(f"done in {time.monotonic() - started:.0f} s: {len(alarms)} alarms ({len(active)} still active)")
    for name, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {n:>4} × {name}")
    await pool.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("device_id")
    p.add_argument("--days", type=float, default=7)
    p.add_argument("--fit", action="store_true", help="fit the coefficients from the stored data first")
    p.add_argument("--replace", action="store_true", help="delete stored process-rule alarms in the window first")
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
