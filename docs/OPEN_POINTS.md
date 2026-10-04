# Open points

Items parked for a later decision. Each says what is open, why it matters, and what the change would be.

## 1. Restrict Platform data in the API, not only in the screens

**Status:** open, raised 2026-10-02.

**Today.** The Platform section (Data quality & link, Asset configuration, Raw data, System health) and the Admin section are visible to admins only. For operators and viewers the menu entries are hidden, links to those pages are removed from other screens, and opening their URLs redirects to the Plant overview. Editing actions in those screens already required the admin role in the API.

**What is still open.** Some read-only data behind those screens can still be fetched by any signed-in user directly from the API:

| Endpoint | Used by | Notes |
|---|---|---|
| `GET /api/v1/assets/{id}/data-quality` | Data quality & link | Completeness, outages, raw register values, commissioning checklist |
| `GET /api/v1/raw-messages` | Raw data; **Live view** (latest stored record) | Live view is for operators too |
| `GET /api/v1/system/stats` | System health; **Plant overview** (data pipeline) | Plant overview is for operators too |
| `GET /api/v1/devices/{id}/register-map` | Asset configuration | |
| `GET /api/v1/system/ingest-errors` | System health | Already admin-only |

**Proposed change.**
- Make `data-quality` and `register-map` admin-only.
- Make the full `raw-messages` list admin-only, and add a small operator-safe endpoint for the single latest record shown on Live view.
- Split `system/stats` into a short pipeline summary for everyone (used by Plant overview) and the full statistics for admins.
- Add end-to-end checks that an operator gets `403` from each restricted endpoint.

**Decide.** Whether operators may see raw payloads and pipeline statistics at all. If not, also drop the "Latest stored record" panel and the data pipeline panel for them.

## 2. Asset health: chart type of the two "vs speed" charts

**Status:** open, raised 2026-10-02.

"Motor load signature" (current vs speed) and "Vibration vs speed" are scatter plots with a dashed expected-value line, as in the reference design. The 30-day trend and the 10-minute view are line charts. Decide whether the two scatter plots should become line charts (e.g. current and vibration over time) instead.

## 3. Local tag configuration after a GCP sync

**Status:** note.

Roles, normal ranges, warning/critical limits and guidance for `conveyer-plc-line-01` were entered on the local stack. `./scripts/sync-from-gcp.sh` replaces the local database with the GCP copy, so enter them on GCP (Asset configuration) once the new version is deployed; after that a sync keeps them.

## 4. Process links still missing from the PM-01 demo model

**Status:** parked, 2026-10-04. Revisit when the demo should show the rules in [PROCESS_RULES.md](PROCESS_RULES.md).

**Today.** The generator (`services/ingestor/ingestor/history.py`) was made more realistic after a review of the generated data: poll-time stamps, irregular events, uneven bearing wear, friction current, noisier moisture with over-drying, a sensor noise floor at standstill, and process-caused stops. It still has only the PLC guide's three links: speed → current, speed → vibration, steam → moisture.

**What is open.** The links a real paper machine has (PROCESS_RULES.md, sections 1, 2 and 5):
- speed → moisture (faster sheet leaves wetter; the review suggested `+0.02 % per m/min` above 276.5)
- speed → steam pressure (moisture control raises steam with speed)
- dryer flooding: current up, then moisture up, at normal steam
- sheet-break signature: current drop, steam cut, moisture held
- roll resonance: a narrow speed band with much higher vibration

**Change.** Add them to `PM01.step`, extend `tests/test_history.py`, regenerate the week on GCP, then sync local.

