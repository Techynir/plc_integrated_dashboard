import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, qs } from "../../api";
import { Performance as Perf, StopEvent, useAnalytics, useDevice, useHistory } from "../../assetApi";
import { useAuth } from "../../auth";
import { roleTag, useAsset } from "../../hooks";
import { fmtDT, fmtDur, fmtNum, MissingRole, Panel, ScreenHead, Tile, WindowNotes } from "../../components/console";
import { Lanes, StateLegend, StateStrip } from "../../components/charts";
import { ErrorText, Loading } from "../../components/ui";
import { lanesFor } from "./Live";
import { SHIFTS, ShiftKey, SHIFT_HOURS } from "../../shifts";

const HOURS = 24;


function ymd(ms: number): string {
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function atLocal(date: string, time: string): number {
  const [y, m, d] = date.split("-").map(Number);
  const [hh, mm] = time.split(":").map(Number);
  return new Date(y, m - 1, d, hh, mm, 0, 0).getTime();
}

/** One calendar day, a clock range on that day, or a shift. A clock that passes midnight ends the next morning. */
function customWindow(date: string, timeFrom: string, timeTo: string): { from: number; to: number } | null {
  if (!date) return null;
  if (!timeFrom && !timeTo) {
    const from = atLocal(date, "00:00");
    return { from, to: from + 86_400_000 };
  }
  const from = atLocal(date, timeFrom || "00:00");
  let to = atLocal(date, timeTo || "23:59");
  if (to <= from) to += 86_400_000;
  return { from, to };
}

function ReasonSelect({ deviceId, start, value, reasons }: { deviceId: string; start: number; value: string; reasons: string[] }) {
  const qc = useQueryClient();
  const save = useMutation({
    mutationFn: (reason: string) => api(`/assets/${encodeURIComponent(deviceId)}/stoppages/${start}`, { method: "PUT", body: { reason } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["asset", deviceId, "performance"] });
      qc.invalidateQueries({ queryKey: ["asset", deviceId, "stoppages"] });
    },
  });
  return (
    <>
      <select value={save.isPending ? save.variables : value} onChange={(e) => save.mutate(e.target.value)} aria-label="Stop reason">
        {reasons.map((r) => (
          <option key={r}>{r}</option>
        ))}
      </select>
      <ErrorText error={save.error} />
    </>
  );
}

const hours = (s: number) => (s / 3600).toFixed(1);

export function Performance() {
  const { asset } = useAsset();
  const { can } = useAuth();
  const id = asset?.device_id;
  const [date, setDate] = useState("");
  const [timeFrom, setTimeFrom] = useState("");
  const [timeTo, setTimeTo] = useState("");
  const [shift, setShift] = useState<ShiftKey | "">("");
  const range = customWindow(date, timeFrom, timeTo);
  const perf = useAnalytics<Perf>(id, "performance", HOURS);
  const log = useQuery({
    queryKey: ["asset", id, "stoppages", range?.from ?? 0, range?.to ?? 0],
    queryFn: () =>
      api<{ stops: StopEvent[] }>(
        `/assets/${encodeURIComponent(id!)}/stoppages${qs({ from: new Date(range!.from).toISOString(), to: new Date(range!.to).toISOString() })}`,
      ),
    enabled: !!id && !!range,
  });
  const detail = useDevice(id, 60_000);
  const p = perf.data;
  const from = p ? new Date(p.window.start).getTime() : 0;
  const to = p ? new Date(p.window.end).getTime() : 0;
  const speedTag = roleTag(detail.data?.tags, "speed");
  const hist = useHistory(id, speedTag && p ? [speedTag.tag] : [], from, to, false);

  if (!asset) return <div className="panel empty">No asset selected.</div>;

  const stops = ((range ? log.data?.stops : p?.stops) ?? []).slice().reverse();
  const nowMs = Date.now();
  const dayOfData = ymd(asset.last_seen ? new Date(asset.last_seen).getTime() : nowMs);
  const rangeText = !range
    ? "last 24 h"
    : shift
      ? `shift ${shift} · ${date}`
      : timeFrom || timeTo
        ? `${date} ${timeFrom || "00:00"}–${timeTo || "24:00"}`
        : date;
  const clearRange = () => {
    setDate("");
    setTimeFrom("");
    setTimeTo("");
    setShift("");
  };
  const useShift = (key: ShiftKey) => {
    const chosen = SHIFTS.find((x) => x.key === key)!;
    setShift(key);
    setDate((d) => d || dayOfData);
    setTimeFrom(chosen.from);
    setTimeTo(chosen.to);
  };
  const useDate = (value: string) => {
    setDate(value);
    if (!value) clearRange();
  };
  const useTime = (which: "from" | "to", value: string) => {
    setShift("");
    if (which === "from") setTimeFrom(value);
    else setTimeTo(value);
    setDate((d) => d || dayOfData);
  };
  const exportFrom = range ? new Date(range.from).toISOString() : p?.window.start;
  const exportTo = range ? new Date(range.to).toISOString() : p?.window.end;
  const csv = `/api/v1/assets/${encodeURIComponent(asset.device_id)}/stoppages.csv${qs({ from: exportFrom, to: exportTo })}`;
  const withData = p ? p.totals.run + p.totals.stop : 0;
  const bestAv = Math.max(...(p?.shifts ?? []).map((s) => s.availability ?? -1));

  return (
    <div className="screen">
      <ScreenHead
        eyebrow="Analytics · last 24 h"
        title="Performance & downtime"
      />
      {perf.isLoading || !p ? (
        perf.error ? <ErrorText error={perf.error} /> : <Loading />
      ) : (
        <>
          <WindowNotes window={p.window} stateSource={p.state_source} />
          <div className="tiles">
            <Tile
              k="Availability"
              v={p.availability != null ? (p.availability * 100).toFixed(1) : "—"}
              unit="%"
              s={`run ÷ (run + stopped)${p.totals.comms >= 1 ? ` · no PLC link ${fmtDur(p.totals.comms)}, left out` : ""}`}
            />
            <Tile k="Run time" v={hours(p.totals.run)} unit="h" s={`of ${hours(withData)} h with data`} />
            <Tile k="Stops" v={p.stops.length} s={`${fmtDur(p.totals.stop)} total`} />
            <Tile k="Longest stop" v={p.longest_stop_s ? fmtDur(p.longest_stop_s) : "—"} />
            <Tile
              k="Performance"
              v={p.performance != null ? (p.performance * 100).toFixed(1) : "—"}
              unit="%"
              s={
                p.rated_actual.length
                  ? `speed while running ÷ rated · ${p.rated_actual.map((r) => `${fmtNum(r.actual, 1)} / ${fmtNum(r.rated, 1)} ${r.unit}`).join(" · ")}`
                  : "no rated speed set"
              }
              cls={p.performance != null && p.performance > 1 ? "good" : undefined}
            />
            <Tile
              k="Overall"
              v={p.overall != null ? (p.overall * 100).toFixed(1) : "—"}
              unit="%"
              s="availability × performance"
            />
          </div>

          <Panel title="Machine state · 24 h" actions={<StateLegend />}>
            <StateStrip segments={p.segments} from={from / 1000} to={to / 1000} height={58} />
            {speedTag ? (
              <div style={{ marginTop: 8 }}>
                <Lanes
                  lanes={lanesFor([speedTag], hist.data?.series, () => (p.avg_speed_running != null ? `avg ${fmtNum(p.avg_speed_running, 1)} ${speedTag.unit}` : "—"))}
                  from={from}
                  to={to}
                  gapMs={Math.max((hist.data?.bucket_s ?? 0) * 2000, 15_000)}
                  laneHeight={110}
                />
              </div>
            ) : (
              <MissingRole what="No tag has the speed role." />
            )}
          </Panel>

          <div className="grid g-7-5">
            <Panel title="Shift comparison" sub={SHIFT_HOURS}>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Shift</th>
                      <th className="num">Run</th>
                      <th className="num">Avail.</th>
                      <th className="num">Stops</th>
                      <th className="num">Avg speed</th>
                      <th className="num">Perf.</th>
                      <th className="num" title="Moisture inside its normal range while running">Moisture OK</th>
                      <th className="num">Alarms</th>
                    </tr>
                  </thead>
                  <tbody>
                    {p.shifts.map((s) => (
                      <tr key={s.shift}>
                        <td className="nowrap">
                          <b>Shift {s.shift}</b>
                        </td>
                        <td className="num">{hours(s.run_s)} h</td>
                        <td className="num">
                          {s.availability != null ? `${(s.availability * 100).toFixed(1)} %` : "—"}
                          {s.availability != null && s.availability === bestAv ? " ▲" : ""}
                        </td>
                        <td className="num">{s.stops}</td>
                        <td className="num">{fmtNum(s.avg_speed, 1)}</td>
                        <td className="num">{s.performance != null ? `${(s.performance * 100).toFixed(1)} %` : "—"}</td>
                        <td className="num">{s.moisture_in_range != null ? `${(s.moisture_in_range * 100).toFixed(1)} %` : "—"}</td>
                        <td className="num">{s.alarms}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="foot">▲ best availability. Perf. = speed while running ÷ rated speed. Shift boundaries follow local plant time ({p.timezone}).</p>
            </Panel>
            <Panel title="Stoppage log" sub={`${rangeText} · classify reasons to build a Pareto over time`}>
              <div className="toolbar" style={{ marginBottom: 10 }}>
                <div className="segmented" role="group" aria-label="Stoppage range">
                  <button type="button" className={!range ? "on" : ""} aria-pressed={!range} onClick={clearRange}>
                    24 h
                  </button>
                </div>
                <label className="filter">
                  Date
                  <input type="date" value={date} max={ymd(nowMs)} onChange={(e) => useDate(e.target.value)} />
                </label>
                <label className="filter">
                  From
                  <input type="time" value={timeFrom} onChange={(e) => useTime("from", e.target.value)} />
                </label>
                <label className="filter">
                  To
                  <input type="time" value={timeTo} onChange={(e) => useTime("to", e.target.value)} />
                </label>
                <div className="segmented" role="group" aria-label="Shift">
                  {SHIFTS.map((sft) => (
                    <button key={sft.key} type="button" className={shift === sft.key ? "on" : ""} aria-pressed={shift === sft.key} onClick={() => useShift(sft.key)}>
                      {sft.label}
                    </button>
                  ))}
                </div>
                <a className="btn" href={csv}>
                  Export
                </a>
              </div>
              {range && log.isLoading ? (
                <Loading />
              ) : range && log.error ? (
                <ErrorText error={log.error} />
              ) : stops.length ? (
                <div className="table-wrap" style={{ maxHeight: 360, overflowY: "auto" }}>
                  <table>
                    <thead>
                      <tr>
                        <th>Start</th>
                        <th className="num">Duration</th>
                        <th>Shift</th>
                        <th>Reason</th>
                      </tr>
                    </thead>
                    <tbody>
                      {stops.map((s) => (
                        <tr key={s.start}>
                          <td className="mono small nowrap">{fmtDT(s.start)}</td>
                          <td className="num">{s.end ? fmtDur(s.seconds) : "ongoing"}</td>
                          <td>{s.shift}</td>
                          <td>{can("operator") ? <ReasonSelect deviceId={asset.device_id} start={s.start} value={s.reason} reasons={p.reasons} /> : s.reason}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="muted" style={{ margin: 0 }}>
                  {range ? "No stops in this range." : "No stops in the last 24 h."}
                </p>
              )}
            </Panel>
          </div>

          <div className="callout">
            <div>
              <b>Phase 2 · add three tags to unlock production and OEE.</b> Tonnes per day needs basis weight (GSM) and deckle width. Energy per tonne needs a kW tag.
              True OEE needs a reject or broke signal and the rated speed per grade.
            </div>
          </div>
        </>
      )}
    </div>
  );
}
