import { useQueries } from "@tanstack/react-query";
import { api } from "../../api";
import { Performance, Summary, useAnalytics } from "../../assetApi";
import { useAsset } from "../../hooks";
import { fmtDur, fmtPct, ScreenHead, Tile } from "../../components/console";
import { Loading } from "../../components/ui";
import { Live } from "./Live";

export function Overview() {
  const { asset, assets, loading } = useAsset();
  const perf = useAnalytics<Performance>(asset?.device_id, "performance", 24);
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
  const p = perf.data;
  const rated = p?.rated_actual ?? [];
  const who = assets.length > 1 ? asset?.name || asset?.device_id : null;

  return (
    <div className="screen">
      <ScreenHead
        eyebrow="Site · Plant"
        title="Plant overview"
      />
      <div className="tiles">
        <Tile k="Assets connected" v={<>{connected}<small> / {assets.length}</small></>} s={`${connected} live · ${assets.length - connected - awaiting} no data · ${awaiting} awaiting PLC`} />
        <Tile k="Active alarms" v={active} s={`${unacked} unacknowledged`} cls={crit ? "crit" : active ? "warn" : undefined} />
        <Tile k="Availability · 24 h" v={run + stop > 0 ? fmtPct(run / (run + stop)) : "—"} unit="%" s={`${fmtDur(stop)} stopped`} />
        <Tile
          k="Downtime · 24 h"
          v={p ? (p.totals.stop / 3600).toFixed(1) : "—"}
          unit="h"
          s={p ? `${p.stops.length} stops${who ? ` · ${who}` : ""}` : ""}
        />
        <Tile
          k="Performance · 24 h"
          v={p?.performance != null ? (p.performance * 100).toFixed(1) : "—"}
          unit="%"
          s={
            rated.length
              ? `actual vs rated · ${rated.length} parameter${rated.length === 1 ? "" : "s"}${who ? ` · ${who}` : ""}`
              : "no rated values set"
          }
        />
      </div>
      {asset && <Live embedded />}
    </div>
  );
}
