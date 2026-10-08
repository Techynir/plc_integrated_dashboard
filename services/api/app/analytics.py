"""Pure analytics used by the asset screens. No I/O: inputs are plain lists of samples.

Times are epoch seconds (float). A "sample" is (t, value). Machine state is derived from the
status tag: each sample holds until the next one, but never longer than ``gap_s``; any time not
covered by a sample is "comms" (no data), so communication loss is never counted as running or
stopped.
"""

import bisect
import datetime as dt
import math
import re
import statistics
from dataclasses import dataclass
from zoneinfo import ZoneInfo

SHIFTS = (("A", 6, 18), ("B", 18, 6))  # two 12-hour shifts; the machine runs 24/7

# keyword -> machine state, same vocabulary as the ingestor's status_from_label
_STATUS_WORDS = [
    ("fault", "FAULT"), ("error", "FAULT"), ("alarm", "FAULT"), ("trip", "FAULT"),
    ("maint", "MAINT"), ("idle", "IDLE"), ("standby", "IDLE"), ("ready", "IDLE"),
    ("stop", "STOP"), ("halt", "STOP"), ("off", "STOP"), ("run", "RUN"), ("on", "RUN"),
]


def status_from_label(label: str | None) -> str | None:
    if not label:
        return None
    words = re.findall(r"[a-z]+", label.lower())
    for keyword, status in _STATUS_WORDS:
        if any(w.startswith(keyword) for w in words):
            return status
    return None


def running_codes(value_labels: dict | None) -> set[float] | None:
    """Status codes that mean RUN (from labels like {"0": "Stopped", "1": "Running"}).
    None means "no labels": any value > 0 counts as running."""
    if not value_labels:
        return None
    return {float(code) for code, label in value_labels.items() if status_from_label(label) == "RUN"}


def is_running(value: float, codes: set[float] | None) -> bool:
    return value > 0 if codes is None else value in codes


# ---------------------------------------------------------------- machine state


@dataclass
class Segment:
    state: str  # run | stop | comms
    start: float
    end: float

    @property
    def seconds(self) -> float:
        return self.end - self.start


def state_segments(samples: list[tuple[float, bool]], start: float, end: float, gap_s: float) -> list[Segment]:
    """samples: (t, running) sorted by t. Returns merged, contiguous segments covering [start, end]."""
    raw: list[Segment] = []
    cursor = start
    for i, (t, running) in enumerate(samples):
        if t >= end:
            break
        nxt = samples[i + 1][0] if i + 1 < len(samples) else end
        seg_start = max(t, start)
        seg_end = min(nxt, t + gap_s, end)
        if seg_end <= start:
            continue
        if seg_start > cursor:
            raw.append(Segment("comms", cursor, seg_start))
        raw.append(Segment("run" if running else "stop", seg_start, seg_end))
        cursor = seg_end
    if cursor < end:
        raw.append(Segment("comms", cursor, end))
    merged: list[Segment] = []
    for seg in raw:
        if seg.end <= seg.start:
            continue
        if merged and merged[-1].state == seg.state and abs(merged[-1].end - seg.start) < 1e-6:
            merged[-1].end = seg.end
        else:
            merged.append(Segment(seg.state, seg.start, seg.end))
    return merged


def totals(segments: list[Segment]) -> dict[str, float]:
    out = {"run": 0.0, "stop": 0.0, "comms": 0.0}
    for s in segments:
        out[s.state] += s.seconds
    return out


def availability(tot: dict[str, float]) -> float | None:
    """Run / (run + stop). Time without data is left out."""
    known = tot["run"] + tot["stop"]
    return tot["run"] / known if known > 0 else None


def stop_events(segments: list[Segment], window_end: float) -> list[dict]:
    """Stops, newest last. A stop still in progress at the window end is "ongoing"."""
    out = []
    for s in segments:
        if s.state == "stop":
            out.append({"start": s.start, "end": None if s.end >= window_end else s.end, "seconds": s.seconds})
    return out


def comms_events(segments: list[Segment], window_end: float, min_s: float = 0) -> list[dict]:
    return [
        {"start": s.start, "end": None if s.end >= window_end else s.end, "seconds": s.seconds}
        for s in segments
        if s.state == "comms" and s.seconds >= min_s
    ]


def in_states(series: list[tuple[float, float]], segments: list[Segment], states: set[str]) -> list[tuple[float, float]]:
    """Keep samples whose time falls inside a segment of one of `states` (both lists time-sorted)."""
    out, j = [], 0
    for t, v in series:
        while j < len(segments) and segments[j].end <= t:
            j += 1
        if j < len(segments) and segments[j].start <= t < segments[j].end and segments[j].state in states:
            out.append((t, v))
    return out


# ---------------------------------------------------------------- shifts (plant local time)


def shift_of(t: float, tz: ZoneInfo) -> str:
    hour = dt.datetime.fromtimestamp(t, tz).hour
    for name, h0, h1 in SHIFTS:
        if (h0 < h1 and h0 <= hour < h1) or (h0 > h1 and (hour >= h0 or hour < h1)):
            return name
    return SHIFTS[-1][0]


def shift_boundaries(start: float, end: float, tz: ZoneInfo) -> list[float]:
    """Epoch times of the shift changes (06:00 and 18:00 local) within (start, end)."""
    out = []
    day = dt.datetime.fromtimestamp(start, tz).date() - dt.timedelta(days=1)
    last = dt.datetime.fromtimestamp(end, tz).date() + dt.timedelta(days=1)
    while day <= last:
        for _, h0, _ in SHIFTS:
            t = dt.datetime(day.year, day.month, day.day, h0, tzinfo=tz).timestamp()
            if start < t < end:
                out.append(t)
        day += dt.timedelta(days=1)
    return sorted(out)


def split_segments(segments: list[Segment], cuts: list[float]) -> list[Segment]:
    out = []
    for s in segments:
        a = s.start
        for c in cuts[bisect.bisect_right(cuts, s.start):]:
            if c >= s.end:
                break
            out.append(Segment(s.state, a, c))
            a = c
        out.append(Segment(s.state, a, s.end))
    return out


# ---------------------------------------------------------------- statistics


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def stdev(values: list[float]) -> float | None:
    return statistics.stdev(values) if len(values) > 1 else None


def minute_means(series: list[tuple[float, float]], min_count: int = 1) -> list[tuple[float, float]]:
    buckets: dict[int, list[float]] = {}
    for t, v in series:
        buckets.setdefault(int(t // 60), []).append(v)
    return [(m * 60.0, sum(vs) / len(vs)) for m, vs in sorted(buckets.items()) if len(vs) >= min_count]


def imr_limits(values: list[float]) -> dict | None:
    """Individuals chart: centre = median, sigma = mean moving range / 1.128."""
    if len(values) < 3:
        return None
    mr = [abs(b - a) for a, b in zip(values, values[1:])]
    sigma = (sum(mr) / len(mr)) / 1.128
    cl = statistics.median(values)
    return {"cl": cl, "sigma": sigma, "ucl": cl + 3 * sigma, "lcl": cl - 3 * sigma}


def spc_flags(values: list[float], cl: float, ucl: float, lcl: float, run: int = 8) -> list[int]:
    """2 = outside control limits, 1 = `run` points in a row on one side of the centre line."""
    flags = []
    for k, v in enumerate(values):
        if v > ucl or v < lcl:
            flags.append(2)
            continue
        if k >= run - 1:
            side = math.copysign(1, v - cl) if v != cl else 0
            window = values[k - run + 1:k + 1]
            if side and all(math.copysign(1, x - cl) == side and x != cl for x in window):
                flags.append(1)
                continue
        flags.append(0)
    return flags


def capability(values: list[float], lsl: float | None, usl: float | None) -> dict:
    m, sd = mean(values), stdev(values)
    out = {"mean": m, "sd": sd, "cp": None, "cpk": None, "in_spec": None, "n": len(values)}
    if values and lsl is not None and usl is not None:
        out["in_spec"] = sum(1 for v in values if lsl <= v <= usl) / len(values)
    if m is None or sd is None or sd <= 0 or lsl is None or usl is None or usl <= lsl:
        return out
    out["cp"] = (usl - lsl) / (6 * sd)
    out["cpk"] = min(usl - m, m - lsl) / (3 * sd)
    return out


def histogram(values: list[float], lo: float, hi: float, bins: int = 24) -> list[dict]:
    if not values or hi <= lo:
        return []
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        counts[min(bins - 1, max(0, int((v - lo) / width)))] += 1
    return [{"lo": lo + i * width, "hi": lo + (i + 1) * width, "count": c} for i, c in enumerate(counts)]


def linreg(xs: list[float], ys: list[float]) -> dict | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        return None
    m = sxy / sxx
    b = my - m * mx
    r = sxy / math.sqrt(sxx * syy) if syy > 0 else 0.0
    rmse = math.sqrt(sum((y - (m * x + b)) ** 2 for x, y in zip(xs, ys)) / n)
    return {"m": m, "b": b, "r": r, "rmse": rmse, "n": n}


# ---------------------------------------------------------------- health


def piecewise(x: float, points: list[tuple[float, float]]) -> float:
    """Linear interpolation through (x, score) points sorted by x; clamped at the ends."""
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0) if x1 != x0 else y1
    return points[-1][1]


def limit_score(value: float | None, tag: dict) -> float | None:
    """0-100 from the tag's limits: inside normal = 100, at warning = 60, at critical = 20."""
    if value is None:
        return None
    d, warn, crit = tag.get("limit_dir"), tag.get("warn_limit"), tag.get("crit_limit")
    if d not in ("high", "low") or warn is None or crit is None:
        return None
    if d == "high":
        normal = tag.get("max_value")
        normal = warn - (crit - warn) if normal is None or normal >= warn else normal
        return piecewise(value, [(normal, 100), (warn, 60), (crit, 20), (crit + 1.5 * (crit - warn), 0)])
    normal = tag.get("min_value")
    normal = warn + (warn - crit) if normal is None or normal <= warn else normal
    return piecewise(-value, [(-normal, 100), (-warn, 60), (-crit, 20), (-(crit - 1.5 * (warn - crit)), 0)])


def residual_score(residual: float | None, sigma: float) -> float | None:
    if residual is None:
        return None
    s = max(sigma, 1e-6)
    return piecewise(abs(residual) / s, [(1.5, 100), (3, 60), (5, 20), (8, 0)])


def overall_score(parts: dict[str, tuple[float | None, float]]) -> float | None:
    """parts: name -> (score, weight). Weighted mean of available parts, capped at the weakest
    part + 20, so one failing component cannot hide behind healthy ones."""
    avail = [(s, w) for s, w in parts.values() if s is not None]
    if not avail:
        return None
    total_w = sum(w for _, w in avail)
    score = sum(s * w for s, w in avail) / total_w
    return min(score, min(s for s, _ in avail) + 20)


def health_state(score: float | None) -> str:
    if score is None:
        return "nodata"
    return "good" if score >= 80 else "watch" if score >= 55 else "act"


# ---------------------------------------------------------------- scheduled maintenance


def in_maintenance(t: float, window: dict | None) -> bool:
    """t inside the weekly window {weekday, start "HH:MM", end "HH:MM", tz} (asset_config.maintenance)."""
    if not isinstance(window, dict) or not all(k in window for k in ("weekday", "start", "end")):
        return False
    local = dt.datetime.fromtimestamp(t, ZoneInfo(window.get("tz", "UTC")))
    if local.strftime("%A") != window["weekday"]:
        return False
    hm = lambda text: int(text.split(":")[0]) * 60 + int(text.split(":")[1])  # noqa: E731
    return hm(window["start"]) <= local.hour * 60 + local.minute < hm(window["end"])


def planned_outage(start: float, end: float, window: dict | None) -> bool:
    """Most (80 %) of the outage falls in the scheduled maintenance window."""
    if not window or end <= start:
        return False
    points = [start + (end - start) * (k + 0.5) / 20 for k in range(20)]
    return sum(in_maintenance(t, window) for t in points) >= 16
