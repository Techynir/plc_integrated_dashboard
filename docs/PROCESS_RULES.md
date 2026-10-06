# PM-01 process relationships and draft rules

**Status:** partly built, 2026-10-04. The rules marked ✅ below run as alarms (live in the ingestor, and over stored data with `ingestor.rules_backfill`). Their results show as insights (what, why, next action) inside the graph panels on *Asset health* and *Process quality*, without naming the rule (`services/api/app/insights.py`). Check the numbers with the mill's process engineer before relying on them.

| Rule | Built as | Screen |
|---|---|---|
| R1 status vs speed | ✅ "Status and speed disagree" (not the sheet-break part: no break signature in the data) | Asset health |
| R2 current vs speed | ✅ "Motor working too hard for its speed", "Motor load dropped" (no flooding branch: needs moisture rising with current) | Asset health |
| R3 vibration vs speed | ✅ "Bearing shakes too much for its speed" (reduced speed hides it), "Bearing shaking more than usual" (25 % of the warning limit above the usual level, ISO 10816-3 step rule). Not built: speed-band baselines, resonance bands | Asset health |
| R4 steam → moisture early warning | ✅ "Low steam: paper getting wet" | Process quality |
| R5 cause of high moisture | ✅ "Paper wet while steam is normal" (the steam case is R4) | Process quality |
| R6 speed up at dryer limit | not built: needs the speed set-point and steam that follows speed | — |
| R7 wet after restart | ✅ "Paper wet after restart" | Process quality |
| R8 group alarms by cause | partly: R2 names the bearing when vibration is up too; no grouping in the alarm list yet | — |
| R9 steam alarms during stops | not built (a limit setting, not a rule) | — |
| R10 settling time | ✅ built into R2, R3 and R5 (3 min after a restart or a speed change) | — |

Coefficients live in `asset_config.rules` (fitted by `rules_backfill --fit`): expected current = a + b × speed, moisture one minute later = a + b × steam pressure.

This note covers how the five PM-01 measurements affect each other on a paper machine, and the monitoring rules that follow from that. It is based on published paper-making and condition-monitoring sources (listed in section 6). The 7 days of PM-01 data were only used to check the demo model; that data was generated from the PLC guide, so it cannot prove anything about a real machine.

| Register | Tag | Unit | Normal range | Warning | Critical |
|---|---|---|---|---|---|
| 400002 | Machine_Speed | m/min | 274–279 (target 276.5) | below 260 | below 240 |
| 400004 | Main_Motor_Current | A | 134–142 | above 155 | above 165 |
| 400006 | Dryer_Steam_Pressure | bar | 4.10–4.25 | below 3.5 | below 3.1 |
| 400008 | Paper_Moisture | % | 5.9–6.4 | above 7.0 | above 7.5 |
| 400010 | Main_Bearing_Vibration | mm/s | 2.8–3.3 | above 4.5 | above 5.5 |

## 1. What affects what

Read each row as "when the left-hand value changes, this is what happens to the other value".

| Change | Affects | What happens on a real machine | Delay | Sources |
|---|---|---|---|---|
| Speed ↑ | Moisture ↑ | The sheet spends less time on the hot cylinders, so it leaves wetter. On a *dryer-limited* machine (steam already at its maximum), any speed increase raises reel moisture. | Minutes (the time for a change to pass through the dryers) | [3], [4], [5] |
| Speed ↑ | Steam pressure ↑ | Moisture control (usually a QCS) raises the steam pressure set-point to hold moisture. During grade and speed changes, speed and steam are moved together on purpose. So in normal operation **steam follows speed**, and moisture stays flat. | Seconds to minutes | [1], [6], [7] |
| Steam pressure ↓ | Moisture ↑ | Lower pressure means lower steam temperature, so less water is evaporated. Moisture is controlled *by* adjusting the steam pressure set-point. | Steam loop: seconds. Moisture: minutes (the cylinder shell and the sheet must heat or cool) | [1], [2] |
| Speed ↑ | Motor current ↑ | Drive current follows torque. Torque comes from friction (vacuum suction boxes cause about 80% of the friction load and rise with speed and vacuum), felts, and the dryer cylinders, whose power rises with the *square* of speed above the rimming speed. The rise is real but not exactly linear, and the slope changes with grade, vacuum and felt condition. | Immediate | [8], [9], [10] |
| Condensate build-up in dryers (flooding) | Current ↑ and moisture ↑ | Water inside the cylinders raises drive load: above about 1% condensate load, drives can overload or trip. Once it forms a layer on the shell (rimming), it blocks heat transfer and moisture rises. Signs: drive load high, steam pressure higher than normal for the grade and speed, lower shell and sheet temperatures. | Minutes to hours | [11], [12], [13] |
| Sheet break | Current ↓, speed briefly ↑, moisture held, steam cut | The drive loses web tension, so load drops; in tension control, speed jumps until the drive falls back to speed control. The moisture gauge is frozen and the steam pressure is cut deliberately (one published example: from 370 to 80 kPa) to stop the cylinders overheating. | Immediate | [14], [15], [16] |
| Moisture too high or too low | Break risk ↑ | Web strength depends mainly on moisture. A sheet that is too wet or too dry breaks more easily. A web restarted while still too wet is fragile, and the restart fails. Threading is done with reduced steam on purpose (higher moisture) so the tail is flexible. | — | [15], [16], [17] |
| Speed ↑ | Vibration ↑ | The force from unbalance grows with the *square* of speed, so vibration velocity rises with speed. Near a roll's critical speed (resonance), vibration can rise 10–30× within a narrow speed band. | Immediate | [18], [19], [20] |
| Bearing wear | Vibration ↑ (late) | The overall velocity in mm/s rises only in the *late* stage of bearing damage. Early stages show at high frequency (envelope, acceleration, shock pulse), which a single mm/s register does not capture. | Weeks to months | [21], [22] |
| Bearing fault | Motor current (spectrum) | Bearing faults show up as current *harmonics* (motor current signature analysis). The plain RMS current from the PLC changes only if friction becomes severe. | — | [23], [24] |
| Moisture into the dryers ↑ | Steam use ↑ | A wetter sheet entering the dryer section needs much more steam. One source: 7% more moisture entering means 34% more steam. If the dryers run out of capacity, reel moisture rises. | Minutes | [17] |

**What this means for PM-01's five values:**
- **Speed** is the main driver. It moves current and vibration straight away, and moisture or steam a few minutes later.
- **Steam pressure and moisture** are a control pair. Steam is the lever and moisture is the result. A large change in only one of them shows where the problem is.
- **Current** reflects load. It rises with speed, and also with flooding, felt or vacuum drag, mechanical drag, or a heavier sheet. It falls on a sheet break.
- **Vibration** reflects mechanical condition, but it must always be read against speed.

## 2. How the demo model compares

The PLC guide's model (`services/ingestor/ingestor/history.py`, `PM01.step`) and the 7 days generated from it contain:

| Link | In the demo | Measured | Real machine (section 1) |
|---|---|---|---|
| Speed → current | `11 + 0.458 × speed` | r = 0.997; 99% of running minutes within 0.34 A | Yes, but non-linear and depends on grade, vacuum and felt |
| Speed → vibration | Proportional to speed | r = 0.95 | Yes, closer to the square of speed, with resonance bands |
| Steam → moisture | −1.6 % per bar, 60 s lag | r = −0.99 one minute later | Yes, with lags of minutes |
| Speed → moisture | **Missing** | r ≈ 0 | Yes (dryer limit) |
| Speed → steam | **Missing** | r ≈ 0 | Yes (moisture control) |
| Flooding → current and moisture | **Missing** | — | Yes |
| Break: current drop, steam cut | **Missing**. Wet-sheet breaks and overload trips now cause about 1–2 stops a day, but a stop only ramps the speed down | — | Yes |
| Steam during stops | Stays at 4.17 bar | — | Cut or lowered on purpose |

So the dashboard demo shows only three of the real links. Rules that depend on the missing links can be written now, but they cannot be demonstrated until the model is extended (section 5).

## 3. Draft rules

"Running" means Machine_Status = 1. Unless a rule says otherwise, every rule is suppressed while the machine is stopped and for a settling time after a restart or a speed change (R10).

### R1. Sheet break signature

- **Condition:** current drops by more than 15% of its expected value within 10 s (starting value; tune it on recorded breaks) while the speed set-point has not been lowered (speed holds or briefly jumps), and status then goes to 0, or speed falls below 100 m/min with status still 1.
- **Means:** a sheet break [14], [15]. Record the stop with the reason "Sheet break" filled in automatically. Today the operator chooses the reason by hand on the Performance screen.
- **Also:** if status = 0 and speed > 270 m/min for 60 s, the status signal or register is wrong.

### R2. Motor current not matching speed (load deviation)

- **Expected current:** `I_expected(speed)`. Fit it from 2–4 weeks of normal running for each paper grade. A curve of the form `a + b·speed + c·speed²` is enough [8], [9]. Demo values: a = 11, b = 0.458, c = 0.
- **Deviation:** `ΔI = current − I_expected`. Warning when ΔI > +10 A for 2 min; critical when ΔI > +20 A. Start with these values, then set the limits to about 4× the standard deviation of ΔI during normal running.
- **Why it beats the fixed 155 A limit:** at 240 m/min the expected current is about 121 A, so a 30 A drag reaches only 151 A and raises no alarm.
- **Diagnosis (combine with other rules):**
  - ΔI high, vibration normal, moisture rising: **dryer flooding** [11], [12]. Check syphons, differential pressure and steam traps.
  - ΔI high, vibration high: **mechanical drag** (bearing, misalignment, a roll). Inspect.
  - ΔI high, vibration and moisture normal: **process load** (vacuum level, felt condition, doctor blades, a heavier sheet) [9], [10].
  - ΔI strongly negative while running: loss of web or load, which is a break (R1) or a coupling slip.

### R3. Vibration read against speed

- **Baseline per speed band:** store the normal vibration for each 10 m/min band, from 2–4 weeks of healthy running. Compare each reading against its own band, not against one fixed number [18], [19].
- **Step-change rule (ISO 10816-3):** a sustained change of more than 25% of the zone B/C limit is significant and needs investigation, even while the value is still in the acceptable zone [25]. With a B/C limit of 4.5 mm/s, that is a step of about 1.1 mm/s above the band baseline.
- **Absolute zones (ISO 10816-3, velocity RMS) [25]:**

  | Machine class | A/B (new) | B/C (limit for long-term running) | C/D (damage) |
  |---|---|---|---|
  | Group 1 (large, above 300 kW), rigid foundation | 2.3 | 4.5 | 7.1 |
  | Group 1, flexible foundation | 3.5 | 7.1 | 11.0 |
  | Group 2 (15–300 kW), rigid foundation | 1.4 | 2.8 | 4.5 |

  The current limits (warning 4.5, critical 5.5) fit a **Group 1, rigid** main drive. **Confirm the motor rating and foundation.** If it is Group 2 on a rigid foundation, the normal 3.05 mm/s is already in zone C, and the limits should be 2.8 and 4.5.
- **Resonance band:** if vibration jumps by more than 2× within a narrow speed range and falls again outside it, this is a critical speed, not wear [19], [20]. Mark that band and do not run at steady speed inside it.
- **Limit of this measurement:** one mm/s value catches bearing damage only in the late stage [21], [22]. For earlier warning, add an envelope or acceleration measurement on the main bearing. Record this as a hardware recommendation.

### R4. Early warning: moisture will rise because of steam

- **Predicted moisture:** `m_pred = m0 + k × (p0 − steam pressure)`. Fit `m0`, `p0` and `k` per grade from data [1], [2]. Demo values: 6.14 %, 4.17 bar, 1.6 % per bar.
- **Condition:** m_pred > 7.0 % gives the warning "moisture expected above 7.0 % within a few minutes: steam pressure low". m_pred > 7.5 % makes it critical.
- **The steam limits are later than the moisture limits** (with the demo coefficients). Moisture passes 7.0 % at 3.64 bar, but the steam warning is at 3.5 bar. Moisture passes 7.5 % at 3.32 bar, but the steam critical is at 3.1 bar. **Proposal:** move the steam limits to 3.65 bar (warning) and 3.3 bar (critical), or replace them with this rule.

### R5. Finding the cause of high moisture

When moisture > 7.0 % for 2 min, choose the guidance by checking these in order:

| Steam pressure | Other signs | Likely cause | Check | Sources |
|---|---|---|---|---|
| Low (below normal range) | — | Steam supply | Header valve, boiler, pressure control loop | [1] |
| At its maximum, or higher than usual for this speed | Speed raised in the last 30 min | Dryers out of capacity (dryer-limited) | Lower speed, or raise press dryness | [3], [4], [5] |
| Normal or high | ΔI high (R2) | Flooded dryers (condensate) | Syphons, differential pressure, steam traps, condensate tank level | [11], [12], [13] |
| Normal | Nothing else abnormal | Wetter sheet entering the dryers, or hood or air problems | Press section loading, felt condition, headbox consistency, hood humidity | [17] |

The moisture alarm's guidance today lists the first and last rows as free text. This rule would pick the right row automatically.

### R6. Speed increase while the dryers are at their limit

- **Condition:** speed set-point raised by more than 2% while steam pressure is within 5% of its highest value of the last 30 days, which is used as the stand-in for the maximum.
- **Means:** moisture will rise because the dryers cannot add more heat [4], [5].
- **Action:** a warning to the operator before moisture goes out of range.

### R7. Too wet at restart (break risk)

- **Condition:** within 15 min after a restart, moisture is more than 0.5 % above its normal range.
- **Means:** a wet, weak web has a high chance of breaking again [15], [16].
- **Action:** a warning: "Sheet wet after restart: check steam pressure has recovered."

### R8. Group alarms that share one cause

Show one event, with the first cause on top and the following alarms under it:
- Steam low, then moisture high within 10 min. Cause: steam.
- Speed raised, then moisture high within 30 min while steam is at its maximum. Cause: dryer limit (R6).
- ΔI high together with moisture high, with vibration normal. Cause: flooding (R2, R5).
- ΔI high together with vibration high. Cause: mechanical.
- Speed low together with ΔI high. Cause: the drive is overloaded and was slowed down.
- Speed low with ΔI near 0. An intentional slowdown: report it as lost production, not an equipment fault.

### R9. Steam alarms during stops and breaks

- Lowering steam pressure during a break or stop is normal practice [14], [16]. Today the steam pressure alarm is **not** suppressed while stopped (`suppress_when_stopped = false`).
- **Proposal:** while stopped, either suppress the steam limit alarm, or use a separate "standby" low limit that only catches a total loss of steam.
- Moisture is frozen during a break [14]. It is already suppressed while stopped. Keep it that way.

### R10. Settling time

- After a restart: ignore R2, R4 and R5 until status = 1 and speed has been inside its normal range for 3 min. Use 5 min if moisture lags longer than that on the real machine.
- After a speed change of more than 5 m/min: hold R2, R4 and R5 for 3 min. Current and vibration follow straight away, but moisture needs minutes.
- Measure the real moisture delay. Find the time shift at which a steam step best matches the moisture change (in the demo this is 1 min), and use it for R4, R8 and this rule.

## 4. Data PM-01 does not have yet (to make the rules reliable)

Ordered by value:
1. **Speed set-point** or the drive's speed reference. It separates intended changes from upsets (R1, R6, R8).
2. **Paper grade or basis weight.** Every coefficient in R2 and R4 depends on the grade.
3. **Steam differential pressure** or condensate tank level. It confirms flooding (R5).
4. **Sheet-break signal** from the break detector or the drive. It makes R1 exact instead of inferred.
5. **Bearing envelope or acceleration value.** It gives earlier bearing warnings than mm/s (R3).

## 5. Implementation notes

- **Derived values** (in the API or the ingestor): `I_expected`, `ΔI`, the vibration speed-band baseline, and `m_pred`. Show them as lanes on Live view and Trends, and use the speed-band vibration in Asset health.
- **The alarm engine** today supports only a fixed limit per tag. These rules need:
  - limits on derived values (R2, R3, R4)
  - conditions across several tags (R1, R5, R6, R7)
  - grouping by cause (R8)
  - limits that depend on machine state (R9)
  - hold-off times (R10)
- **Coefficients** (expected-current curve, moisture model, speed bands, ISO class) belong in the asset analytics settings, per grade.
- **Demo model:** to demonstrate these rules, extend `PM01.step` with:
  - speed → moisture
  - a moisture controller that moves steam with speed
  - flooding events (current up, then moisture up)
  - breaks (current drop, steam cut, moisture frozen)
  - a resonance band in vibration

  Then regenerate the history.

## 6. Sources

1. Slätteke, O. (2006), *Modeling and Control of the Paper Machine Drying Section*, PhD thesis, Lund University. https://skoge.folk.ntnu.no/puublications_others/thesis/thesis-ola-slatteke-jan06.pdf
2. *Fundamentals of Paper Drying – Theory and Application from Industrial Perspective*, InTech. https://cdn.intechopen.com/pdfs/19429/InTech-Fundamentals_of_paper_drying_theory_and_application_from_industrial_perspective.pdf
3. TAPPI PaperCon 2011, *Improved Energy Efficiency in Paper Making Through …*. https://www.tappi.org/content/events/11papercon/documents/318.455%20doc.pdf
4. US 3930934, *Speed optimization control for paper making machines with dryer limited conditions*. https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/3930934
5. US 4314878, *Method of operating a papermachine drying line*. https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/4314878
6. *Feedforward control in the paper machine drying section*, IEEE. https://ieeexplore.ieee.org/document/1656506/
7. US 3801426A, *Dryer control and grade change system for a paper machine*. https://patents.google.com/patent/US3801426A/en
8. *Drive Power and Torque in Paper Machine Dryers*. https://www.slideshare.net/slideshow/drive-power-and-torque-in-paper-machine-dryers/67599747
9. *Global energy consumption due to friction in paper machines*, Tribology International (2013). https://www.sciencedirect.com/science/article/abs/pii/S0301679X13000443
10. ABB, *Paper Machine Drives Performance*. https://campaign.abb.com/PM-drives-performance-data-sheet
11. Kadant, *Why Dryers Flood in Papermaking*. https://kadant.com/en/blog/fluid-handling/why-dryers-flood-in-papermaking
12. Kadant, *Identifying Flooded Dryers in Papermaking*. https://kadant.com/en/blog/fluid-handling/identifying-flooded-dryers-in-papermaking
13. *Online Estimation of the Condensate Load in Dryer Cylinders During Section Starting*. https://www.researchgate.net/publication/252029250_Online_Estimation_of_the_Condensate_Load_in_Dryer_Cylinders_During_Section_Starting
14. *Improved web break strategy using a new approach for steam pressure control in paper machines*, Control Engineering Practice (2008). https://www.sciencedirect.com/science/article/abs/pii/S0967066108000178
15. *Dryer section control in paper machines during web breaks*. https://www.lunduniversity.lu.se/publication/afa132be-474e-4254-9a0c-c92bbc148a69
16. US 6712935, *Method and arrangement in tail threading in a paper machine*. https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/6712935
17. *Paper Drying in the Manufacturing Process*. https://www.researchgate.net/publication/331998171_Paper_Drying_in_the_Manufacturing_Process
18. Tractian, *Unbalance and Field Balancing*. https://tractian.com/en/glossary/unbalance-field-balancing
19. *Critical Speed in Rotating Machinery*. https://www.fabrico.io/blog/critical-speed-rotordynamics/
20. Eckert, W. F., *Paper Machine Speed Increase Trial*, Wood plc. https://vdn.woodplc.com/assets/pdfs/Technical_Articles/Paper_Machine_Speed_Increase_Trial.pdf
21. Hansford Sensors, *Acceleration enveloping to detect bearing damage*. https://hansfordsensors.com/wp-content/uploads/2023/01/Hansford-Sensors-Acceleration-Enveloping-White-Paper-Nov-2016.pdf
22. *Detecting Rolling Elements Bearings Faults*. https://www.researchgate.net/publication/339123818_Detecting_Rolling_Elements_Bearings_Faults
23. *Motor Current Signature Analysis for Bearing Fault Detection in Mechanical Systems*. https://www.researchgate.net/publication/265513574_Motor_Current_Signature_Analysis_for_Bearing_Fault_Detection_in_Mechanical_Systems
24. *A Study of Rolling-Element Bearing Fault Diagnosis Using Motor's Vibration and Current Signatures*. https://www.sciencedirect.com/science/article/pii/S1474667016358025
25. *ISO 10816-3 Vibration Severity: Zones, Limits and How to Read Them*. https://www.fabrico.io/blog/iso-10816-3-vibration-severity/
