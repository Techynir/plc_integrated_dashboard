"""Process rules: each fires on its situation, after its delay, with a plain what / why / next."""

from ingestor.alarms import Rule
from ingestor.process_rules import Inputs, ProcessEngine, TagInfo, inputs_from_config

ROLES = {
    "machine_status": TagInfo("Machine_Status"),
    "speed": TagInfo("Machine_Speed", "m/min", 274, 279, 260, 240),
    "motor_current": TagInfo("Main_Motor_Current", "A", 134, 142, 155, 165),
    "steam_pressure": TagInfo("Dryer_Steam_Pressure", "bar", 4.10, 4.25, 3.5, 3.1),
    "moisture": TagInfo("Paper_Moisture", "%", 5.9, 6.4, 7.0, 7.5),
    "vibration": TagInfo("Main_Bearing_Vibration", "mm/s", 2.8, 3.3, 4.5, 5.5),
}
CFG = {"speed_target": 276.5, "rules": {"current_model": {"a": 11.0, "b": 0.458, "rmse": 0.4},
                                        "moisture_model": {"a": 12.81, "b": -1.6}}}
DELAYS = {"status_speed": (60, 10), "load_high": (120, 30), "load_drop": (10, 30), "vib_low_speed": (120, 30),
          "vib_rise": (120, 60), "steam_wet": (10, 30), "wet_other": (60, 30), "wet_restart": (30, 30)}
RULES = [Rule(i, code, "pm", None, "process", None, 0, "warning", "", None, on, off, False, "", f"process:pm:{code}")
         for i, (code, (on, off)) in enumerate(DELAYS.items(), start=1)]


def normal(speed=276.5, **over):
    v = {"speed": speed, "motor_current": 11 + 0.458 * speed, "steam_pressure": 4.17, "moisture": 6.14,
         "vibration": 3.1 * speed / 276.5}
    v.update(over)
    return v


class Sim:
    def __init__(self, cfg=CFG):
        self.engine = ProcessEngine()
        self.engine.configure("pm", inputs_from_config(ROLES, cfg))
        self.active: dict[str, dict] = {}
        self.events: list[tuple[float, str, str, dict | None]] = []
        self.t = 0.0

    def run(self, seconds, running=True, **values):
        for _ in range(int(seconds)):
            self.t += 1
            for tr in self.engine.evaluate("pm", RULES, self.t, running, values, lambda rid, _d: RULES[rid - 1].name in self.active):
                self.events.append((self.t, tr.action, tr.rule.name, tr.explain))
                if tr.action == "raise":
                    self.active[tr.rule.name] = tr.explain
                else:
                    self.active.pop(tr.rule.name)
        return self

    def raised(self, code):
        return [e for e in self.events if e[1] == "raise" and e[2] == code]


def settled():
    return Sim().run(400, **normal())


def test_normal_running_raises_nothing():
    s = Sim().run(4 * 3600, **normal())
    assert s.events == []


def test_motor_too_hard_at_reduced_speed_is_caught():
    s = settled().run(200, **normal(276.5))
    s.run(200, **normal(240.0))  # slow down (settling time passes)
    s.run(200, **normal(240.0, motor_current=151.0))  # +30 A, still below the 155 A limit alarm
    [(t, _, _, ex)] = s.raised("load_high")
    assert ex["what"] == "Motor is working much too hard."
    assert ex["why"] == "Current is 151 A. At this speed it should be about 121 A. The bearing is fine."
    assert ex["next"].startswith("Look for drag")


def test_motor_too_hard_with_shaking_bearing_points_at_the_bearing():
    s = settled().run(150, **normal(motor_current=160.0, vibration=5.0))
    [(_, _, _, ex)] = s.raised("load_high")
    assert "bearing is shaking more" in ex["why"] and ex["next"] == "Check the main bearing and its grease."


def test_load_rule_waits_two_minutes_and_clears():
    s = settled().run(119, **normal(motor_current=160.0))
    assert not s.raised("load_high")
    s.run(5, **normal(motor_current=160.0))
    assert s.raised("load_high")
    s.run(40, **normal())
    assert s.events[-1][1:3] == ("clear", "load_high")


def test_load_rule_is_held_after_a_restart():
    s = settled().run(60, running=False, **normal(0.0))
    s.run(150, **normal(motor_current=160.0))  # within 3 min of the restart
    assert not s.raised("load_high")


def test_bearing_hidden_by_low_speed():
    s = settled().run(200, **normal(240.0, vibration=4.2))  # 4.2 < 4.5 limit, but 4.84 at full speed
    [(_, _, _, ex)] = s.raised("vib_low_speed")
    assert ex["why"] == "Shaking is 4.2 mm/s at 240 m/min. At full speed it would be about 4.8 mm/s."


def test_bearing_shaking_more_than_usual():
    s = settled().run(90 * 60, **normal())  # an hour and a half to learn the usual level
    s.run(150, **normal(vibration=4.3))  # 1.2 above usual: more than 25 % of the 4.5 limit
    [(_, _, _, ex)] = s.raised("vib_rise")
    assert ex["why"] == "Now 4.3 mm/s. Usual for this bearing is 3.1 mm/s."


def test_low_steam_warns_before_the_paper_is_wet():
    s = settled().run(15, **normal(steam_pressure=3.45))  # predicted 7.29 %, moisture still 6.14
    [(t, _, _, ex)] = s.raised("steam_wet")
    assert ex == {"what": "Paper is getting too wet.", "why": "Steam pressure is low: 3.45 bar. Normal is 4.10 to 4.25 bar.",
                  "next": "Check the steam valve and the steam supply."}
    assert not s.raised("wet_other")  # wet because of steam: not the "other cause" rule


def test_wet_paper_with_normal_steam_points_elsewhere():
    s = settled().run(700, **normal())
    s.run(70, **normal(moisture=7.2))
    [(_, _, _, ex)] = s.raised("wet_other")
    assert ex["why"] == "Moisture is 7.2 %. Steam pressure is normal."
    assert "press section" in ex["next"]


def test_wet_soon_after_low_steam_is_the_steam():
    s = settled().run(20, **normal(steam_pressure=3.4)).run(300, **normal(moisture=7.2))
    assert not s.raised("wet_other")


def test_wet_after_restart():
    s = settled().run(300, running=False, **normal(0.0, moisture=7.3))
    s.run(40, **normal(moisture=7.1))
    [(_, _, _, ex)] = s.raised("wet_restart")
    assert ex["what"] == "Paper is wet after the restart."
    assert ex["next"].startswith("Give the dryers")


def test_status_and_speed_disagree():
    s = settled().run(70, running=False, **normal())  # says stopped at full speed
    [(_, _, _, ex)] = s.raised("status_speed")
    assert ex["what"] == "Machine says Stopped, but it is running."


def test_rules_without_coefficients_stay_quiet():
    s = Sim(cfg={"speed_target": 276.5}).run(600, **normal(motor_current=170.0))
    assert not s.raised("load_high")


def test_inputs_from_config_falls_back_to_the_speed_range():
    inp = inputs_from_config(ROLES, {})
    assert isinstance(inp, Inputs) and inp.speed_ref == 276.5 and inp.current_model is None
