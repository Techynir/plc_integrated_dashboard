import { useState } from "react";
import { QualityData, useAnalytics } from "../../assetApi";
import { useAsset } from "../../hooks";
import { Chip, fmtNum, fmtPct, MissingRole, Panel, ScreenHead, Tile, WindowNotes } from "../../components/console";
import { Histogram, Scatter, SpcChart } from "../../components/charts";
import { ErrorText, Loading } from "../../components/ui";

const RANGES = [
  { h: 1, label: "1 h" },
  { h: 8, label: "8 h" },
  { h: 24, label: "24 h" },
  { h: 72, label: "3 days" },
];

function capCls(v: number | null | undefined) {
  if (v == null) return undefined;
  return v < 1 ? "crit" : v < 1.33 ? "warn" : "good";
}

export function Quality() {
  const { asset } = useAsset();
  const [hours, setHours] = useState(8);
  const q = useAnalytics<QualityData>(asset?.device_id, "quality", hours);
  const d = q.data;
  if (!asset) return <div className="panel empty">No asset selected.</div>;

  const mt = d?.moisture_tag;
  const unit = mt?.unit ?? "";
  const dec = mt?.decimals ?? 2;
  const cap = d?.capability;
  const noVar = !!d?.limits?.no_variation;

  return (
    <div className="screen">
      <ScreenHead
        eyebrow={`Analytics · ${asset.device_id}`}
        title="Process quality"
        desc="Paper moisture while the machine is running: a control chart of 1-minute averages (I-MR), its spread against the normal range, and how it follows dryer steam pressure."
        actions={
          <div className="segmented" role="group" aria-label="Time range">
            {RANGES.map((r) => (
              <button key={r.h} className={hours === r.h ? "on" : ""} aria-pressed={hours === r.h} onClick={() => setHours(r.h)}>
                {r.label}
              </button>
            ))}
          </div>
        }
      />
      {q.error && <ErrorText error={q.error} />}
      {!d ? (
        <Loading />
      ) : d.missing ? (
        <MissingRole what="No tag has the moisture role." />
      ) : (
        <>
          <WindowNotes window={d.window} />
          <div className="tiles">
            <Tile k="Mean moisture" v={fmtNum(cap?.mean, dec + 1)} unit={unit} s={mt?.min_value != null ? `normal ${mt.min_value}–${mt.max_value} ${unit}` : "no normal range set"} />
            <Tile k="Std deviation" v={fmtNum(cap?.sd, 3)} unit={unit} s={`${(cap?.n ?? 0).toLocaleString()} running samples`} />
            <Tile k="Cp" v={fmtNum(cap?.cp, 2)} cls={capCls(cap?.cp)} s="spread vs normal range · ≥1.33 capable" />
            <Tile k="Cpk" v={fmtNum(cap?.cpk, 2)} cls={capCls(cap?.cpk)} s="also counts centring" />
            <Tile k="In normal range" v={fmtPct(cap?.in_spec)} unit="%" />
            <Tile
              k="Out of control"
              v={d.out_of_control ?? 0}
              cls={d.out_of_control ? "crit" : d.run_rule ? "warn" : undefined}
              s={`${d.run_rule ?? 0} minute(s) in an 8-point run`}
            />
          </div>
          {noVar && (
            <div className="alert warning">
              <Chip cls="warn">No variation</Chip>
              <span>
                Moisture has been exactly constant ({fmtNum(cap?.mean, dec + 1)} {unit}) over this range, so control limits and Cp/Cpk cannot be calculated. A
                real sensor always varies a little: check that the moisture register is live and not a fixed test value.
              </span>
            </div>
          )}
          <Panel
            title="Control chart — 1-minute means"
            sub={
              d.limits && !noVar
                ? `CL ${fmtNum(d.limits.cl, dec + 1)} · UCL ${fmtNum(d.limits.ucl, dec + 1)} · LCL ${fmtNum(d.limits.lcl, dec + 1)} ${unit}; green = normal range`
                : "running minutes only"
            }
          >
            {d.minute_means?.length ? (
              <>
                <SpcChart points={d.minute_means} limits={d.limits ?? null} spec={d.spec ?? { lsl: null, usl: null }} unit={unit} decimals={dec} />
                <div className="legend">
                  <span><i style={{ background: "var(--trace)" }} />1-min mean</span>
                  <span><i style={{ background: "var(--critical)" }} />beyond control limits</span>
                  <span><i style={{ background: "var(--warning)" }} />8 in a row on one side</span>
                  <span><i style={{ background: "var(--band)", border: "1px solid var(--good)" }} />normal range</span>
                </div>
              </>
            ) : (
              <div className="empty">No running minutes with moisture data in this range.</div>
            )}
          </Panel>
          <div className="grid g2">
            <Panel title="Distribution" sub="every running sample vs normal range (LSL/USL)">
              {d.histogram?.length ? (
                <Histogram bins={d.histogram} spec={d.spec ?? { lsl: null, usl: null }} mean={cap?.mean ?? null} unit={unit} />
              ) : (
                <div className="empty">No data.</div>
              )}
            </Panel>
            <Panel
              title="Moisture vs steam pressure"
              sub={d.regression ? `r = ${fmtNum(d.regression.r, 2)} · slope ${fmtNum(d.regression.m, 3)} ${unit} per ${d.steam_tag?.unit ?? "unit"}` : "1-minute means while running"}
            >
              {!d.steam_tag ? (
                <MissingRole what="No tag has the steam pressure role." />
              ) : d.scatter?.length ? (
                <>
                  <Scatter
                    points={d.scatter}
                    model={d.regression}
                    xLabel={`${d.steam_tag.label} (${d.steam_tag.unit})`}
                    yLabel={`${mt?.label} (${unit})`}
                  />
                  <p className="note">
                    Higher steam pressure dries the sheet more, so the points normally slope down. A flat or rising cloud points to a sensor or control issue.
                  </p>
                </>
              ) : (
                <div className="empty">Not enough running minutes.</div>
              )}
            </Panel>
          </div>
        </>
      )}
    </div>
  );
}
