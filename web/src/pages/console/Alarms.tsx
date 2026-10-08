import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alarm, api, qs } from "../../api";
import { useDevice } from "../../assetApi";
import { useAuth } from "../../auth";
import { formatValue } from "../../format";
import { mergeLive, runningNow, useAsset } from "../../hooks";
import { useLiveDevice } from "../../live";
import { Chip, fmtDT, fmtDur, fmtNum, Panel, ScreenHead, tagFault, Tile } from "../../components/console";
import { HourBars } from "../../components/charts";
import { ErrorText, Loading, useNow } from "../../components/ui";
import { orderTags } from "./Live";
import { ExplainLines } from "../../components/rules";
import { SHIFTS, ShiftKey } from "../../shifts";


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

function sourceName(a: { rule_name: string; message?: string }): string {
  return a.rule_name || a.message || "Alarm";
}

function inWindow(iso: string | null | undefined, span: { from: number; to: number } | null): boolean {
  if (!span) return true;
  if (!iso) return false;
  const t = new Date(iso).getTime();
  return t >= span.from && t < span.to;
}

interface AlarmSummary {
  active_critical: number;
  active_warning: number;
  unacked_active: number;
  unacked: number;
  count: number;
  per_hour_avg: number;
  peak_hour: number;
  top_sources: { source: string; count: number }[];
  history: Alarm[];
}

function sevChip(a: Alarm) {
  if (a.rule_name.toLowerCase().includes("offline") || a.message.toLowerCase().includes("offline")) return <Chip cls="comms">Comms</Chip>;
  return a.severity === "critical" ? <Chip cls="crit">Critical</Chip> : a.severity === "warning" ? <Chip cls="warn">Warning</Chip> : <Chip cls="info">Info</Chip>;
}

function AckButton({ a }: { a: Alarm }) {
  const qc = useQueryClient();
  const ack = useMutation({
    mutationFn: () => api(`/alarms/${a.id}/ack`, { method: "POST", body: { comment: "" } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alarms"] }),
  });
  return (
    <button className="small" onClick={() => ack.mutate()} disabled={ack.isPending} title={ack.error ? String(ack.error) : undefined}>
      Acknowledge
    </button>
  );
}

/** A condition shown on the plant overview right now (not a recorded alarm, nothing to acknowledge). */
export interface LiveCondition {
  tag: string;
  cls: Parameters<typeof Chip>[0]["cls"];
  label: string;
  text: string;
  value: string;
  seen: string | number | null | undefined;
}

/** Active alarms in one table: live conditions first, then recorded alarms (still in alarm, or cleared but not
 *  acknowledged). Unacknowledged rows carry a coloured left edge; guidance (context) under the message. */
export function AlarmTable({
  alarms,
  conditions = [],
  compact = false,
  showDevice = false,
  deviceId,
  focusTag,
}: {
  alarms: Alarm[];
  conditions?: LiveCondition[];
  compact?: boolean;
  showDevice?: boolean;
  deviceId?: string;
  focusTag?: string | null;
}) {
  const { can } = useAuth();
  const now = useNow(5000);
  return (
    <div className="table-wrap" style={{ maxHeight: 560, overflowY: "auto" }}>
      <table>
        <thead>
          <tr>
            <th>Severity</th>
            <th>Alarm</th>
            {showDevice && <th>Asset</th>}
            {!compact && <th className="num">Value</th>}
            <th>Raised</th>
            <th>{compact ? "" : "Acknowledged"}</th>
          </tr>
        </thead>
        <tbody>
          {conditions.map((f) => (
            <tr key={`live-${f.tag}`} id={`fault-${f.tag}`} className={f.tag === focusTag ? "hit" : undefined}>
              <td>
                <Chip cls={f.cls}>{f.label}</Chip>
              </td>
              <td>
                <div style={{ fontWeight: 600 }}>{f.text}</div>
                <div className="ctx">Live condition on the plant overview, not a recorded alarm.</div>
              </td>
              {showDevice && <td className="mono small">{deviceId ?? "—"}</td>}
              {!compact && <td className="num">{f.value}</td>}
              <td className="small nowrap">
                {fmtDT(f.seen ?? undefined)}
                <div className="muted">now</div>
              </td>
              <td className="small muted">Clears by itself</td>
            </tr>
          ))}
          {alarms.map((a) => (
            <tr
              key={a.id}
              id={a.tag ? `alarm-${a.tag}` : undefined}
              className={[a.acked_at ? "" : `unack${a.severity === "critical" ? "" : " w"}`, focusTag && a.tag === focusTag ? "hit" : ""].filter(Boolean).join(" ") || undefined}
            >
              <td>
                {sevChip(a)}
                {a.cleared_at && (
                  <div style={{ marginTop: 4 }}>
                    <Chip cls="ok" title="The condition has cleared; the alarm still needs acknowledging">Cleared</Chip>
                  </div>
                )}
              </td>
              <td>
                <div style={{ fontWeight: 600 }}>{a.message || a.rule_name}</div>
                {a.explain ? <ExplainLines e={a.explain} /> : a.context && <div className="ctx">{a.context}</div>}
              </td>
              {showDevice && <td className="mono small">{a.device_id}</td>}
              {!compact && <td className="num">{a.trigger_value != null ? fmtNum(a.trigger_value, 2) : "—"}</td>}
              <td className="small nowrap">
                {fmtDT(a.raised_at)}
                <div className="muted">
                  {a.cleared_at
                    ? `cleared ${fmtDT(a.cleared_at)} · lasted ${fmtDur((new Date(a.cleared_at).getTime() - new Date(a.raised_at).getTime()) / 1000)}`
                    : `for ${fmtDur((now - new Date(a.raised_at).getTime()) / 1000)}`}
                </div>
              </td>
              <td className="small">
                {a.acked_at ? (
                  <span className="muted">
                    {a.acked_by}
                    <br />
                    {fmtDT(a.acked_at)}
                  </span>
                ) : can("operator") ? (
                  <AckButton a={a} />
                ) : (
                  <Chip cls="warn">Unacknowledged</Chip>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Alarms() {
  const { asset } = useAsset();
  const { can } = useAuth();
  const qc = useQueryClient();
  const [params] = useSearchParams();
  const focusTag = params.get("tag");
  const [scope, setScope] = useState<"asset" | "all">("asset");
  const [date, setDate] = useState("");
  const [timeFrom, setTimeFrom] = useState("");
  const [timeTo, setTimeTo] = useState("");
  const [shift, setShift] = useState<ShiftKey | "">("");
  const [source, setSource] = useState("");
  const detail = useDevice(asset?.device_id);
  const live = useLiveDevice(asset?.device_id);
  const deviceId = scope === "asset" ? asset?.device_id : undefined;
  const range = customWindow(date, timeFrom, timeTo);
  const summary = useQuery({
    queryKey: ["alarms", "summary", deviceId, range?.from ?? "24h", range?.to ?? ""],
    queryFn: () =>
      api<AlarmSummary>(
        `/alarms/summary${qs({
          device_id: deviceId,
          hours: 24,
          from: range ? new Date(range.from).toISOString() : undefined,
          to: range ? new Date(range.to).toISOString() : undefined,
        })}`,
      ),
    refetchInterval: !range || range.to > Date.now() ? 15_000 : false,
  });
  const active = useQuery({
    queryKey: ["alarms", "active", deviceId ?? "all"],
    queryFn: () => api<Alarm[]>(`/alarms${qs({ state: "active", device_id: deviceId })}`),
    refetchInterval: 15_000,
  });
  const ackAll = useMutation({
    mutationFn: () => api<{ acknowledged: number }>("/alarms/ack-all", { method: "POST", body: { device_id: deviceId ?? null, comment: "" } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alarms"] }),
  });

  const s = summary.data;
  const device = detail.data;
  const merged = device ? mergeLive(device, live) : null;
  const comms = merged ? !merged.online : false;
  const stoppedNow = !!(device && merged && runningNow(device, device.tags ?? [], merged.value, merged.status, false) === false);
  const faults = (merged && device
    ? orderTags((device.tags ?? []).filter((t) => t.data_type === "number" && t.role !== "machine_status")).flatMap((t) => {
        const fault = tagFault(t, merged.value(t), { comms, bad: merged.quality(t) === 2 });
        if (!fault) return [];
        // while stopped, values held off by their alarms (crawl speed, standby steam) are not faults
        if (!comms && merged.quality(t) !== 2 && stoppedNow && t.suppress_when_stopped) return [];
        const v = merged.value(t);
        return [{ tag: t.tag, ...fault, value: typeof v === "number" ? `${formatValue(v, t)}${t.unit ? ` ${t.unit}` : ""}` : "—", seen: merged.ts(t) ?? merged.lastSeen }];
      })
    : []);
  const faultKey = faults.map((f) => f.tag).join(",");

  useEffect(() => {
    if (!focusTag) return;
    document.getElementById(`fault-${focusTag}`)?.scrollIntoView({ block: "center" });
  }, [focusTag, faultKey]);
  const sourceCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const a of s?.history ?? []) counts.set(sourceName(a), (counts.get(sourceName(a)) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  }, [s?.history]);
  const rangeRows = (s?.history ?? []).filter((a) => !source || sourceName(a) === source);
  // each alarm in one place: still needing the operator -> Active alarms; cleared and acknowledged -> Event history
  const historyRows = rangeRows.filter((a) => a.cleared_at && a.acked_at);
  const hours = (() => {
    const out = new Map<number, number>();
    if (range) {
      const start = Math.floor(range.from / 3600_000) * 3600_000;
      const last = Math.max(start, range.to - 1);
      for (let h = start; h <= last; h += 3600_000) out.set(h, 0);
    } else {
      const h0 = Math.floor(Date.now() / 3600_000) * 3600_000;
      for (let i = 23; i >= 0; i--) out.set(h0 - i * 3600_000, 0);
    }
    for (const a of rangeRows) {
      const h = Math.floor(new Date(a.raised_at).getTime() / 3600_000) * 3600_000;
      if (out.has(h)) out.set(h, (out.get(h) ?? 0) + 1);
    }
    return [...out].map(([h, count]) => ({ h, count }));
  })();
  const listedSources = source && !sourceCounts.slice(0, 10).some(([name]) => name === source)
    ? [[source, sourceCounts.find(([name]) => name === source)?.[1] ?? 0] as [string, number], ...sourceCounts.slice(0, 9)]
    : sourceCounts.slice(0, 10);
  const maxSrc = Math.max(1, ...listedSources.map(([, count]) => count));
  const nowMs = Date.now();
  const dayOfData = ymd(asset?.last_seen ? new Date(asset.last_seen).getTime() : nowMs);
  const showFaults = !range || (nowMs >= range.from && nowMs < range.to);
  const stillActive = (active.data ?? []).filter((a) => inWindow(a.raised_at, range) && (!source || sourceName(a) === source));
  const clearedUnacked = rangeRows.filter((a) => a.cleared_at && !a.acked_at);
  const activeRows = [...stillActive, ...clearedUnacked];
  const activeSourceKey = (active.data ?? []).map((a) => sourceName(a)).join("\n");
  useEffect(() => {
    if (!source || !s) return;
    const stillThere = s.history.some((a) => sourceName(a) === source) || activeSourceKey.split("\n").includes(source);
    if (!stillThere) setSource("");
  }, [source, s, activeSourceKey]);
  const sourceTags = new Set(
    [...(s?.history ?? []), ...(active.data ?? [])].flatMap((a) => (!source || sourceName(a) === source) && a.tag ? [a.tag] : []),
  );
  const shownFaults = faults.filter((f) => !source || sourceTags.has(f.tag) || f.text.toLowerCase().includes(source.toLowerCase()));
  const raisedCount = source ? rangeRows.length : (s?.count ?? 0);
  const raisedAvg = source ? raisedCount / (range ? Math.max((range.to - range.from) / 3600_000, 1 / 60) : 24) : (s?.per_hour_avg ?? 0);
  const raisedPeak = source ? Math.max(0, ...hours.map((h) => h.count)) : (s?.peak_hour ?? 0);
  const critCount = source ? activeRows.filter((a) => a.severity === "critical").length : (s?.active_critical ?? 0);
  const warnCount = source ? activeRows.filter((a) => a.severity !== "critical").length : (s?.active_warning ?? 0);
  const unackedShown = (source ? stillActive : active.data ?? []).filter((a) => !a.acked_at).length + clearedUnacked.length;
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

  return (
    <div className="screen">
      <ScreenHead
        eyebrow="Operations"
        title="Alarms & events"
        actions={
          <div className="row">
            <div className="segmented" role="group" aria-label="Scope">
              <button className={scope === "asset" ? "on" : ""} aria-pressed={scope === "asset"} onClick={() => setScope("asset")}>
                {asset?.name || asset?.device_id || "This asset"}
              </button>
              <button className={scope === "all" ? "on" : ""} aria-pressed={scope === "all"} onClick={() => setScope("all")}>
                All assets
              </button>
            </div>
            {can("operator") && (
              <button className="primary" disabled={!s?.unacked || ackAll.isPending} onClick={() => confirm(`Acknowledge all ${s?.unacked ?? 0} unacknowledged alarms?`) && ackAll.mutate()}>
                Acknowledge all
              </button>
            )}
          </div>
        }
      />
      <section className="panel">
      <div className="toolbar">
        <div className="segmented" role="group" aria-label="Time range">
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
        <label className="filter">
          Source
          <select value={source} onChange={(e) => setSource(e.target.value)} aria-label="Alarm source">
            <option value="">All sources</option>
            {sourceCounts.map(([name, count]) => (
              <option key={name} value={name}>
                {name} ({count})
              </option>
            ))}
          </select>
        </label>
      </div>
      </section>
      <ErrorText error={ackAll.error || summary.error} />
      {summary.isLoading ? (
        <Loading />
      ) : (
        <div className="tiles">
          <Tile k="Active critical" v={critCount} cls={critCount ? "crit" : undefined} />
          <Tile k="Active warning" v={warnCount} cls={warnCount ? "warn" : undefined} />
          <Tile k="Unacknowledged" v={unackedShown} s={`${clearedUnacked.length} of them already cleared${source ? ` · ${source}` : ""}`} cls={unackedShown ? "warn" : undefined} />
          <Tile k={range || source ? "Raised in range" : "Raised in 24 h"} v={raisedCount} s={`${source ? `${source} · ` : ""}${rangeText} · avg ${fmtNum(raisedAvg, 1)}/h · peak ${raisedPeak}/h`} />
        </div>
      )}
      <Panel
        title="Active alarms"
        sub={`${stillActive.length} active · ${clearedUnacked.length} cleared, not yet acknowledged · newest first`}
      >
        {activeRows.length > 0 || (showFaults && shownFaults.length > 0) ? (
          <AlarmTable
            alarms={activeRows}
            conditions={showFaults ? shownFaults : []}
            showDevice={scope === "all"}
            deviceId={asset?.device_id}
            focusTag={focusTag}
          />
        ) : (
          <div className="empty">{source ? `No active alarms from ${source}.` : range ? "No active alarms in this range." : "No active alarms."}</div>
        )}
      </Panel>
      <div className="grid g-7-5">
        <Panel title="Alarms raised per hour" sub={source ? `${source} · ${rangeText}` : rangeText}>
          <HourBars hours={hours} />
        </Panel>
        <Panel title="Top alarm sources" sub={source ? `${source} · click again to clear` : `${rangeText} · click a source`}>
          {listedSources.length ? (
            <div className="bars" role="group" aria-label="Alarm sources">
              {listedSources.map(([name, count]) => (
                <button
                  type="button"
                  className={source === name ? "bar on" : "bar"}
                  key={name}
                  aria-pressed={source === name}
                  title={source === name ? `Clear ${name}` : `Show ${name}`}
                  onClick={() => setSource((cur) => (cur === name ? "" : name))}
                >
                  <span>{name}</span>
                  <div className="tr">
                    <i style={{ width: `${(count / maxSrc) * 100}%` }} />
                  </div>
                  <span className="n">{count}</span>
                </button>
              ))}
            </div>
          ) : (
            <div className="empty">{range ? "No alarms in this range." : "No alarms in the last 24 hours."}</div>
          )}
        </Panel>
      </div>
      <Panel
        title="Event history"
        sub={`cleared and acknowledged · ${source ? `${source} · ${rangeText}` : range ? `raised ${rangeText}` : "raised in the last 24 h"}`}
      >
        {historyRows.length ? (
          <div className="table-wrap" style={{ maxHeight: 420, overflowY: "auto" }}>
            <table>
              <thead>
                <tr>
                  <th>Raised</th>
                  <th>Severity</th>
                  <th>Alarm</th>
                  {scope === "all" && <th>Asset</th>}
                  <th className="num">Value</th>
                  <th>Cleared</th>
                  <th>Duration</th>
                  <th>Acknowledged by</th>
                </tr>
              </thead>
              <tbody>
                {historyRows.map((a) => (
                  <tr key={a.id} className={focusTag && a.tag === focusTag ? "hit" : undefined}>
                    <td className="small nowrap">{fmtDT(a.raised_at)}</td>
                    <td>{sevChip(a)}</td>
                    <td>
                      {a.message || a.rule_name}
                      {a.explain && <ExplainLines e={a.explain} />}
                    </td>
                    {scope === "all" && <td className="mono small">{a.device_id}</td>}
                    <td className="num">{a.trigger_value != null ? fmtNum(a.trigger_value, 2) : "—"}</td>
                    <td className="small nowrap">{a.cleared_at ? fmtDT(a.cleared_at) : <Chip cls="crit">Active</Chip>}</td>
                    <td className="small nowrap">{fmtDur(((a.cleared_at ? new Date(a.cleared_at).getTime() : Date.now()) - new Date(a.raised_at).getTime()) / 1000)}</td>
                    <td className="small">
                      {a.acked_by}
                      <div className="muted">{fmtDT(a.acked_at)}</div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty">
            {source ? `No finished alarms from ${source}.` : range ? "No finished alarms in this range." : "No finished alarms in the last 24 hours."} An alarm moves here once
            it has cleared and been acknowledged.
          </div>
        )}
      </Panel>
    </div>
  );
}
