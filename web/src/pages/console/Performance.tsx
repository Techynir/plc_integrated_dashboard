import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api";
import { Performance as Perf, useAnalytics, useDevice, useHistory } from "../../assetApi";
import { useAuth } from "../../auth";
import { roleTag, useAsset } from "../../hooks";
import { fmtDT, fmtDur, fmtNum, MissingRole, Panel, ScreenHead, Tile, WindowNotes } from "../../components/console";
import { Lanes, StateLegend, StateStrip } from "../../components/charts";
import { ErrorText, Loading } from "../../components/ui";
import { lanesFor } from "./Live";

const HOURS = 24;

function ReasonSelect({ deviceId, start, value, reasons }: { deviceId: string; start: number; value: string; reasons: string[] }) {
  const qc = useQueryClient();
  const save = useMutation({
    mutationFn: (reason: string) => api(`/assets/${encodeURIComponent(deviceId)}/stoppages/${start}`, { method: "PUT", body: { reason } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["asset", deviceId, "performance"] }),
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
  const perf = useAnalytics<Perf>(id, "performance", HOURS);
  const detail = useDevice(id, 60_000);
  const p = perf.data;
  const from = p ? new Date(p.window.start).getTime() : 0;
  const to = p ? new Date(p.window.end).getTime() : 0;
  const speedTag = roleTag(detail.data?.tags, "speed");
  const hist = useHistory(id, speedTag && p ? [speedTag.tag] : [], from, to, false);

  if (!asset) return <div className="panel empty">No asset selected.</div>;

  const target = p?.speed_target ?? null;
  const stops = (p?.stops ?? []).slice().reverse();
  const withData = p ? p.totals.run + p.totals.stop : 0;
  const bestAv = Math.max(...(p?.shifts ?? []).map((s) => s.availability ?? -1));

  return (
    <div className="screen">
      <ScreenHead
        eyebrow="Analytics · last 24 h"
        title="Performance & downtime"
        desc={`Run time, stops and speed from the ${roleTag(detail.data?.tags, "machine_status")?.tag ?? "machine status"} and ${speedTag?.tag ?? "speed"} tags. Time with no PLC link is left out of availability.`}
      />
      {perf.isLoading || !p ? (
        perf.error ? <ErrorText error={perf.error} /> : <Loading />
      ) : (
        <>
          <WindowNotes window={p.window} stateSource={p.state_source} />
          <div className="tiles">
            <Tile k="Availability" v={p.availability != null ? (p.availability * 100).toFixed(1) : "—"} unit="%" s="run ÷ (run + stopped)" />
            <Tile k="Run time" v={hours(p.totals.run)} unit="h" s={`of ${hours(withData)} h with data`} />
            <Tile k="Stops" v={p.stops.length} s={`${fmtDur(p.totals.stop)} total`} />
            <Tile k="Longest stop" v={p.longest_stop_s ? fmtDur(p.longest_stop_s) : "—"} />
            <Tile
              k="Avg speed · running"
              v={fmtNum(p.avg_speed_running, 1)}
              unit={p.speed_tag?.unit}
              s={target ? `target ${fmtNum(target, 1)} · ${p.avg_speed_running ? ((p.avg_speed_running / target) * 100).toFixed(1) : "—"} % of target` : "no speed target set"}
            />
            <Tile k="No PLC link" v={fmtDur(p.totals.comms)} s="excluded from availability" />
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
            <Panel title="Shift comparison" sub="A 06–14 · B 14–22 · C 22–06">
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Shift</th>
                      <th className="num">Run time</th>
                      <th className="num">Availability</th>
                      <th className="num">Stops</th>
                      <th className="num">Avg speed</th>
                      <th className="num">Moisture in range</th>
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
                        <td className="num">{s.moisture_in_range != null ? `${(s.moisture_in_range * 100).toFixed(1)} %` : "—"}</td>
                        <td className="num">{s.alarms}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="foot">▲ best availability. Shift boundaries follow local plant time ({p.timezone}).</p>
            </Panel>
            <Panel title="Stoppage log" sub="Classify reasons to build a Pareto over time">
              {stops.length ? (
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
                  No stops in the last 24 h.
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
