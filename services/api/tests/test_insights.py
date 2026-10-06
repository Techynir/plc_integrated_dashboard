"""Insights under the health and quality graphs: right diagnosis, plain words, real numbers."""

from app import insights as ins

SPEED = {"unit": "m/min", "min_value": 274, "max_value": 279}
CURRENT = {"unit": "A"}
VIB = {"unit": "mm/s", "warn_limit": 4.5}
MODEL = {"m": 0.45, "b": 13.0, "limit": 10.0}
USUAL = {"usual": 3.1, "ref_speed": 276.5, "step": 1.125}
STOP = 10 * 3600.0


def minute(t, speed=276.5, current=None, vib=3.1):
    return {"t": t, "speed": speed, "current": current if current is not None else 13 + 0.45 * speed, "vibration": vib}


def finding(code, start, end, active=False):
    return {"code": code, "start": start, "end": end, "active": active,
            "explain": {"what": "w", "why": "y", "next": "n"}}


def health(minutes, findings, projection=None):
    return ins.health_insights(minutes=minutes, findings=findings, stop=STOP, speed=SPEED, current=CURRENT,
                               vibration=VIB, model=MODEL, usual=USUAL, projection=projection)


def test_overloads_at_normal_speed_and_calm_bearing_point_to_drag():
    mins = [minute(t * 60.0) for t in range(600)]
    mins[100] = minute(6000.0, current=163.0)
    mins[300] = minute(18000.0, current=165.0)
    out = health(mins, [finding("load_high", 5990, 6050), finding("load_high", 17990, 18050)])
    [load, *_] = out["load"]
    assert load["what"] == "The motor worked too hard twice in the last 7 days."
    assert load["why"] == "Each time speed was normal (276 m/min) and the bearing was calm (3.1 mm/s). So it is drag, not the bearing."
    assert load["next"] == "Check the felts for filling and the web tension."


def test_overloads_with_a_shaking_bearing_point_to_the_bearing():
    mins = [minute(6000.0, current=165.0, vib=5.2)]
    [load] = health(mins, [finding("load_high", 5990, 6050)])["load"]
    assert "bearing is the likely cause" in load["why"] and load["next"] == "Check the main bearing and its grease."


def test_spikes_at_normal_speed_and_current_point_to_a_local_cause():
    mins = [minute(6000.0, vib=4.8), minute(9000.0, vib=4.9)]
    [vib] = health(mins, [finding("vib_rise", 5990, 6050), finding("vib_rise", 8990, 9050)])["vibration"]
    assert vib["what"] == "The bearing shook more than usual twice in the last 7 days."
    assert vib["why"].startswith("Each time speed (276 m/min) and current (137 A) stayed normal.")
    assert vib["next"] == "Check the bearing grease and the roll balance."


def test_overloads_and_spikes_at_different_times_are_two_jobs():
    mins = [minute(6000.0, current=165.0), minute(9000.0, vib=4.9)]
    out = health(mins, [finding("load_high", 5990, 6050), finding("vib_rise", 8990, 9050)])
    assert out["load"][-1]["what"] == "Motor overloads and bearing spikes are two separate problems."
    assert out["load"][-1]["next"] == "Treat them as two jobs: a drag check and a bearing check."


def test_quiet_week_says_so_and_active_findings_come_first():
    out = health([minute(60.0)], [])
    assert out["load"][0]["level"] == "ok" and out["vibration"][0]["what"] == "The bearing shook as usual."
    out = health([minute(STOP - 60, current=170.0)], [finding("load_high", STOP - 200, None, active=True)])
    assert out["load"][0]["level"] == "now"


def test_bearing_wear_projection():
    out = health([], [], projection={"m": 0.027, "per_month": 0.82, "days_to_warning": 46})
    assert out["trend"][0]["why"] == "Shaking rises 0.82 mm/s a month. At this rate it reaches the 4.5 mm/s warning in about 46 days."
    assert out["trend"][0]["next"] == "Plan a bearing check in the next 5 weeks."


MOIST = {"unit": "%", "min_value": 5.9, "max_value": 6.4, "warn_limit": 7.0}
STEAM = {"unit": "bar", "min_value": 4.1}
QMODEL = {"a": 12.54, "b": -1.535}  # moisture over 6.4 % below 4.00 bar


def wet_hour(t0):
    """Steam drops at t0, moisture goes over 6.4 % 40 s later and stays there 5 minutes."""
    steam = [(t0 + s, 4.17 if s < 0 else 3.6) for s in range(-60, 400)]
    moist = [(t0 + s, 6.1 if s < 40 else 6.9 if s < 340 else 6.1) for s in range(-60, 400)]
    return steam, moist


def test_wet_paper_after_low_steam_gives_the_lead_time():
    steam, moist = wet_hour(1000.0)
    s2, m2 = wet_hour(5000.0)
    out = ins.quality_insights(moisture=moist + m2, steam=steam + s2, moist_tag=MOIST, steam_tag=STEAM, model=QMODEL,
                               steam_limit=3.61, findings=[], in_spec=0.9, period="in the last 8 hours", cap_s=5)
    [c] = out["control"]
    assert c["what"] == "Paper was too wet (above 6.4 %) for 10 minutes while running, in the last 8 hours."
    assert c["why"] == "Almost all of it came after the steam pressure fell below 4.00 bar. The steam fell about 40 seconds before the moisture rose."
    assert c["next"] == "Warn the crew when steam falls below 4.00 bar. That gives about 40 seconds to act."


def test_wet_paper_without_low_steam_points_elsewhere():
    moist = [(float(s), 6.9) for s in range(600)]
    steam = [(float(s), 4.17) for s in range(600)]
    out = ins.quality_insights(moisture=moist, steam=steam, moist_tag=MOIST, steam_tag=STEAM, model=QMODEL,
                               steam_limit=3.61, findings=[], in_spec=0.5, period="in the last hour", cap_s=5)
    assert out["control"][0]["why"].startswith("Only 0 % of it came after low steam")
    assert out["control"][0]["next"] == "Check the press section, the felts and the dryer drains."


def test_normal_moisture_and_the_steam_relationship():
    moist = [(float(s), 6.1) for s in range(600)]
    out = ins.quality_insights(moisture=moist, steam=[], moist_tag=MOIST, steam_tag=STEAM, model=QMODEL, steam_limit=3.61,
                               findings=[finding("steam_wet", 10, 70)], in_spec=0.99, period="in the last hour", cap_s=5)
    assert out["control"][0]["what"] == "Moisture stayed normal."
    assert out["steam"][0]["why"] == ("1 bar less steam adds about 1.5 % moisture a minute later. Below 3.61 bar the paper "
                                      "goes above 7 %. It went that low once in the last hour.")
    assert out["steam"][0]["next"] == "Keep steam above 4.1 bar."
