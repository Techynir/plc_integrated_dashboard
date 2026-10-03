import { HealthData, useAnalytics, useDevice, useHistory, windowEnd } from "../../assetApi";
import { roleTag, useAsset } from "../../hooks";
import { Chip, fmtDT, fmtNum, MissingRole, Panel, ScreenHead } from "../../components/console";
import { Lanes, Projection, Scatter } from "../../components/charts";
import { ErrorText, Loading, useNow } from "../../components/ui";
import { lanesFor } from "./Live";

const STATE = {
  good: { cls: "ok" as const, label: "Good" },
  watch: { cls: "warn" as const, label: "Watch" },
  act: { cls: "crit" as const, label: "Act" },
  nodata: { cls: "bad" as const, label: "No data" },
};

function Comp({ name, detail, score }: { name: string; detail: string; score: number | null }) {
  const cls = score == null ? "" : score < 55 ? "c" : score < 80 ? "w" : "";
  return (
    <>
      <div className="comp">
        <span>{name}</span>
        <div className="tr" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={score ?? undefined} aria-label={name}>
          <i className={cls} style={{ width: `${score ?? 0}%` }} />
        </div>
        <b>{score == null ? "—" : Math.round(score)}</b>
      </div>
      <div className="ctx" style={{ margin: "-4px 0 8px 140px" }}>
        {detail}
      </div>
    </>
  );
}

const signed = (v: number | null | undefined, d: number) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(d)}`);

export function Health() {
  const { asset } = useAsset();
  const id = asset?.device_id;
  const h = useAnalytics<HealthData>(id, "health", null, 60_000);
  const detail = useDevice(id, 60_000);
  const now = useNow(10_000);
  const vibTag = roleTag(detail.data?.tags, "vibration");
  const span = 10 * 60_000;
  const { end, anchored } = windowEnd(asset?.last_seen, span, Math.floor(now / 10_000) * 10_000);
  const hist = useHistory(id, vibTag ? [vibTag.tag] : [], end - span, end, anchored ? false : 10_000);

  if (!asset) return <div className="panel empty">No asset selected.</div>;
  const d = h.data;
  const vt = d?.tags.vibration;
  const ct = d?.tags.motor_current;
  const st = d?.tags.speed;
  const pt = d?.tags.steam_pressure;
  const state = STATE[d?.state ?? "nodata"];
  const warn = vt?.warn_limit ?? null;
  const short = d?.short_term;
  const slope = short?.slope_per_min ?? null;

  const projNote = (() => {
    if (!d || !vt) return "";
    if (!d.projection) return `${d.daily_vibration.length} day(s) of running data so far. The projection appears after 3 days.`;
    const p = d.projection;
    if (p.m > 0 && p.days_to_warning != null && p.days_to_warning > 0)
      return `Trend ${signed(p.per_month, 2)} ${vt.unit} per month. At this rate vibration reaches the ${warn} ${vt.unit} warning level in about ${Math.round(p.days_to_warning)} days. Indicative only.`;
    return "No upward trend in the daily means.";
  })();
  const shortNote = (() => {
    if (!short || slope === null || warn === null) return "Not enough running data in the last 5 minutes.";
    if (slope > 0.02 && short.current != null && short.current < warn && short.minutes_to_warning != null)
      return `Rising fast. If this continues, the ${warn} ${vt?.unit} warning is about ${Math.round(short.minutes_to_warning)} min away.`;
    if (slope > 0.02 && short.current != null && short.current >= warn) return `Above the ${warn} ${vt?.unit} warning level and still rising. Inspect the bearing.`;
    return "Short-term trend is flat. No early warning.";
  })();
  const n = d?.load_signature.length ?? 0;
  const band = d ? Math.max(2 * d.inputs.sigma, 0.5) : 0;

  return (
    <div className="screen">
      <ScreenHead
        eyebrow="Analytics · condition monitoring"
        title="Asset health · main drive & bearing"
        desc="A 0–100 score built from vibration, motor load against speed, and dryer steam supply. Projections are indicative and need months of real data before being used for maintenance planning."
      />
      {h.error && <ErrorText error={h.error} />}
      {!d ? (
        <Loading />
      ) : (
        <>
          {d.anchored && (
            <div className="alert comms">
              <Chip cls="comms">No recent data</Chip>
              <span>
                Scored from the last minute of data, ending <b>{fmtDT(d.end)}</b>.
              </span>
            </div>
          )}
          <section className="panel">
            <div className="health">
              <div className="score">
                <span className="eyebrow">Overall health</span>
                <div className="big">
                  {d.overall == null ? "—" : Math.round(d.overall)}
                  {d.overall != null && <small>/100</small>}
                </div>
                <Chip cls={state.cls}>{state.label}</Chip>
              </div>
              <div>
                <Comp name="Main bearing" score={d.components.bearing} detail={vt ? `vibration ${fmtNum(d.inputs.vibration, 2)} ${vt.unit} · last 60 s` : "no vibration tag"} />
                <Comp
                  name="Main drive"
                  score={d.components.drive}
                  detail={ct ? `current ${signed(d.inputs.residual, 1)} ${ct.unit} vs expected for speed` : "needs motor current and speed tags"}
                />
                <Comp name="Dryer steam" score={d.components.dryer} detail={pt ? `pressure ${fmtNum(d.inputs.steam_pressure, 2)} ${pt.unit}` : "no steam pressure tag"} />
                <p className="foot">
                  Score = weighted mix of bearing 40 %, drive 30 %, dryer steam 30 %, capped at the weakest component + 20 so one failing part cannot hide behind two
                  healthy ones.
                </p>
              </div>
            </div>
          </section>

          <div className="grid g2">
            <Panel title="Bearing vibration · 30-day trend" sub="Daily mean while running, with linear projection">
              {!vt ? (
                <MissingRole what="No tag has the vibration role." />
              ) : (
                <>
                  <Projection
                    daily={d.daily_vibration}
                    projection={d.projection}
                    warn={warn}
                    normal={vt.min_value != null && vt.max_value != null ? [vt.min_value, vt.max_value] : null}
                    unit={vt.unit}
                  />
                  <div className="note" style={{ marginTop: 8 }}>
                    {projNote}
                  </div>
                </>
              )}
            </Panel>
            <Panel title="Vibration · last 10 minutes" sub={slope !== null ? `Slope over 5 min: ${signed(slope, 3)} ${vt?.unit ?? ""} per min` : undefined}>
              {vibTag ? (
                <>
                  <Lanes
                    lanes={lanesFor([vibTag], hist.data?.series, (t) => (d.inputs.vibration != null ? `${fmtNum(d.inputs.vibration, 2)} ${t.unit}` : "—"))}
                    from={end - span}
                    to={end}
                    gapMs={15_000}
                    laneHeight={170}
                  />
                  <div className="note" style={{ marginTop: 8 }}>
                    {shortNote}
                  </div>
                </>
              ) : (
                <MissingRole what="No tag has the vibration role." />
              )}
            </Panel>
          </div>

          <div className="grid g2">
            <Panel title="Motor load signature" sub="Current vs speed · 1-min means · 24 h">
              {!ct || !st ? (
                <MissingRole what="Needs the motor current and speed roles." />
              ) : n ? (
                <>
                  <Scatter
                    points={d.load_signature.map((p, k) => ({ x: p.x, y: p.y, t: p.t, hi: p.residual != null && Math.abs(p.residual) > band, recent: k >= n - 5 }))}
                    model={d.model}
                    band={band}
                    xLabel={`Speed (${st.unit})`}
                    yLabel={`Current (${ct.unit})`}
                    highlight="outside the band"
                  />
                  <div className="note" style={{ marginTop: 8 }}>
                    {d.model
                      ? `Dashed line: expected current for the speed (${fmtNum(d.model.b, 1)} ${ct.unit} + ${fmtNum(d.model.m, 3)} ${ct.unit} per ${st.unit}, fitted on ${d.model.n} running minutes). Band ±${fmtNum(band, 1)} ${ct.unit}. Right now the drive is ${signed(d.inputs.residual, 1)} ${ct.unit} from expected${d.inputs.residual != null && Math.abs(d.inputs.residual) > band ? ", outside the band" : ""}.`
                      : "Not enough running minutes to fit the expected current yet."}
                  </div>
                </>
              ) : (
                <p className="muted">Not enough data yet.</p>
              )}
            </Panel>
            <Panel title="Vibration vs speed" sub="1-min means · 24 h · separates speed effect from wear">
              {!vt || !st ? (
                <MissingRole what="Needs the vibration and speed roles." />
              ) : d.vibration_vs_speed.length ? (
                <>
                  <Scatter
                    points={d.vibration_vs_speed.map((p, k, a) => ({ x: p.x, y: p.y, t: p.t, hi: p.above, recent: k >= a.length - 5 }))}
                    model={d.vibration_regression}
                    xLabel={`Speed (${st.unit})`}
                    yLabel={`Vibration (${vt.unit})`}
                    highlight="above the cloud"
                  />
                  <div className="note" style={{ marginTop: 8 }}>
                    Vibration normally scales with speed. Points sitting above the cloud at the same speed are the early sign of wear, even when the value is still inside its
                    alarm limit.
                  </div>
                </>
              ) : (
                <p className="muted">Not enough data yet.</p>
              )}
            </Panel>
          </div>
        </>
      )}
    </div>
  );
}
