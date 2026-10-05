import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alarm, api, qs } from "../../api";
import { useDevice } from "../../assetApi";
import { useAuth } from "../../auth";
import { formatValue } from "../../format";
import { mergeLive, useAsset } from "../../hooks";
import { useLiveDevice } from "../../live";
import { Chip, fmtDT, fmtDur, fmtNum, Panel, ScreenHead, tagFault, Tile } from "../../components/console";
import { HourBars } from "../../components/charts";
import { ErrorText, Loading, useNow } from "../../components/ui";
import { orderTags } from "./Live";

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

/** Active alarms: unacknowledged rows carry a coloured left edge; guidance (context) under the message. */
export function AlarmTable({ alarms, compact = false, showDevice = false, focusTag }: { alarms: Alarm[]; compact?: boolean; showDevice?: boolean; focusTag?: string | null }) {
  const { can } = useAuth();
  const now = useNow(5000);
  return (
    <div className="table-wrap">
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
          {alarms.map((a) => (
            <tr
              key={a.id}
              id={a.tag ? `alarm-${a.tag}` : undefined}
              className={[a.acked_at ? "" : `unack${a.severity === "critical" ? "" : " w"}`, focusTag && a.tag === focusTag ? "hit" : ""].filter(Boolean).join(" ") || undefined}
            >
              <td>{sevChip(a)}</td>
              <td>
                <div style={{ fontWeight: 600 }}>{a.message || a.rule_name}</div>
                {a.context && <div className="ctx">{a.context}</div>}
              </td>
              {showDevice && <td className="mono small">{a.device_id}</td>}
              {!compact && <td className="num">{a.trigger_value != null ? fmtNum(a.trigger_value, 2) : "—"}</td>}
              <td className="small nowrap">
                {fmtDT(a.raised_at)}
                <div className="muted">for {fmtDur((now - new Date(a.raised_at).getTime()) / 1000)}</div>
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
  const detail = useDevice(asset?.device_id);
  const live = useLiveDevice(asset?.device_id);
  const deviceId = scope === "asset" ? asset?.device_id : undefined;
  const summary = useQuery({
    queryKey: ["alarms", "summary", deviceId],
    queryFn: () => api<AlarmSummary>(`/alarms/summary${qs({ device_id: deviceId, hours: 24 })}`),
    refetchInterval: 15_000,
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
  const unackedActive = (active.data ?? []).filter((a) => !a.acked_at).length;
  const device = detail.data;
  const merged = device ? mergeLive(device, live) : null;
  const comms = merged ? !merged.online : false;
  const faults = (merged && device
    ? orderTags((device.tags ?? []).filter((t) => t.data_type === "number" && t.role !== "machine_status")).flatMap((t) => {
        const fault = tagFault(t, merged.value(t), { comms, bad: merged.quality(t) === 2 });
        if (!fault) return [];
        const v = merged.value(t);
        return [{ tag: t.tag, ...fault, value: typeof v === "number" ? `${formatValue(v, t)}${t.unit ? ` ${t.unit}` : ""}` : "—", seen: merged.ts(t) ?? merged.lastSeen }];
      })
    : []);
  const faultKey = faults.map((f) => f.tag).join(",");

  useEffect(() => {
    if (!focusTag) return;
    document.getElementById(`fault-${focusTag}`)?.scrollIntoView({ block: "center" });
  }, [focusTag, faultKey]);
  const hours = (() => {
    const out = new Map<number, number>();
    const h0 = Math.floor(Date.now() / 3600_000) * 3600_000;
    for (let i = 23; i >= 0; i--) out.set(h0 - i * 3600_000, 0);
    for (const a of s?.history ?? []) {
      const h = Math.floor(new Date(a.raised_at).getTime() / 3600_000) * 3600_000;
      if (out.has(h)) out.set(h, (out.get(h) ?? 0) + 1);
    }
    return [...out].map(([h, count]) => ({ h, count }));
  })();
  const maxSrc = Math.max(1, ...(s?.top_sources ?? []).map((x) => x.count));

  return (
    <div className="screen">
      <ScreenHead
        eyebrow="Operations"
        title="Alarms & events"
        desc="Limit alarms wait 3 s before raising and clearing, and most are held off while the machine is stopped or within 90 s of a restart, so a stop does not flood the list."
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
      <ErrorText error={ackAll.error || summary.error} />
      {summary.isLoading ? (
        <Loading />
      ) : (
        <div className="tiles">
          <Tile k="Active critical" v={s?.active_critical ?? 0} cls={s?.active_critical ? "crit" : undefined} />
          <Tile k="Active warning" v={s?.active_warning ?? 0} cls={s?.active_warning ? "warn" : undefined} />
          <Tile k="Unacknowledged" v={unackedActive} s={`${s?.unacked ?? 0} incl. cleared`} cls={unackedActive ? "warn" : undefined} />
          <Tile k="Raised in 24 h" v={s?.count ?? 0} s={`avg ${fmtNum(s?.per_hour_avg ?? 0, 1)}/h · peak ${s?.peak_hour ?? 0}/h`} />
        </div>
      )}
      <Panel title="Active alarms" sub={`${active.data?.length ?? 0} active · newest first`}>
        {faults.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Severity</th>
                  <th>Error on plant overview</th>
                  <th className="num">Value</th>
                  <th>Seen</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {faults.map((f) => (
                  <tr key={f.tag} id={`fault-${f.tag}`} className={f.tag === focusTag ? "hit" : undefined}>
                    <td><Chip cls={f.cls}>{f.label}</Chip></td>
                    <td style={{ fontWeight: 600 }}>{f.text}</td>
                    <td className="num">{f.value}</td>
                    <td className="small nowrap">{fmtDT(f.seen)}</td>
                    <td className="small muted">Live condition</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {active.data && active.data.length > 0 ? (
          <AlarmTable alarms={active.data} showDevice={scope === "all"} focusTag={focusTag} />
        ) : faults.length === 0 ? (
          <div className="empty">No active alarms.</div>
        ) : null}
      </Panel>
      <div className="grid g-7-5">
        <Panel title="Alarms raised per hour" sub="last 24 h">
          <HourBars hours={hours} />
        </Panel>
        <Panel title="Top alarm sources" sub="last 24 h">
          {s?.top_sources.length ? (
            <div className="bars">
              {s.top_sources.map((x) => (
                <div className="bar" key={x.source}>
                  <span title={x.source}>{x.source}</span>
                  <div className="tr">
                    <i style={{ width: `${(x.count / maxSrc) * 100}%` }} />
                  </div>
                  <span className="n">{x.count}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="empty">No alarms in the last 24 hours.</div>
          )}
        </Panel>
      </div>
      <Panel title="Event history" sub="raised in the last 24 h">
        {s?.history.length ? (
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
                {s.history.map((a) => (
                  <tr key={a.id} className={focusTag && a.tag === focusTag ? "hit" : undefined}>
                    <td className="small nowrap">{fmtDT(a.raised_at)}</td>
                    <td>{sevChip(a)}</td>
                    <td>{a.message || a.rule_name}</td>
                    {scope === "all" && <td className="mono small">{a.device_id}</td>}
                    <td className="num">{a.trigger_value != null ? fmtNum(a.trigger_value, 2) : "—"}</td>
                    <td className="small nowrap">{a.cleared_at ? fmtDT(a.cleared_at) : <Chip cls="crit">Active</Chip>}</td>
                    <td className="small nowrap">{fmtDur(((a.cleared_at ? new Date(a.cleared_at).getTime() : Date.now()) - new Date(a.raised_at).getTime()) / 1000)}</td>
                    <td className="small">{a.acked_by ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty">No events in the last 24 hours.</div>
        )}
      </Panel>
    </div>
  );
}
