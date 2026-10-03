import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api";
import { CheckItem, DataQuality as DQ, useAnalytics } from "../../assetApi";
import { useAuth } from "../../auth";
import { useAsset } from "../../hooks";
import { Chip, fmtDT, fmtDur, fmtNum, fmtPct, Panel, ScreenHead, Tile, WindowNotes } from "../../components/console";
import { IntervalChart } from "../../components/charts";
import { ErrorText, Loading } from "../../components/ui";

const RANGES = [
  { h: 1, label: "1 h" },
  { h: 24, label: "24 h" },
  { h: 168, label: "7 days" },
];

/** The four bytes of a float32 as it would sit in two Modbus registers (big-endian, ABCD). */
function floatBytes(v: number): number[] {
  const b = new DataView(new ArrayBuffer(4));
  b.setFloat32(0, v);
  return [0, 1, 2, 3].map((i) => b.getUint8(i));
}

function fromBytes(bytes: number[]): number {
  const b = new DataView(new ArrayBuffer(4));
  bytes.forEach((x, i) => b.setUint8(i, x));
  return b.getFloat32(0);
}

const hex = (bytes: number[]) => bytes.map((x) => x.toString(16).padStart(2, "0").toUpperCase());

/** What the same registers would read as with a different byte/word order. */
const ORDERS: { key: string; label: string; perm: number[] }[] = [
  { key: "CDAB", label: "words swapped", perm: [2, 3, 0, 1] },
  { key: "BADC", label: "bytes swapped", perm: [1, 0, 3, 2] },
  { key: "DCBA", label: "both swapped", perm: [3, 2, 1, 0] },
];

const msText = (ms: number | null) => (ms == null ? "—" : ms < 1000 ? `${Math.round(ms)} ms` : `${fmtNum(ms / 1000, 2)} s`);

function plausible(v: number) {
  const a = Math.abs(v);
  return Number.isFinite(v) && (a === 0 || (a > 1e-3 && a < 1e6));
}

function Checklist({ deviceId, items }: { deviceId: string; items: CheckItem[] }) {
  const { can } = useAuth();
  const qc = useQueryClient();
  const base = `/assets/${encodeURIComponent(deviceId)}/checklist`;
  const refresh = () => qc.invalidateQueries({ queryKey: ["asset", deviceId, "data-quality"] });
  const patch = useMutation({ mutationFn: ({ id, status }: { id: number; status: string }) => api(`${base}/${id}`, { method: "PATCH", body: { status } }), onSuccess: refresh });
  const add = useMutation({ mutationFn: (title: string) => api(base, { method: "POST", body: { title, status: "open" } }), onSuccess: refresh });
  const del = useMutation({ mutationFn: (id: number) => api(`${base}/${id}`, { method: "DELETE" }), onSuccess: refresh });
  const chip = { open: <Chip cls="warn">Open</Chip>, assumed: <Chip cls="info">Assumed</Chip>, done: <Chip cls="ok">Done</Chip> };
  const open = items.filter((i) => i.status === "open").length;
  return (
    <Panel
      title="Commissioning checklist"
      sub={`${open} open · ${items.length - open} assumed or done`}
      actions={
        can("admin") && (
          <button
            className="small"
            onClick={() => {
              const t = prompt("New checklist item");
              if (t?.trim()) add.mutate(t.trim());
            }}
          >
            Add item
          </button>
        )
      }
    >
      <ErrorText error={patch.error || add.error || del.error} />
      <div className="checklist">
        {items.map((i) => (
          <div className="check" key={i.id}>
            {can("admin") ? (
              <select value={i.status} onChange={(e) => patch.mutate({ id: i.id, status: e.target.value })} aria-label={`Status of ${i.title}`} style={{ minWidth: 104 }}>
                <option value="open">Open</option>
                <option value="assumed">Assumed</option>
                <option value="done">Done</option>
              </select>
            ) : (
              chip[i.status]
            )}
            <div style={{ flex: 1 }}>
              <b>{i.title}</b>
              {i.detail && <div className="small secondary">{i.detail}</div>}
              {i.updated_by && (
                <div className="small muted">
                  {i.updated_by} · {fmtDT(i.updated_at)}
                </div>
              )}
            </div>
            {can("admin") && (
              <button className="small ghost danger" onClick={() => confirm(`Remove "${i.title}"?`) && del.mutate(i.id)} aria-label={`Remove ${i.title}`}>
                ✕
              </button>
            )}
          </div>
        ))}
      </div>
    </Panel>
  );
}

export function DataQuality() {
  const { asset } = useAsset();
  const [hours, setHours] = useState(24);
  const q = useAnalytics<DQ>(asset?.device_id, "data-quality", hours, 30_000);
  const d = q.data;
  if (!asset) return <div className="panel empty">No asset selected.</div>;
  const burst = d?.update_interval.burst;

  return (
    <div className="screen">
      <ScreenHead
        eyebrow={`Platform · ${asset.device_id}`}
        title="Data quality & link"
        desc="Can the numbers be trusted? Completeness per signal, how regularly records arrive, every communication outage, and the raw register values for commissioning checks."
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
      ) : (
        <>
          <WindowNotes window={d.window} />
          <div className="tiles">
            <Tile k="Completeness" v={fmtPct(d.completeness)} unit="%" cls={d.completeness != null && d.completeness < 0.95 ? "warn" : "good"} s={`vs 1 record / ${d.interval_s} s`} />
            <Tile k="Poll success (1 h)" v={fmtPct(d.poll_success_1h)} unit="%" cls={d.poll_success_1h != null && d.poll_success_1h < 0.95 ? "warn" : undefined} />
            <Tile k="No data" v={fmtDur(d.comms_lost_s)} s={`${d.comms_events.length} outage(s) > ${d.comms_timeout_s} s`} cls={d.comms_events.length ? "warn" : undefined} />
            <Tile k="Rejected reads" v={d.bad_reads} s="out-of-range values discarded" cls={d.bad_reads ? "warn" : undefined} />
            <Tile
              k="Update interval"
              v={msText(d.update_interval.p50_ms)}
              unit="median"
              s={d.update_interval.p95_ms != null ? `p95 ${msText(d.update_interval.p95_ms)}` : undefined}
            />
            <Tile k="Delivery pattern" v={burst ? `${burst.records}×` : "steady"} s={burst ? `burst every ${fmtNum(burst.period_s, 1)} s` : "one record at a time"} />
          </div>
          {burst && (
            <p className="note">
              The gateway delivers about {burst.records} records together every {fmtNum(burst.period_s, 1)} s. Each record still carries its own time, so trends
              are unaffected; only outages longer than {d.comms_timeout_s} s count as lost communication (set in Asset configuration).
            </p>
          )}
          <div className="grid g-7-5">
            <Panel title="Completeness by signal" sub={`expected ${d.expected_per_tag.toLocaleString()} records each since the first record in the range`}>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Signal</th>
                      <th className="num">Good</th>
                      <th className="num">Rejected</th>
                      <th className="num">Missing</th>
                      <th className="num">Complete %</th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.per_tag.map((t) => (
                      <tr key={t.tag}>
                        <td>
                          <b>{t.label}</b> <span className="mono muted small">{t.tag}</span>
                        </td>
                        <td className="num">{t.good.toLocaleString()}</td>
                        <td className="num">{t.bad.toLocaleString()}</td>
                        <td className="num">{t.missing.toLocaleString()}</td>
                        <td className="num">{fmtPct(t.completeness)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Panel>
            <Panel title="Time between records" sub="last 15 min of data; dashed line = comms timeout">
              {d.update_interval.series.length ? <IntervalChart points={d.update_interval.series} timeoutS={d.comms_timeout_s} /> : <div className="empty">No records.</div>}
            </Panel>
          </div>
          <Panel title="Communication outages" sub={`gaps longer than ${d.comms_timeout_s} s, newest first`}>
            {d.comms_events.length ? (
              <div className="table-wrap" style={{ maxHeight: 320, overflowY: "auto" }}>
                <table>
                  <thead>
                    <tr>
                      <th>Lost</th>
                      <th>Restored</th>
                      <th className="num">Duration</th>
                      <th className="num">Records missed</th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.comms_events
                      .slice()
                      .reverse()
                      .map((e) => (
                        <tr key={e.start}>
                          <td className="small nowrap">{fmtDT(e.start)}</td>
                          <td className="small nowrap">{e.end ? fmtDT(e.end) : <Chip cls="comms">Still lost</Chip>}</td>
                          <td className="num">{fmtDur(e.seconds)}</td>
                          <td className="num">{e.missing_records.toLocaleString()}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="empty">No outages in this range.</div>
            )}
          </Panel>
          <Panel title="Raw registers" sub={`latest values · configured byte order ${d.byte_order}`}>
            {d.registers.length ? (
              <>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Address</th>
                        <th>Tag</th>
                        <th>Type</th>
                        <th className="num">Value</th>
                        <th>Registers (hex)</th>
                        <th>Same registers read in another order</th>
                        <th>Updated</th>
                      </tr>
                    </thead>
                    <tbody>
                      {d.registers.map((r) => {
                        const isFloat = r.data_type === "float32" && typeof r.value_num === "number";
                        const bytes = isFloat ? floatBytes(r.value_num!) : null;
                        const h = bytes ? hex(bytes) : null;
                        return (
                          <tr key={r.address}>
                            <td className="mono">{r.address}</td>
                            <td className="mono">{r.tag}</td>
                            <td className="small">{r.data_type}</td>
                            <td className="num">{r.value_num != null ? fmtNum(r.value_num, isFloat ? 3 : 0) : r.value_text ?? "—"}</td>
                            <td className="mono small">{h ? `${h[0]}${h[1]} ${h[2]}${h[3]}` : r.value_num != null ? (r.value_num & 0xffff).toString(16).padStart(4, "0").toUpperCase() : "—"}</td>
                            <td className="mono small">
                              {bytes
                                ? ORDERS.map((o) => {
                                    const v = fromBytes(o.perm.map((i) => bytes[i]));
                                    return (
                                      <div key={o.key} title={o.label} className={plausible(v) ? undefined : "muted"}>
                                        {o.key}: {plausible(v) ? fmtNum(v, 3) : v.toExponential(2)}
                                      </div>
                                    );
                                  })
                                : "—"}
                            </td>
                            <td className="small nowrap">{r.ts ? fmtDT(r.ts) : "—"}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                <p className="note" style={{ marginTop: 10 }}>
                  To confirm the byte order, compare one value with the machine HMI. If the HMI matches one of the alternative columns instead, change the byte
                  order on the gateway (or in Asset configuration).
                </p>
              </>
            ) : (
              <div className="empty">No register map for this asset. Add one in Asset configuration.</div>
            )}
          </Panel>
          <Checklist deviceId={asset.device_id} items={d.checklist} />
        </>
      )}
    </div>
  );
}
