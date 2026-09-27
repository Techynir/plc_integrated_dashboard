import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, Device, Tag } from "../api";
import { formatAge, formatValue, secondsSince } from "../format";
import { LiveDevice, live, useIngestorStats, useLiveVersion } from "../live";
import { useActiveAlarms } from "../components/Layout";
import { Loading, SeverityBadge, SimulatedBadge, StatTile, StatusBadge, deviceStatus, useNow } from "../components/ui";

export function useDevices() {
  return useQuery({ queryKey: ["devices"], queryFn: () => api<Device[]>("/devices"), refetchInterval: 15_000 });
}

/** Merge the REST snapshot with newer values from the live stream. */
export function mergeLive(d: Device, l: LiveDevice | undefined) {
  return {
    online: l?.online ?? d.online,
    status: l?.status ?? d.status,
    lastSeen: l?.lastSeen ?? d.last_seen,
    value: (t: Tag) => (l?.values[t.tag] ? l.values[t.tag].v : t.value),
    ts: (t: Tag) => l?.values[t.tag]?.ts ?? t.ts,
  };
}

function DeviceCard({ d, now }: { d: Device; now: number }) {
  const m = mergeLive(d, live.devices.get(d.device_id));
  const staleAfter = Math.max(15, 3 * d.expected_interval_s);
  const alarmClass = d.active_alarms ? ` alarm-${d.top_severity === "critical" ? "critical" : "warning"}` : "";
  return (
    <Link to={`/devices/${encodeURIComponent(d.device_id)}`} className={`card device-card${alarmClass}`}>
      <div className="row">
        <div style={{ minWidth: 0 }}>
          <div className="title">{d.name || d.device_id}</div>
          <div className="small muted mono">{d.device_id}</div>
        </div>
        <span className="spacer" />
        <div className="row" style={{ gap: 4, justifyContent: "flex-end" }}>
          {d.simulated && <SimulatedBadge />}
          <StatusBadge online={m.online} status={m.status} enabled={d.enabled} />
        </div>
      </div>
      <div className="kv">
        {(d.preview_tags ?? []).map((t) => {
          const age = secondsSince(m.ts(t), now);
          const stale = age === null || age > staleAfter;
          return (
            <div key={t.tag} className={stale ? "stale" : undefined} title={stale ? "No recent value" : undefined}>
              <div className="kv-label">{t.display_name}</div>
              <div className="kv-value">
                {formatValue(m.value(t), t)}
                {t.unit && t.data_type === "number" ? <span className="unit">{t.unit}</span> : null}
              </div>
            </div>
          );
        })}
        {!d.preview_tags?.length && <div className="small muted">No data received yet</div>}
      </div>
      <div className="row small muted" style={{ marginTop: 12 }}>
        <span>Updated {formatAge(secondsSince(m.lastSeen, now))}</span>
        <span className="spacer" />
        {d.active_alarms > 0 && d.top_severity && (
          <span className="row" style={{ gap: 4 }}>
            <SeverityBadge severity={d.top_severity} />
            {d.active_alarms > 1 && <span>×{d.active_alarms}</span>}
          </span>
        )}
      </div>
    </Link>
  );
}

export function Overview() {
  const devices = useDevices();
  const alarms = useActiveAlarms();
  const stats = useIngestorStats();
  const now = useNow(1000);
  useLiveVersion();
  const [params, setParams] = useSearchParams();
  const kiosk = params.get("kiosk") === "1";
  const [site, setSite] = useState<string>("");
  const [line, setLine] = useState<string>("");

  const list = devices.data ?? [];
  const sites = useMemo(() => [...new Set(list.map((d) => d.site).filter(Boolean))].sort(), [list]);
  const lines = useMemo(
    () => [...new Set(list.filter((d) => !site || d.site === site).map((d) => d.line).filter(Boolean))].sort(),
    [list, site],
  );
  const visible = list.filter((d) => (!site || d.site === site) && (!line || d.line === line));

  const states = list.map((d) => {
    const m = mergeLive(d, live.devices.get(d.device_id));
    return deviceStatus(m.online, m.status, d.enabled).label;
  });
  const online = states.filter((s) => s !== "Offline" && s !== "Disabled").length;
  const faults = states.filter((s) => s === "Fault").length;
  const critical = (alarms.data ?? []).filter((a) => a.severity === "critical");

  const groups = new Map<string, Device[]>();
  for (const d of visible) {
    const key = [d.site, d.line].filter(Boolean).join(" · ") || "Unassigned";
    groups.set(key, [...(groups.get(key) ?? []), d]);
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Plant overview</h1>
          <div className="sub">
            {online} of {list.length} devices online · live
          </div>
        </div>
        {kiosk && (
          <button
            className="small ghost"
            onClick={() => {
              params.delete("kiosk");
              setParams(params);
            }}
          >
            Exit kiosk
          </button>
        )}
      </div>

      {critical.length > 0 && (
        <Link to="/alarms" className="alert critical" style={{ color: "inherit", textDecoration: "none" }}>
          <SeverityBadge severity="critical" />
          <strong>{critical.length} critical alarm{critical.length > 1 ? "s" : ""}</strong>
          <span className="secondary">{critical[0].message}{critical.length > 1 ? ` and ${critical.length - 1} more` : ""}</span>
        </Link>
      )}

      <div className="grid grid-tiles">
        <StatTile label="Devices online" value={`${online} / ${list.length}`} />
        <StatTile label="Devices in fault" value={faults} />
        <StatTile label="Active alarms" value={alarms.data?.length ?? "—"} foot={`${critical.length} critical`} />
        <StatTile
          label="Ingest rate"
          value={stats ? stats.msg_rate.toFixed(1) : "—"}
          unit="msg/s"
          foot={stats ? `${stats.row_rate.toFixed(0)} values/s stored` : "waiting for ingestor"}
        />
      </div>

      {(sites.length > 1 || lines.length > 1) && (
        <div className="row section">
          <span className="small secondary">Filter</span>
          {sites.length > 1 && (
            <>
              <button className={`chip${!site ? " on" : ""}`} onClick={() => { setSite(""); setLine(""); }}>
                All sites
              </button>
              {sites.map((s) => (
                <button key={s} className={`chip${site === s ? " on" : ""}`} onClick={() => { setSite(s); setLine(""); }}>
                  {s}
                </button>
              ))}
            </>
          )}
          {lines.length > 1 && (
            <>
              <span className="muted">|</span>
              <button className={`chip${!line ? " on" : ""}`} onClick={() => setLine("")}>
                All lines
              </button>
              {lines.map((l) => (
                <button key={l} className={`chip${line === l ? " on" : ""}`} onClick={() => setLine(l)}>
                  {l}
                </button>
              ))}
            </>
          )}
        </div>
      )}

      {devices.isLoading && <Loading />}
      {devices.data && list.length === 0 && (
        <div className="card empty section">
          No devices yet. An admin can add one under <Link to="/admin/devices">Admin → Devices</Link>, or a PLC that publishes
          with valid credentials is registered automatically.
        </div>
      )}
      {[...groups.entries()].map(([group, items]) => (
        <section key={group} className="section">
          <h2 style={{ marginBottom: 10 }}>{group}</h2>
          <div className="grid grid-cards">
            {items.map((d) => (
              <DeviceCard key={d.device_id} d={d} now={now} />
            ))}
          </div>
        </section>
      ))}
    </>
  );
}
