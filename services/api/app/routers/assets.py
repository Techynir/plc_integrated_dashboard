"""Asset analytics: the numbers behind the Live, Performance, Quality, Health and Data-quality
screens, computed from the stored 1-second telemetry. Pure maths lives in app/analytics.py.

Tags are found by role (Asset configuration): machine_status, speed, motor_current,
steam_pressure, moisture, vibration. A screen whose roles are not assigned says so instead of
guessing.
"""

import datetime as dt
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import analytics as an
from .. import db
from ..config import settings
from ..deps import CurrentUser, admin, operator, viewer

router = APIRouter(prefix="/assets", tags=["assets"])

ROLES = ("machine_status", "speed", "motor_current", "steam_pressure", "moisture", "vibration")
STOP_REASONS = ["Unclassified", "Web break", "Grade change", "Planned maintenance", "Felt / wire change",
                "Steam / utility issue", "Electrical trip", "Mechanical fault", "No material", "Other"]


def tz() -> ZoneInfo:
    return ZoneInfo(settings.plant_tz)


def epoch(t: dt.datetime) -> float:
    return t.timestamp()


def iso(t: float | None) -> str | None:
    return None if t is None else dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()


# ---------------------------------------------------------------- loading


async def load_asset(device_id: str) -> dict:
    row = await db.pool().fetchrow(
        "SELECT device_id, name, site, line, expected_interval_s, online, status, last_seen, created_at, "
        "asset_type, asset_config, simulated FROM devices WHERE device_id = $1",
        device_id,
    )
    if row is None:
        raise HTTPException(404, "Asset not found")
    tags = await db.pool().fetch(
        "SELECT tag, coalesce(nullif(display_name, ''), tag) AS label, unit, decimals, data_type, min_value, "
        "max_value, limit_dir, warn_limit, crit_limit, role, value_labels, suppress_when_stopped, valid_min, "
        "valid_max FROM tags WHERE device_id = $1",
        device_id,
    )
    asset = dict(row)
    asset["tags"] = {t["tag"]: dict(t) for t in tags}
    asset["roles"] = {t["role"]: dict(t) for t in tags if t["role"]}
    cfg = asset["asset_config"] or {}
    poll_ms = cfg.get("poll_interval_ms")
    asset["interval_s"] = (poll_ms / 1000.0) if isinstance(poll_ms, (int, float)) and poll_ms > 0 else float(
        asset["expected_interval_s"] or 1.0
    )
    # Gateways often deliver in bursts (e.g. 5 records every 5 s), so a few seconds of silence is
    # normal: data counts as lost only after comms_timeout_s (default 15 s).
    timeout = cfg.get("comms_timeout_s")
    asset["gap_s"] = max(3 * asset["interval_s"], float(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else 15.0)
    return asset


def window(asset: dict, hours: float, end: dt.datetime | None) -> tuple[float, float, bool]:
    """[start, end) in epoch seconds. Without an explicit end, a device that has been silent for
    longer than the window is shown up to its last data rather than as an empty window."""
    now = dt.datetime.now(dt.timezone.utc)
    anchored = False
    if end is None:
        end = now
        last = asset["last_seen"]
        if last is not None and (now - last).total_seconds() > hours * 3600:
            end, anchored = last + dt.timedelta(seconds=1), True
    elif end.tzinfo is None:
        end = end.replace(tzinfo=dt.timezone.utc)
    e = epoch(end)
    return e - hours * 3600, e, anchored


async def series(device_id: str, tag: str | None, start: float, end: float) -> list[tuple[float, float]]:
    if not tag:
        return []
    rows = await db.pool().fetch(
        "SELECT extract(epoch FROM ts) AS t, value_num AS v FROM telemetry "
        "WHERE device_id = $1 AND tag = $2 AND ts >= to_timestamp($3) AND ts < to_timestamp($4) "
        "AND value_num IS NOT NULL AND quality < 2 ORDER BY ts",
        device_id, tag, start, end,
    )
    return [(float(r["t"]), float(r["v"])) for r in rows]


def role_tag(asset: dict, role: str) -> dict | None:
    return asset["roles"].get(role)


def speed_threshold(asset: dict) -> float:
    """Speed above which the machine counts as running (asset_config.running_speed_min, else half
    the normal minimum speed, else anything above zero)."""
    cfg = asset["asset_config"] or {}
    sp = role_tag(asset, "speed")
    if isinstance(cfg.get("running_speed_min"), (int, float)):
        return float(cfg["running_speed_min"])
    if sp and sp["min_value"] is not None and sp["min_value"] > 0:
        return 0.5 * sp["min_value"]
    return 0.0


async def machine_segments(asset: dict, start: float, end: float) -> tuple[list[an.Segment], set | None, str]:
    """Machine state. asset_config.running_source: "status" (status register), "speed"
    (speed above a threshold) or "auto" (default): the status register, unless it contradicts the
    speed (says stopped while the machine is at speed), in which case speed is used and the
    returned source says so, so the screens can warn about it."""
    status, sp = role_tag(asset, "machine_status"), role_tag(asset, "speed")
    mode = (asset["asset_config"] or {}).get("running_source", "auto")
    status_samples = speed_samples = None
    codes = None
    if status and mode in ("auto", "status"):
        codes = an.running_codes(status["value_labels"])
        status_samples = [(t, an.is_running(v, codes)) for t, v in await series(asset["device_id"], status["tag"], start, end)]
    if sp and (mode == "speed" or (mode == "auto" and (status_samples is None or status_samples))):
        thr = speed_threshold(asset)
        speed_samples = [(t, v > thr, v) for t, v in await series(asset["device_id"], sp["tag"], start, end)]

    conflict = False
    if mode == "auto" and status_samples and speed_samples:
        stopped_frac = sum(1 for _, r in status_samples if not r) / len(status_samples)
        fast_frac = sum(1 for _, r, _ in speed_samples if r) / len(speed_samples)
        conflict = stopped_frac > 0.9 and fast_frac > 0.5  # "stopped" nearly always, yet mostly at speed
    if status_samples is not None and not conflict and mode != "speed":
        samples, source = status_samples, f"{status['tag']} (status register)"
    elif speed_samples is not None:
        samples = [(t, r) for t, r, _ in speed_samples]
        thr = speed_threshold(asset)
        source = (f"CONFLICT: {status['tag']} reports stopped while {sp['tag']} is above {thr:g} {sp['unit']}; "
                  f"running state taken from speed" if conflict else f"{sp['tag']} > {thr:g} {sp['unit']}")
    elif status_samples is not None:
        samples, source = status_samples, f"{status['tag']} (status register)"
    else:
        any_tag = next(iter(asset["tags"]), None)
        samples = [(t, True) for t, _ in await series(asset["device_id"], any_tag, start, end)]
        source = "data received (no status or speed tag assigned)"
    return an.state_segments(samples, start, end, asset["gap_s"]), codes, source


def tag_public(t: dict | None) -> dict | None:
    if not t:
        return None
    return {k: t[k] for k in ("tag", "label", "unit", "decimals", "min_value", "max_value", "limit_dir",
                              "warn_limit", "crit_limit", "value_labels")}


def window_info(start: float, end: float, anchored: bool) -> dict:
    return {"start": iso(start), "end": iso(end), "anchored": anchored}


# ---------------------------------------------------------------- summary (overview tiles + asset cards)


@router.get("/{device_id}/summary")
async def summary(device_id: str, hours: float = Query(24, gt=0, le=24 * 31), end: dt.datetime | None = None,
                  _: CurrentUser = Depends(viewer)) -> dict:
    asset = await load_asset(device_id)
    start, stop, anchored = window(asset, hours, end)
    segs, _, source = await machine_segments(asset, start, stop)
    tot = an.totals(segs)
    first = await db.pool().fetchval(
        "SELECT extract(epoch FROM min(ts)) FROM telemetry WHERE device_id = $1 AND ts >= to_timestamp($2) "
        "AND ts < to_timestamp($3)", device_id, start, stop,
    )
    covered = max(0.0, stop - max(start, float(first))) if first else 0.0
    records = await db.pool().fetchval(
        "SELECT count(*) FROM raw_messages WHERE device_id = $1 AND status = 'ok' AND ts >= to_timestamp($2) "
        "AND ts < to_timestamp($3)", device_id, start, stop,
    )
    known = tot["run"] + tot["stop"]
    alarms = await db.pool().fetchrow(
        "SELECT count(*) FILTER (WHERE cleared_at IS NULL) AS active, "
        "count(*) FILTER (WHERE acked_at IS NULL AND cleared_at IS NULL) AS unacked, "
        "count(*) FILTER (WHERE cleared_at IS NULL AND severity = 'critical') AS critical "
        "FROM alarms WHERE device_id = $1", device_id,
    )
    health = await compute_health(asset, None)
    return {
        "window": window_info(start, stop, anchored),
        "state_source": source,
        "totals": tot,
        "availability": an.availability(tot),
        "stops": len(an.stop_events(segs, stop)),
        "completeness": (known / covered) if covered > 0 else None,
        "records": records,
        "health": {"score": health["overall"], "state": health["state"]},
        "alarms": dict(alarms),
        "roles": {r: tag_public(role_tag(asset, r)) for r in ROLES},
    }


# ---------------------------------------------------------------- performance & downtime


@router.get("/{device_id}/performance")
async def performance(device_id: str, hours: float = Query(24, gt=0, le=24 * 7), end: dt.datetime | None = None,
                      _: CurrentUser = Depends(viewer)) -> dict:
    asset = await load_asset(device_id)
    start, stop, anchored = window(asset, hours, end)
    segs, _, source = await machine_segments(asset, start, stop)
    tot = an.totals(segs)
    stops = an.stop_events(segs, stop)
    reasons = {
        round(epoch(r["started_at"]), 3): r["reason"]
        for r in await db.pool().fetch(
            "SELECT started_at, reason FROM stoppage_reasons WHERE device_id = $1 AND started_at >= to_timestamp($2)",
            device_id, start - 3600,
        )
    }
    zone = tz()
    for s in stops:
        s["reason"] = reasons.get(round(s["start"], 3), "Unclassified")
        s["shift"] = an.shift_of(s["start"], zone)

    speed_tag, moist_tag = role_tag(asset, "speed"), role_tag(asset, "moisture")
    speed_run = an.in_states(await series(device_id, speed_tag and speed_tag["tag"], start, stop), segs, {"run"})
    moist_run = an.in_states(await series(device_id, moist_tag and moist_tag["tag"], start, stop), segs, {"run"})
    avg_speed = an.mean([v for _, v in speed_run])
    cfg = asset["asset_config"] or {}
    target = cfg.get("speed_target")
    rated_by_tag = {
        k: float(v) for k, v in (cfg.get("rated") or {}).items() if isinstance(v, (int, float)) and float(v) != 0
    }
    if speed_tag and isinstance(target, (int, float)) and float(target) != 0:
        rated_by_tag.setdefault(speed_tag["tag"], float(target))
    rated_actual = []
    for name, rated in rated_by_tag.items():
        spec = asset["tags"].get(name)
        if not spec or spec.get("data_type") != "number" or spec.get("role") == "machine_status":
            continue
        if speed_tag and name == speed_tag["tag"]:
            actual = avg_speed
        else:
            pts = an.in_states(await series(device_id, name, start, stop), segs, {"run"})
            actual = an.mean([v for _, v in pts])
        if actual is None:
            continue
        # 1 when actual equals rated; the same gap above or below rated scores the same.
        ratio = min(actual / rated, rated / actual)
        rated_actual.append({
            "tag": name, "label": spec["label"], "unit": spec["unit"] or "",
            "actual": actual, "rated": rated, "ratio": ratio,
        })
    performance = an.mean([c["ratio"] for c in rated_actual])

    # shifts
    pieces = an.split_segments(segs, an.shift_boundaries(start, stop, zone))
    shifts = {name: {"run": 0.0, "stop": 0.0, "comms": 0.0, "stops": 0, "speed": [], "moist_in": 0, "moist_n": 0,
                     "alarms": 0} for name, _, _ in an.SHIFTS}
    for p in pieces:
        shifts[an.shift_of((p.start + p.end) / 2, zone)][p.state] += p.seconds
    for s in stops:
        shifts[s["shift"]]["stops"] += 1
    for t, v in speed_run:
        shifts[an.shift_of(t, zone)]["speed"].append(v)
    lo = moist_tag and moist_tag["min_value"]
    hi = moist_tag and moist_tag["max_value"]
    for t, v in moist_run:
        sh = shifts[an.shift_of(t, zone)]
        sh["moist_n"] += 1
        if lo is not None and hi is not None and lo <= v <= hi:
            sh["moist_in"] += 1
    for r in await db.pool().fetch(
        "SELECT extract(epoch FROM raised_at) AS t FROM alarms WHERE device_id = $1 AND raised_at >= to_timestamp($2) "
        "AND raised_at < to_timestamp($3)", device_id, start, stop,
    ):
        shifts[an.shift_of(float(r["t"]), zone)]["alarms"] += 1
    shift_rows = []
    for name, h0, h1 in an.SHIFTS:
        sh = shifts[name]
        known = sh["run"] + sh["stop"]
        shift_rows.append({
            "shift": name, "hours": f"{h0:02d}–{h1:02d}", "run_s": sh["run"], "stop_s": sh["stop"],
            "comms_s": sh["comms"], "availability": sh["run"] / known if known else None, "stops": sh["stops"],
            "avg_speed": an.mean(sh["speed"]),
            "moisture_in_range": sh["moist_in"] / sh["moist_n"] if sh["moist_n"] and lo is not None else None,
            "alarms": sh["alarms"],
        })

    return {
        "window": window_info(start, stop, anchored),
        "state_source": source,
        "totals": tot,
        "availability": an.availability(tot),
        "stops": stops,
        "longest_stop_s": max((s["seconds"] for s in stops), default=None),
        "avg_speed_running": avg_speed,
        "speed_target": target,
        "performance": performance,
        "rated_actual": rated_actual,
        "speed_tag": tag_public(speed_tag),
        "segments": [{"state": s.state, "start": s.start, "end": s.end} for s in segs],
        "shifts": shift_rows,
        "reasons": STOP_REASONS,
        "timezone": settings.plant_tz,
    }


class ReasonIn(BaseModel):
    reason: str = Field(min_length=1, max_length=80)


@router.put("/{device_id}/stoppages/{started_at}")
async def set_stop_reason(device_id: str, started_at: float, body: ReasonIn, user: CurrentUser = Depends(operator)) -> dict:
    await load_asset(device_id)
    await db.pool().execute(
        """
        INSERT INTO stoppage_reasons (device_id, started_at, reason, set_by) VALUES ($1, to_timestamp($2), $3, $4)
        ON CONFLICT (device_id, started_at) DO UPDATE SET reason = EXCLUDED.reason, set_by = EXCLUDED.set_by,
            set_at = now()
        """,
        device_id, started_at, body.reason, user.email,
    )
    await db.audit(user.email, "stoppage.reason", device_id, {"started_at": iso(started_at), "reason": body.reason})
    return {"ok": True}


# ---------------------------------------------------------------- process quality


@router.get("/{device_id}/quality")
async def quality(device_id: str, hours: float = Query(8, gt=0, le=24 * 7), end: dt.datetime | None = None,
                  _: CurrentUser = Depends(viewer)) -> dict:
    asset = await load_asset(device_id)
    moist, steam = role_tag(asset, "moisture"), role_tag(asset, "steam_pressure")
    start, stop, anchored = window(asset, hours, end)
    base = {"window": window_info(start, stop, anchored), "moisture_tag": tag_public(moist),
            "steam_tag": tag_public(steam)}
    if not moist:
        return {**base, "missing": "Assign the moisture role to a tag in Asset configuration."}
    segs, _, _ = await machine_segments(asset, start, stop)
    m_run = an.in_states(await series(device_id, moist["tag"], start, stop), segs, {"run"})
    values = [v for _, v in m_run]
    min_count = max(1, int(30 / asset["interval_s"]))
    mm = an.minute_means(m_run, min_count)
    limits = an.imr_limits([v for _, v in mm])
    if limits and limits["sigma"] <= 0:
        limits = {**limits, "no_variation": True}  # constant signal: no control limits to speak of
    flags = (an.spc_flags([v for _, v in mm], limits["cl"], limits["ucl"], limits["lcl"])
             if limits and not limits.get("no_variation") else [0] * len(mm))
    lsl, usl = moist["min_value"], moist["max_value"]
    cap = an.capability(values, lsl, usl)

    lo_candidates = [min(values)] if values else []
    hi_candidates = [max(values)] if values else []
    if lsl is not None:
        lo_candidates.append(lsl)
    if usl is not None:
        hi_candidates.append(usl)
    hist = []
    if values:
        lo, hi = min(lo_candidates), max(hi_candidates)
        pad = (hi - lo) * 0.08 or 0.05
        hist = an.histogram(values, lo - pad, hi + pad, bins=28)

    scatter, reg = [], None
    if steam:
        s_run = dict(an.minute_means(an.in_states(await series(device_id, steam["tag"], start, stop), segs, {"run"}),
                                     min_count))
        pairs = [(s_run[t], v, t) for t, v in mm if t in s_run]
        scatter = [{"t": t, "x": x, "y": y} for x, y, t in pairs]
        reg = an.linreg([p[0] for p in pairs], [p[1] for p in pairs])

    return {
        **base,
        "minute_means": [{"t": t, "v": v, "flag": f} for (t, v), f in zip(mm, flags or [0] * len(mm))],
        "limits": limits,
        "spec": {"lsl": lsl, "usl": usl},
        "capability": cap,
        "out_of_control": sum(1 for f in flags if f == 2),
        "run_rule": sum(1 for f in flags if f == 1),
        "histogram": hist,
        "scatter": scatter,
        "regression": reg,
    }


# ---------------------------------------------------------------- asset health


async def running_basis(asset: dict, since: float, until: float) -> str:
    """Which signal says "running" for minute rollups: "status", "speed" or "none". Mirrors
    machine_segments' rules (including the auto conflict check), using the cheap 1-minute rollup."""
    status, sp = role_tag(asset, "machine_status"), role_tag(asset, "speed")
    mode = (asset["asset_config"] or {}).get("running_source", "auto")
    if mode == "speed" or (mode != "status" and sp and not status):
        return "speed" if sp else "none"
    if not status:
        return "none"
    if mode == "status" or not sp:
        return "status"
    codes = an.running_codes(status["value_labels"])
    row = await db.pool().fetchrow(
        """
        SELECT avg(CASE WHEN s.tag = $2 THEN (CASE WHEN $4::float8[] IS NULL THEN (s.max_val <= 0)::int
                                              ELSE (NOT s.max_val = ANY($4))::int END) END) AS stopped,
               avg(CASE WHEN s.tag = $3 THEN (s.min_val > $5)::int END) AS fast
        FROM telemetry_1m s WHERE s.device_id = $1 AND s.tag IN ($2, $3)
          AND s.bucket >= to_timestamp($6) AND s.bucket < to_timestamp($7)
        """,
        asset["device_id"], status["tag"], sp["tag"], sorted(codes) if codes else None, speed_threshold(asset), since, until,
    )
    conflict = row and row["stopped"] is not None and row["fast"] is not None and row["stopped"] > 0.9 and row["fast"] > 0.5
    return "speed" if conflict else "status"


async def running_minute_means(asset: dict, tags: list[str], since: float, until: float) -> list[dict]:
    """1-minute means (from the rollup) for minutes the machine was running the whole minute."""
    status = role_tag(asset, "machine_status")
    params: list = [asset["device_id"], tags, since, until]
    run_filter = ""
    basis = await running_basis(asset, since, until)
    if basis == "speed":
        params += [role_tag(asset, "speed")["tag"], speed_threshold(asset)]
        run_filter = ("AND EXISTS (SELECT 1 FROM telemetry_1m s WHERE s.device_id = v.device_id AND "
                      "s.bucket = v.bucket AND s.tag = $5 AND s.min_val > $6)")
    elif basis == "status":
        codes = an.running_codes(status["value_labels"])
        params.append(status["tag"])
        if codes:
            params.append(sorted(codes))
            run_filter = (f"AND EXISTS (SELECT 1 FROM telemetry_1m s WHERE s.device_id = v.device_id AND "
                          f"s.bucket = v.bucket AND s.tag = ${len(params) - 1} AND s.min_val = ANY(${len(params)}) "
                          f"AND s.max_val = ANY(${len(params)}))")
        else:
            run_filter = (f"AND EXISTS (SELECT 1 FROM telemetry_1m s WHERE s.device_id = v.device_id AND "
                          f"s.bucket = v.bucket AND s.tag = ${len(params)} AND s.min_val > 0)")
    rows = await db.pool().fetch(
        f"SELECT extract(epoch FROM v.bucket) AS t, v.tag, v.sum_val / nullif(v.cnt, 0) AS v FROM telemetry_1m v "
        f"WHERE v.device_id = $1 AND v.tag = ANY($2) AND v.bucket >= to_timestamp($3) AND v.bucket < to_timestamp($4) "
        f"{run_filter} ORDER BY v.bucket",
        *params,
    )
    by_t: dict[float, dict] = {}
    for r in rows:
        if r["v"] is not None:
            by_t.setdefault(float(r["t"]), {"t": float(r["t"])})[r["tag"]] = float(r["v"])
    return [m for m in by_t.values() if all(k in m for k in tags)]


async def compute_health(asset: dict, end: dt.datetime | None) -> dict:
    dev = asset["device_id"]
    _, stop, anchored = window(asset, 1, end)
    vib, cur, spd, steam = (role_tag(asset, r) for r in ("vibration", "motor_current", "speed", "steam_pressure"))
    if end is None:
        # Score the most recent running minute in the last 12 h, so a short comms drop or a stop
        # does not blank the score; anchored says the minute is not the current one.
        recent, _, _ = await machine_segments(asset, stop - 12 * 3600, stop)
        last_run = next((s for s in reversed(recent) if s.state == "run" and s.seconds >= 60), None)
        if last_run is not None and last_run.end < stop - 1:
            stop, anchored = last_run.end, True
    segs, _, _ = await machine_segments(asset, stop - 60, stop)

    def last_mean(s, running=True):
        vals = [v for _, v in (an.in_states(s, segs, {"run"}) if running else s)]
        return an.mean(vals)

    v_now = last_mean(await series(dev, vib and vib["tag"], stop - 60, stop)) if vib else None
    p_now = last_mean(await series(dev, steam and steam["tag"], stop - 60, stop), running=False) if steam else None
    c_now = last_mean(await series(dev, cur and cur["tag"], stop - 60, stop)) if cur else None
    s_now = last_mean(await series(dev, spd and spd["tag"], stop - 60, stop)) if spd else None

    model, residual = None, None
    if cur and spd:
        pts = await running_minute_means(asset, [spd["tag"], cur["tag"]], stop - 7 * 86400, stop)
        model = an.linreg([p[spd["tag"]] for p in pts], [p[cur["tag"]] for p in pts])
        if model and c_now is not None and s_now is not None:
            residual = c_now - (model["m"] * s_now + model["b"])
    sigma = max(model["rmse"], 0.5) if model else 1.0

    parts = {
        "bearing": (an.limit_score(v_now, vib) if vib else None, 0.4),
        "drive": (an.residual_score(residual, sigma), 0.3),
        "dryer": (an.limit_score(p_now, steam) if steam else None, 0.3),
    }
    overall = an.overall_score(parts)
    return {
        "anchored": anchored, "end": iso(stop),
        "overall": overall, "state": an.health_state(overall),
        "components": {k: v[0] for k, v in parts.items()},
        "inputs": {"vibration": v_now, "steam_pressure": p_now, "current": c_now, "speed": s_now,
                   "residual": residual, "sigma": sigma},
        "model": model,
    }


@router.get("/{device_id}/health")
async def health(device_id: str, end: dt.datetime | None = None, _: CurrentUser = Depends(viewer)) -> dict:
    asset = await load_asset(device_id)
    h = await compute_health(asset, end)
    stop = dt.datetime.fromisoformat(h["end"]).timestamp()
    vib, cur, spd = (role_tag(asset, r) for r in ("vibration", "motor_current", "speed"))

    daily, projection, short = [], None, None
    if vib:
        mm = await running_minute_means(asset, [vib["tag"]], stop - 30 * 86400, stop)
        zone = tz()
        days: dict[dt.date, list[float]] = {}
        for m in mm:
            days.setdefault(dt.datetime.fromtimestamp(m["t"], zone).date(), []).append(m[vib["tag"]])
        today = dt.datetime.fromtimestamp(stop, zone).date()
        daily = [{"day": d.isoformat(), "offset": (d - today).days, "mean": an.mean(v), "minutes": len(v)}
                 for d, v in sorted(days.items())]
        if len(daily) >= 3:
            reg = an.linreg([d["offset"] for d in daily], [d["mean"] for d in daily])
            if reg:
                warn = vib["warn_limit"]
                days_to = ((warn - reg["b"]) / reg["m"]) if (warn is not None and reg["m"] > 0) else None
                projection = {**reg, "per_month": reg["m"] * 30, "days_to_warning": days_to}
        segs, _, _ = await machine_segments(asset, stop - 300, stop)
        s = an.in_states(await series(device_id, vib["tag"], stop - 300, stop), segs, {"run"})
        if len(s) > 30:
            reg = an.linreg([(t - stop) / 60 for t, _ in s], [v for _, v in s])
            cur_v = an.mean([v for _, v in s[-30:]])
            warn = vib["warn_limit"]
            mins = ((warn - cur_v) / reg["m"]) if reg and warn is not None and reg["m"] > 0 and cur_v < warn else None
            short = {"slope_per_min": reg["m"] if reg else None, "current": cur_v, "minutes_to_warning": mins}

    load_pts, vib_pts, vib_reg = [], [], None
    if spd and (cur or vib):
        tags = [spd["tag"]] + [t["tag"] for t in (cur, vib) if t]
        mm = await running_minute_means(asset, tags, stop - 86400, stop)
        model = h["model"]
        for m in mm:
            if cur:
                exp = model["m"] * m[spd["tag"]] + model["b"] if model else None
                load_pts.append({"t": m["t"], "x": m[spd["tag"]], "y": m[cur["tag"]],
                                 "residual": (m[cur["tag"]] - exp) if exp is not None else None})
        if vib:
            vib_reg = an.linreg([m[spd["tag"]] for m in mm], [m[vib["tag"]] for m in mm])
            for m in mm:
                exp = vib_reg["m"] * m[spd["tag"]] + vib_reg["b"] if vib_reg else None
                vib_pts.append({"t": m["t"], "x": m[spd["tag"]], "y": m[vib["tag"]],
                                "above": exp is not None and m[vib["tag"]] - exp > 2 * max(vib_reg["rmse"], 0.05)})
    return {
        **h,
        "tags": {r: tag_public(role_tag(asset, r)) for r in ("vibration", "motor_current", "speed", "steam_pressure")},
        "daily_vibration": daily,
        "projection": projection,
        "short_term": short,
        "load_signature": load_pts,
        "vibration_vs_speed": vib_pts,
        "vibration_regression": vib_reg,
    }


# ---------------------------------------------------------------- data quality & link


DEFAULT_CHECKLIST = [
    ("Register map confirmed", "Each register address and data type matches the PLC programmer's map.", "open"),
    ("Float32 byte and word order", "Verify one known value end to end (e.g. machine speed on the HMI vs dashboard).", "open"),
    ("Gateway / PLC IP addresses", "Record the PLC and gateway IP addresses in Asset configuration.", "open"),
    ("Status codes", "Status register codes and their meaning (e.g. 0 = Stopped, 1 = Running, 2 = Fault).", "open"),
    ("Timestamp source", "The gateway sends no timestamp; the server's receive time is used.", "assumed"),
    ("Failure handling", "Bad reads are rejected and gaps are shown as gaps, never as zero.", "done"),
    ("Alarm limits", "Normal, warning and critical limits confirmed with the machine OEM / process team.", "open"),
]


async def checklist(device_id: str) -> list[dict]:
    rows = await db.pool().fetch(
        "SELECT id, title, detail, status, updated_by, updated_at FROM commissioning_items WHERE device_id = $1 "
        "ORDER BY position, id", device_id,
    )
    if not rows:
        async with db.pool().acquire() as conn, conn.transaction():
            for i, (title, detail, status) in enumerate(DEFAULT_CHECKLIST):
                await conn.execute(
                    "INSERT INTO commissioning_items (device_id, position, title, detail, status) VALUES ($1, $2, $3, $4, $5)",
                    device_id, i, title, detail, status,
                )
        return await checklist(device_id)
    return [{**dict(r), "updated_at": r["updated_at"].isoformat()} for r in rows]


@router.get("/{device_id}/data-quality")
async def data_quality(device_id: str, hours: float = Query(24, gt=0, le=24 * 7), end: dt.datetime | None = None,
                       _: CurrentUser = Depends(viewer)) -> dict:
    asset = await load_asset(device_id)
    start, stop, anchored = window(asset, hours, end)
    interval = asset["interval_s"]
    segs, _, _ = await machine_segments(asset, start, stop)
    first = next((s.start for s in segs if s.state != "comms"), None)
    covered = (stop - first) if first is not None else 0.0
    expected = covered / interval if covered else 0

    counts = await db.pool().fetch(
        "SELECT tag, count(*) FILTER (WHERE quality < 2 AND value_num IS NOT NULL) AS good, "
        "count(*) FILTER (WHERE quality = 2) AS bad FROM telemetry WHERE device_id = $1 "
        "AND ts >= to_timestamp($2) AND ts < to_timestamp($3) GROUP BY tag",
        device_id, start, stop,
    )
    count_by = {r["tag"]: r for r in counts}
    regs = await db.pool().fetch(
        "SELECT r.address, r.tag, r.data_type, l.value_num, l.value_text, l.quality, l.ts FROM register_map r "
        "LEFT JOIN tag_latest l USING (device_id, tag) WHERE r.device_id = $1 ORDER BY r.address", device_id,
    )
    tags_in_order = [r["tag"] for r in regs] or sorted(asset["tags"])
    per_tag = []
    for tag in tags_in_order:
        c = count_by.get(tag)
        good, bad = (c["good"], c["bad"]) if c else (0, 0)
        per_tag.append({
            "tag": tag, "label": asset["tags"].get(tag, {}).get("label", tag),
            "good": good, "bad": bad, "missing": max(0, round(expected) - good - bad),
            "completeness": min(1.0, good / expected) if expected else None,
        })

    hour_start = max(stop - 3600, first or stop)
    status = role_tag(asset, "machine_status") or next(iter(asset["roles"].values()), None)
    probe_tag = status["tag"] if status else (tags_in_order[0] if tags_in_order else None)
    hour_samples = await series(device_id, probe_tag, hour_start, stop)
    hour_expected = (stop - hour_start) / interval if stop > hour_start else 0
    recent = await series(device_id, probe_tag, stop - 900, stop)
    gaps = [(t1, (t1 - t0) * 1000) for (t0, _), (t1, _) in zip(recent, recent[1:])]
    diffs = sorted(g for _, g in gaps)

    def pct(p):
        return diffs[min(len(diffs) - 1, int(len(diffs) * p))] if diffs else None

    pauses = [g for g in diffs if g >= 500]  # gaps between bursts
    burst = None
    if pauses and len(pauses) < len(diffs) * 0.5:  # most gaps are tiny: the gateway sends in bursts
        period_ms = pauses[len(pauses) // 2]
        burst = {"period_s": period_ms / 1000, "records": round(len(recent) / (len(pauses) + 1), 1)}

    events = [e for e in an.comms_events(segs, stop, min_s=asset["gap_s"]) if first is not None and e["start"] >= first]
    for e in events:
        e["missing_records"] = round(e["seconds"] / interval)
    bad_reads = await db.pool().fetchval(  # messages with a rejected (out-of-range) read
        "SELECT count(DISTINCT ts) FROM telemetry WHERE device_id = $1 AND quality = 2 "
        "AND ts >= to_timestamp($2) AND ts < to_timestamp($3)", device_id, start, stop,
    )
    return {
        "window": window_info(start, stop, anchored),
        "interval_s": interval,
        "expected_per_tag": round(expected),
        "poll_success_1h": (len(hour_samples) / hour_expected) if hour_expected else None,
        # time without data after the first sample in the window (before it, the device simply had no data yet)
        "comms_lost_s": sum(s.seconds for s in segs if s.state == "comms" and first is not None and s.start >= first),
        "comms_events": events,
        "bad_reads": bad_reads,
        "completeness": an.mean([t["completeness"] for t in per_tag if t["completeness"] is not None]),
        "update_interval": {"p50_ms": pct(0.5), "p95_ms": pct(0.95), "series": gaps[-900:], "burst": burst},
        "comms_timeout_s": asset["gap_s"],
        "per_tag": per_tag,
        "registers": [{**dict(r), "ts": r["ts"].isoformat() if r["ts"] else None} for r in regs],
        "byte_order": (asset["asset_config"] or {}).get("byte_order", "ABCD"),
        "checklist": await checklist(device_id),
    }


class ChecklistItemIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    detail: str | None = Field(default=None, max_length=1000)
    status: Literal["open", "assumed", "done"] | None = None


@router.post("/{device_id}/checklist", status_code=201)
async def add_check(device_id: str, body: ChecklistItemIn, user: CurrentUser = Depends(admin)) -> dict:
    await load_asset(device_id)
    row = await db.pool().fetchrow(
        "INSERT INTO commissioning_items (device_id, position, title, detail, status, updated_by) "
        "VALUES ($1, (SELECT coalesce(max(position), 0) + 1 FROM commissioning_items WHERE device_id = $1), "
        "$2, $3, $4, $5) RETURNING id",
        device_id, body.title or "New item", body.detail or "", body.status or "open", user.email,
    )
    return {"id": row["id"]}


@router.patch("/{device_id}/checklist/{item_id}")
async def update_check(device_id: str, item_id: int, body: ChecklistItemIn, user: CurrentUser = Depends(admin)) -> dict:
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(400, "Nothing to update")
    sets = ", ".join(f"{k} = ${i}" for i, k in enumerate(changes, start=3))
    res = await db.pool().execute(
        f"UPDATE commissioning_items SET {sets}, updated_by = ${len(changes) + 3}, updated_at = now() "
        f"WHERE device_id = $1 AND id = $2", device_id, item_id, *changes.values(), user.email,
    )
    if res == "UPDATE 0":
        raise HTTPException(404, "Checklist item not found")
    await db.audit(user.email, "checklist.update", device_id, {"item": item_id, **changes})
    return {"ok": True}


@router.delete("/{device_id}/checklist/{item_id}", status_code=204)
async def delete_check(device_id: str, item_id: int, user: CurrentUser = Depends(admin)) -> None:
    await db.pool().execute("DELETE FROM commissioning_items WHERE device_id = $1 AND id = $2", device_id, item_id)
    await db.audit(user.email, "checklist.delete", device_id, {"item": item_id})


# ---------------------------------------------------------------- trend statistics


@router.get("/{device_id}/stats")
async def stats(device_id: str, tags: str, start: dt.datetime = Query(alias="from"), stop: dt.datetime = Query(alias="to"),
                _: CurrentUser = Depends(viewer)) -> dict:
    asset = await load_asset(device_id)
    names = [t for t in tags.split(",") if t in asset["tags"]][:20]
    s0, s1 = start.timestamp(), stop.timestamp()
    if s1 <= s0 or s1 - s0 > 7 * 86400:
        raise HTTPException(400, "Range must be positive and at most 7 days")
    segs, _, _ = await machine_segments(asset, s0, s1)
    first = next((s.start for s in segs if s.state != "comms"), None)
    expected = ((s1 - first) / asset["interval_s"]) if first is not None else 0
    out = []
    for name in names:
        t = asset["tags"][name]
        all_vals = await series(device_id, name, s0, s1)
        running_only = bool(t["suppress_when_stopped"]) and role_tag(asset, "machine_status") is not None
        vals = [v for _, v in (an.in_states(all_vals, segs, {"run"}) if running_only else all_vals)]
        lo, hi = t["min_value"], t["max_value"]
        in_normal = (sum(1 for v in vals if lo <= v <= hi) / len(vals)) if vals and lo is not None and hi is not None else None
        out.append({
            "tag": name, "label": t["label"], "unit": t["unit"], "decimals": t["decimals"], "running_only": running_only,
            "min": min(vals) if vals else None, "avg": an.mean(vals), "max": max(vals) if vals else None,
            "sd": an.stdev(vals), "in_normal": in_normal,
            "valid": min(1.0, len(all_vals) / expected) if expected else None, "samples": len(all_vals),
        })
    return {"tags": out, "from": iso(s0), "to": iso(s1)}
