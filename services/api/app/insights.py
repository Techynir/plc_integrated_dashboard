"""Insights shown under the Asset health and Process quality graphs.

Each insight is three short, plain sentences: what happened, why (with the numbers), and the next
action. They are built from the process-rule findings (alarms of the built-in rules, see
process_rules.py) and the measured data. Rule names are not shown: only what was found.

Pure functions, so the wording and the decisions can be tested without a database.
"""

from statistics import median

LOOKBACK_DAYS = 7  # Asset health insights look at a week: a day has too few events to see a pattern


def item(what: str, why: str, next_: str, level: str = "info") -> dict:
    """level: "now" (happening right now), "warn" (found in the period), "ok" (nothing found)."""
    return {"what": what, "why": why, "next": next_, "level": level}


def n_times(n: int) -> str:
    return "once" if n == 1 else "twice" if n == 2 else f"{n} times"


def duration(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} hours"
    if seconds >= 60:
        return f"{round(seconds / 60)} minutes"
    return f"{round(seconds)} seconds"


def share(k: int, n: int) -> str:
    return "Each time" if k == n else f"{k} of {n} times"


def now_items(findings: list[dict], codes: set[str]) -> list[dict]:
    """Findings still active: their own what / why / next, marked as happening now."""
    return [item(f["explain"]["what"], f["explain"]["why"], f["explain"]["next"], "now")
            for f in findings if f["active"] and f["code"] in codes and f.get("explain")]


def during(minutes: list[dict], f: dict, stop: float) -> list[dict]:
    """Running minutes inside a finding (at least the minute it started in)."""
    start, end = f["start"], f["end"] or stop
    inside = [m for m in minutes if start - 60 <= m["t"] <= end]
    return inside or [m for m in minutes if abs(m["t"] - start) <= 120][:1]


def avg(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return sum(vals) / len(vals) if vals else None


# ---------------------------------------------------------------- asset health


def health_insights(*, minutes: list[dict], findings: list[dict], stop: float, speed: dict | None,
                    current: dict | None, vibration: dict | None, model: dict | None, usual: dict | None,
                    projection: dict | None) -> dict:
    """minutes: running 1-minute means over LOOKBACK_DAYS, keys t / speed / current / vibration.
    findings: process-rule findings over the same days. Returns insights per graph."""
    out: dict[str, list[dict]] = {"trend": [], "load": [], "vibration": []}
    days = f"in the last {LOOKBACK_DAYS} days"

    def speed_ok(s: float | None) -> bool:
        if s is None or not speed:
            return False
        lo, hi = speed.get("min_value"), speed.get("max_value")
        return lo is not None and hi is not None and lo - 1 <= s <= hi + 1

    def current_ok(c: float | None, s: float | None) -> bool:
        return c is not None and s is not None and model is not None and abs(c - (model["b"] + model["m"] * s)) < model["limit"]

    def vib_ok(v: float | None) -> bool:
        if v is None:
            return False
        if usual and usual.get("step") is not None:
            return v <= usual["usual"] + usual["step"]
        return vibration is not None and vibration.get("warn_limit") is not None and v < vibration["warn_limit"]

    su = speed["unit"] if speed else ""
    cu = current["unit"] if current else ""
    vu = vibration["unit"] if vibration else ""

    # bearing trend (30-day projection)
    if vibration and projection and projection.get("days_to_warning") and projection["m"] > 0 and projection["days_to_warning"] > 0:
        d = round(projection["days_to_warning"])
        weeks = max(1, d // 7 - 1)
        out["trend"].append(item(
            "The bearing is slowly wearing.",
            f"Shaking rises {projection['per_month']:.2f} {vu} a month. At this rate it reaches the "
            f"{vibration['warn_limit']:g} {vu} warning in about {d} days.",
            f"Plan a bearing check in the next {weeks} week{'s' if weeks > 1 else ''}.",
            "warn" if d < 60 else "info",
        ))
    elif vibration and projection:
        out["trend"].append(item("The bearing is not wearing faster.", "Daily shaking is flat.", "Nothing to do.", "ok"))

    # motor load: worked too hard (overloads)
    loads = [f for f in findings if f["code"] == "load_high"]
    out["load"] += now_items(findings, {"load_high", "load_drop"})
    if current and speed:
        if not loads:
            out["load"].append(item("Motor load is normal for its speed.",
                                    f"The current stayed close to normal {days}.", "Nothing to do.", "ok"))
        else:
            stats = [(avg(during(minutes, f, stop), "speed"), avg(during(minutes, f, stop), "vibration"),
                      avg(during(minutes, f, stop), "current")) for f in loads]
            drag = [s for s in stats if speed_ok(s[0]) and vib_ok(s[1])]
            shaky = [s for s in stats if not vib_ok(s[1])]
            n = len(loads)
            sp, vb = avg([{"v": s[0]} for s in drag], "v"), avg([{"v": s[1]} for s in drag], "v")
            if drag and len(drag) >= len(shaky):
                out["load"].append(item(
                    f"The motor worked too hard {n_times(n)} {days}.",
                    f"{share(len(drag), n)} speed was normal ({sp:.0f} {su}) and the bearing was calm ({vb:.1f} {vu}). "
                    "So it is drag, not the bearing.",
                    "Check the felts for filling and the web tension.",
                    "warn",
                ))
            else:
                vb = avg([{"v": s[1]} for s in shaky], "v")
                out["load"].append(item(
                    f"The motor worked too hard {n_times(n)} {days}.",
                    f"{share(len(shaky), n)} the bearing was shaking too ({vb:.1f} {vu}). So the bearing is the likely cause.",
                    "Check the main bearing and its grease.",
                    "warn",
                ))

    # bearing: shook more than usual (spikes)
    spikes = [f for f in findings if f["code"] == "vib_rise"]
    low_speed = [f for f in findings if f["code"] == "vib_low_speed"]
    out["vibration"] += now_items(findings, {"vib_rise", "vib_low_speed"})
    if vibration and speed:
        if not spikes:
            out["vibration"].append(item("The bearing shook as usual.",
                                         f"No shaking above its usual level {days}.", "Nothing to do.", "ok"))
        else:
            stats = [(avg(during(minutes, f, stop), "speed"), avg(during(minutes, f, stop), "current")) for f in spikes]
            local = [s for s in stats if speed_ok(s[0]) and (current is None or current_ok(s[1], s[0]))]
            n = len(spikes)
            if local and len(local) * 2 >= n:
                sp, cur = avg([{"v": s[0]} for s in local], "v"), avg([{"v": s[1]} for s in local], "v")
                normal = f"speed ({sp:.0f} {su}) and current ({cur:.0f} {cu})" if cur is not None else f"speed ({sp:.0f} {su})"
                out["vibration"].append(item(
                    f"The bearing shook more than usual {n_times(n)} {days}.",
                    f"{share(len(local), n)} {normal} stayed normal. So the cause is at the bearing or a roll.",
                    "Check the bearing grease and the roll balance.",
                    "warn",
                ))
            else:
                out["vibration"].append(item(
                    f"The bearing shook more than usual {n_times(n)} {days}.",
                    "Speed or motor load was also off at those times.",
                    "Check the speed changes and drive load at those times first.",
                    "warn",
                ))
        if low_speed:
            out["vibration"].append(item(
                f"The bearing shook too much at low speed {n_times(len(low_speed))} {days}.",
                "The reading looked fine, but at full speed it would have been above the warning.",
                "Plan a bearing check before going back to full speed.",
                "warn",
            ))

    # overloads and spikes at different times: two separate problems
    if loads and spikes:
        together = sum(1 for a in loads if any(a["start"] < (b["end"] or stop) and b["start"] < (a["end"] or stop)
                                               for b in spikes))
        if together <= 0.2 * len(loads):
            when = "They never happened at the same time." if together == 0 else \
                f"Only {together} of the {len(loads)} overloads came with a bearing spike."
            out["load"].append(item(
                "Motor overloads and bearing spikes are two separate problems.",
                when,
                "Treat them as two jobs: a drag check and a bearing check.",
                "info",
            ))
    return out


# ---------------------------------------------------------------- process quality


def low_periods(samples: list[tuple[float, float]], below: float, min_s: float = 10.0, join_s: float = 5.0) -> list[float]:
    """Start times of periods where the value stays below `below` for at least min_s."""
    starts, cur_start, last = [], None, None
    for t, v in samples:
        if v < below:
            if cur_start is None or (last is not None and t - last > join_s):
                if cur_start is not None and last - cur_start >= min_s:
                    starts.append(cur_start)
                cur_start = t
            last = t
        elif cur_start is not None and last is not None and t - last > join_s:
            if last - cur_start >= min_s:
                starts.append(cur_start)
            cur_start = last = None
    if cur_start is not None and last is not None and last - cur_start >= min_s:
        starts.append(cur_start)
    return starts


def wet_episodes(samples: list[tuple[float, float]], above: float, cap_s: float, gap_s: float = 60.0) -> list[dict]:
    """Periods with the value above `above` (running samples): start and time spent above."""
    eps: list[dict] = []
    for (t, v), nxt in zip(samples, samples[1:] + [(None, None)]):
        if v <= above:
            continue
        dt = min(nxt[0] - t, cap_s) if nxt[0] is not None else 0.0
        if eps and t - eps[-1]["last"] <= gap_s:
            eps[-1]["seconds"] += dt
            eps[-1]["last"] = t
        else:
            eps.append({"start": t, "last": t, "seconds": dt})
    return eps


def quality_insights(*, moisture: list[tuple[float, float]], steam: list[tuple[float, float]], moist_tag: dict,
                     steam_tag: dict | None, model: dict | None, steam_limit: float | None, findings: list[dict],
                     in_spec: float | None, period: str, cap_s: float) -> dict:
    """moisture: running samples in the window; steam: all samples in the window (and 15 min before)."""
    out: dict[str, list[dict]] = {"control": [], "steam": []}
    usl, mu = moist_tag.get("max_value"), moist_tag.get("unit", "")
    out["control"] += now_items(findings, {"steam_wet", "wet_other", "wet_restart"})
    if usl is not None and moisture:
        eps = wet_episodes(moisture, usl, cap_s)
        wet_s = sum(e["seconds"] for e in eps)
        if wet_s < 60:
            lo = moist_tag.get("min_value")
            rng = f"between {lo:g} and {usl:g} {mu}" if lo is not None else f"below {usl:g} {mu}"
            pct = f"{in_spec * 100:.0f} % of the running time " if in_spec is not None else ""
            out["control"].append(item("Moisture stayed normal.", f"It was {pct}{rng}.", "Nothing to do.", "ok"))
        else:
            what = f"Paper was too wet (above {usl:g} {mu}) for {duration(wet_s)} while running, {period}."
            trigger = None
            if steam_tag:
                if model and model.get("b", 0) < 0:
                    trigger = (usl - model["a"]) / model["b"]  # steam pressure that pushes moisture over the limit
                elif steam_tag.get("min_value") is not None:
                    trigger = steam_tag["min_value"] - 0.05
            if trigger is not None:
                drops = low_periods(steam, trigger)
                leads, covered = [], 0.0
                for e in eps:
                    before = [d for d in drops if e["start"] - 900 <= d <= e["start"]]
                    if before:
                        leads.append(e["start"] - before[-1])
                        covered += e["seconds"]
                part = covered / wet_s
                su = steam_tag.get("unit", "")
                if leads and part >= 0.5:
                    lead = median(leads)
                    out["control"].append(item(
                        what,
                        f"{'Almost all' if part >= 0.9 else f'{part * 100:.0f} %'} of it came after the steam pressure fell "
                        f"below {trigger:.2f} {su}. The steam fell about {round(lead)} seconds before the moisture rose.",
                        f"Warn the crew when steam falls below {trigger:.2f} {su}. That gives about {round(lead)} seconds to act.",
                        "warn",
                    ))
                else:
                    out["control"].append(item(
                        what,
                        f"Only {part * 100:.0f} % of it came after low steam. Most of it has another cause.",
                        "Check the press section, the felts and the dryer drains.",
                        "warn",
                    ))
            else:
                out["control"].append(item(what, "There is no steam pressure signal to compare.",
                                           "Check the dryer steam supply and the press section.", "warn"))
    restarts = [f for f in findings if f["code"] == "wet_restart"]
    if restarts:
        out["control"].append(item(
            f"Paper was wet after {'a restart' if len(restarts) == 1 else f'{len(restarts)} restarts'}, {period}.",
            "A wet sheet breaks easily in the first minutes.",
            "After a stop, bring steam pressure up before full speed.",
            "warn",
        ))
    other = [f for f in findings if f["code"] == "wet_other"]
    if other:
        out["control"].append(item(
            f"Paper was too wet while steam was normal, {n_times(len(other))} {period}.",
            "The steam was fine, so the water came from earlier in the machine.",
            "Check the press section, the felts and the dryer drains.",
            "warn",
        ))

    if steam_tag and model and model.get("b", 0) < 0 and steam_limit is not None:
        su = steam_tag.get("unit", "")
        lows = [f for f in findings if f["code"] == "steam_wet"]
        seen = f" It went that low {n_times(len(lows))} {period}." if lows else ""
        keep = steam_tag.get("min_value")
        out["steam"].append(item(
            "Steam pressure controls the paper moisture.",
            f"1 {su} less steam adds about {abs(model['b']):.1f} {mu} moisture a minute later. "
            f"Below {steam_limit:.2f} {su} the paper goes above {moist_tag.get('warn_limit'):g} {mu}.{seen}",
            f"Keep steam above {keep:g} {su}." if keep is not None else "Keep steam in its normal range.",
            "warn" if lows else "info",
        ))
    return out
