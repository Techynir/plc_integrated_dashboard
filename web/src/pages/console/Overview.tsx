import { ReactNode, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useQueries, useQuery } from "@tanstack/react-query";
import { api, Device, SystemStats } from "../../api";
import { Summary, useHistory, windowEnd } from "../../assetApi";
import { useAuth } from "../../auth";
import { mergeLive, roleTag, runningNow, useAsset } from "../../hooks";
import { useIngestorStats, useLiveDevice } from "../../live";
import { formatAge, formatValue, secondsSince } from "../../format";
import { Chip, fmtDur, fmtNum, fmtPct, ScreenHead, Tile } from "../../components/console";
import { Sparkline } from "../../components/charts";
import { Loading, useNow } from "../../components/ui";

const HEALTH = { good: ["ok", "Good"], watch: ["warn", "Watch"], act: ["crit", "Act"], nodata: ["bad", "No data"] } as const;

function AssetCard({ d, s }: { d: Device; s: Summary | undefined }) {
  const isAdmin = useAuth().can("admin");
  const l = useLiveDevice(d.device_id);
  const m = mergeLive(d, l);
  const now = useNow(10_000);
  const tags = d.preview_tags ?? [];
  const speed = roleTag(tags, "speed");
  const moisture = roleTag(tags, "moisture");
  const conflict = !!s?.state_source.startsWith("CONFLICT");
  const running = runningNow(d, tags, m.value, m.status, conflict);
  const span = 3600_000;
  const { end } = windowEnd(d.last_seen, span, Math.floor(now / 60_000) * 60_000);
  const hist = useHistory(d.last_seen ? d.device_id : undefined, speed ? [speed.tag] : [], end - span, end, 60_000);

  if (!d.last_seen) {
    const Card = ({ children }: { children: ReactNode }) =>
      isAdmin ? (
        <Link className="asset template" to={`/config?asset=${encodeURIComponent(d.device_id)}`}>
          {children}
        </Link>
      ) : (
        <div className="asset template">{children}</div>
      );
    return (
      <Card>
        <div className="ah">
          <div>
            <h3>
              {d.name || d.device_id}
              {d.asset_type ? ` · ${d.asset_type}` : ""}
            </h3>
            <div className="meta">Configured · waiting for its first data</div>
          </div>
          <Chip cls="dev">Awaiting PLC link</Chip>
        </div>
        {tags.length > 0 && (
          <div className="taglist">
            {tags.map((t) => (
              <span key={t.tag}>{t.tag}</span>
            ))}
          </div>
        )}
        <div className="meta">Same gateway, historian, alarm engine and dashboards. Goes live when the PLC IP and register map are confirmed.</div>
      </Card>
    );
  }

  const comms = !m.online;
  const stateChip = comms ? <Chip cls="comms">Comms lost</Chip> : running === false ? <Chip cls="warn">Stopped</Chip> : <Chip cls="run">{running ? "Running" : "Online"}</Chip>;
  const [hcls, hlabel] = HEALTH[s?.health.state ?? "nodata"];
  const nProcess = (d.tag_count ?? tags.length) - (roleTag(tags, "machine_status") ? 1 : 0);
  const cfg = d.asset_config ?? {};
  const interval = cfg.poll_interval_ms ? `${cfg.poll_interval_ms / 1000} s` : `${d.expected_interval_s} s`;
  const v = (t: typeof speed, dec: number) => {
    const x = t ? m.value(t) : null;
    return typeof x === "number" ? fmtNum(x, dec) : t ? formatValue(x, t) : "—";
  };

  return (
    <Link className="asset" to={`/live?asset=${encodeURIComponent(d.device_id)}`}>
      <div className="ah">
        <div>
          <h3>
            {d.name || d.device_id}
            {d.asset_type ? ` · ${d.asset_type}` : ""}
          </h3>
          <div className="meta">
            {cfg.protocol || "MQTT"} · {nProcess} process tags{roleTag(tags, "machine_status") ? " + status" : ""} · {interval}
          </div>
        </div>
        <div className="row" style={{ justifyContent: "flex-end" }}>
          {stateChip}
          {d.simulated && <Chip cls="bad">Simulated</Chip>}
        </div>
      </div>
      <div className="kv">
        <div>
          <span>Speed</span>
          <b>{v(speed, 1)}</b>
        </div>
        <div>
          <span>Moisture</span>
          <b>{v(moisture, 2)}</b>
        </div>
        <div>
          <span>Health</span>
          <b>{s?.health.score != null ? Math.round(s.health.score) : "—"}</b>
        </div>
        <div>
          <span>Alarms</span>
          <b>{d.active_alarms}</b>
        </div>
      </div>
      <div className="chips">
        <Chip cls={hcls}>Health: {hlabel}</Chip>
        <Chip cls={comms ? "comms" : "ok"}>Data: {comms ? "Communication Lost" : "Good"}</Chip>
        {conflict && <Chip cls="warn">Check status register</Chip>}
      </div>
      {speed && (
        <>
          <Sparkline
            points={(hist.data?.series[speed.tag] ?? []).map((p) => [p[0], p[1]])}
            range={speed.min_value != null && speed.max_value != null ? [speed.min_value, speed.max_value] : null}
            ariaLabel="Machine speed, last 60 minutes" />
          <div className="meta">
            Machine speed · last 60 min{comms ? ` (to last data, ${formatAge(secondsSince(m.lastSeen, now))})` : ""}
          </div>
        </>
      )}
    </Link>
  );
}

const PIPE_KEY = "plc-pipeline-open";

function Pipeline({ asset }: { asset: Device | undefined }) {
  const [open, setOpen] = useState(() => {
    try {
      return localStorage.getItem(PIPE_KEY) !== "0";
    } catch {
      return true;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(PIPE_KEY, open ? "1" : "0");
    } catch {
      /* storage unavailable */
    }
  }, [open]);
  const stats = useQuery({ queryKey: ["system"], queryFn: () => api<SystemStats>("/system/stats"), refetchInterval: 10_000 });
  const ing = useIngestorStats() ?? stats.data?.ingestor ?? null;
  const l = useLiveDevice(asset?.device_id);
  const now = useNow(2000);
  const s = stats.data;
  const m = asset ? mergeLive(asset, l) : null;
  const down = !m?.online;
  const cfg = asset?.asset_config ?? {};
  const ingFresh = ing ? now - new Date(ing.ts).getTime() < 30_000 : false;
  const node = (title: string, l1: string, l2: string, bad: boolean) => (
    <div className={`pnode${bad ? " down" : ""}`}>
      <span className="pt">
        <i />
        {title}
      </span>
      <span className="pl">{l1}</span>
      <span className="pl">{l2}</span>
    </div>
  );
  const link = (label: string, value: string, bad: boolean) => (
    <div className={`plink${bad ? " down" : ""}`}>
      <span>{label}</span>
      <div className="ln" />
      <span>{bad ? "no data" : value}</span>
    </div>
  );
  return (
    <details className="panel" open={open} onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary>
        <div className="phead">
          <h2>Data pipeline{asset ? ` · ${asset.name || asset.device_id}` : ""}</h2>
          <span className="sub">Each hop reports its own health. A break shows as a break, never as a zero value.</span>
        </div>
      </summary>
      <div className="pipe">
        {node("PLC", [cfg.plc_ip, cfg.unit_id != null ? `Unit ${cfg.unit_id}` : null].filter(Boolean).join(" · ") || "IP not recorded", cfg.block_read || "register map", down)}
        {link(cfg.protocol || "Modbus", m?.lastSeen ? formatAge(secondsSince(m.lastSeen, now)) : "—", down)}
        {node("Gateway", cfg.gateway || "PLC gateway", m?.lastSeen ? `last ${formatAge(secondsSince(m.lastSeen, now))}` : "no data yet", down)}
        {link("MQTT/TLS", "connected", down)}
        {node("MQTT broker", s ? (s.broker.connected ? `${s.broker.clients_connected ?? "?"} clients` : "disconnected") : "…", "Mosquitto", !!s && !s.broker.connected)}
        {link("subscribe", ing ? `${fmtNum(ing.msg_rate, 1)} msg/s` : "—", !ingFresh)}
        {node("Ingestor + historian", s ? `${Intl.NumberFormat(undefined, { notation: "compact" }).format(s.telemetry_rows)} rows` : "…", ing ? `flush ${Math.round(ing.last_flush_ms)} ms` : "no heartbeat", !ingFresh)}
        {link("WebSocket", "live", false)}
        {node("Web dashboard", `${s?.websocket_clients ?? "?"} viewers`, "This screen", false)}
      </div>
      {down && asset?.last_seen && (
        <p className="note" style={{ marginTop: 10 }}>
          The server side is healthy; this PLC has not sent data for {formatAge(secondsSince(asset.last_seen, now)).replace(" ago", "")}. Check the gateway power/network or its MQTT
          settings.
        </p>
      )}
    </details>
  );
}

export function Overview() {
  const { asset, assets, loading } = useAsset();
  const { can } = useAuth();
  const summaries = useQueries({
    queries: assets.map((d) => ({
      queryKey: ["asset", d.device_id, "summary", 24],
      queryFn: () => api<Summary>(`/assets/${encodeURIComponent(d.device_id)}/summary?hours=24`),
      enabled: !!d.last_seen,
      refetchInterval: 60_000,
    })),
  });
  if (loading) return <Loading />;

  const connected = assets.filter((d) => d.online).length;
  const awaiting = assets.filter((d) => !d.last_seen).length;
  const bySum = new Map(assets.map((d, i) => [d.device_id, summaries[i]?.data]));
  const sums = [...bySum.values()].filter((s): s is Summary => !!s);
  const active = assets.reduce((n, d) => n + d.active_alarms, 0);
  const crit = assets.some((d) => d.top_severity === "critical" && d.active_alarms > 0);
  const unacked = sums.reduce((n, s) => n + s.alarms.unacked, 0);
  const run = sums.reduce((n, s) => n + s.totals.run, 0);
  const stop = sums.reduce((n, s) => n + s.totals.stop, 0);
  const comms = sums.reduce((n, s) => n + s.totals.comms, 0);
  const records = sums.reduce((n, s) => n + s.records, 0);
  const completeness = sums.length ? sums.reduce((n, s) => n + (s.completeness ?? 0), 0) / sums.length : null;
  const interval = asset?.asset_config?.poll_interval_ms ? asset.asset_config.poll_interval_ms / 1000 : asset?.expected_interval_s ?? 1;

  return (
    <div className="screen">
      <ScreenHead
        eyebrow="Site · Plant"
        title="Plant overview"
        desc="Every asset on one page: live state, health, alarms and whether its data can be trusted."
      />
      <div className="tiles">
        <Tile k="Assets connected" v={<>{connected}<small> / {assets.length}</small></>} s={`${connected} live · ${assets.length - connected - awaiting} no data · ${awaiting} awaiting PLC`} />
        <Tile k="Active alarms" v={active} s={`${unacked} unacknowledged`} cls={crit ? "crit" : active ? "warn" : undefined} />
        <Tile k="Availability · 24 h" v={run + stop > 0 ? fmtPct(run / (run + stop)) : "—"} unit="%" s={`${fmtDur(stop)} stopped`} />
        <Tile k="Data completeness · 24 h" v={fmtPct(completeness, 2)} unit="%" s={`${fmtDur(comms)} with no PLC link`} />
        <Tile k="Records stored · 24 h" v={records.toLocaleString("en-IN")} s={`1 record every ${interval} s`} />
      </div>
      <div className="assets">
        {assets.map((d) => (
          <AssetCard key={d.device_id} d={d} s={bySum.get(d.device_id)} />
        ))}
        {can("admin") && (
        <Link className="asset template" to="/admin/devices">
          <h3 style={{ color: "var(--ink-2)" }}>Add an asset</h3>
          <div className="meta">Register the PLC, give the programmer its MQTT details, then map registers, set limits and go live in Asset configuration.</div>
        </Link>
        )}
      </div>
      <Pipeline asset={asset} />
    </div>
  );
}
