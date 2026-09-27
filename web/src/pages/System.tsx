import { useQuery } from "@tanstack/react-query";
import { api, SystemStats } from "../api";
import { useAuth } from "../auth";
import { compactNumber, formatBytes, formatDateTime, formatDuration } from "../format";
import { useIngestorStats } from "../live";
import { ErrorText, Loading, StatTile } from "../components/ui";

interface IngestError {
  ts: string;
  topic: string;
  device_id: string | null;
  reason: string;
  payload: string;
}

export function System() {
  const { can } = useAuth();
  const stats = useQuery({ queryKey: ["system"], queryFn: () => api<SystemStats>("/system/stats"), refetchInterval: 10_000 });
  const liveStats = useIngestorStats();
  const errors = useQuery({
    queryKey: ["ingest-errors"],
    queryFn: () => api<IngestError[]>("/system/ingest-errors?limit=50"),
    enabled: can("admin"),
    refetchInterval: 30_000,
  });

  if (stats.isLoading) return <Loading />;
  const s = stats.data;
  const ing = liveStats ?? s?.ingestor ?? null;
  const ingestorFresh = ing ? Date.now() - new Date(ing.ts).getTime() < 30_000 : false;

  return (
    <>
      <div className="page-header">
        <div>
          <h1>System health</h1>
          <div className="sub">Broker, ingestion pipeline and storage</div>
        </div>
      </div>
      <ErrorText error={stats.error} />
      {s && (
        <>
          <h2 style={{ marginBottom: 10 }}>Pipeline</h2>
          <div className="grid grid-tiles">
            <StatTile
              label="MQTT broker"
              value={s.broker.connected ? "Connected" : "Disconnected"}
              foot={`${s.broker.clients_connected ?? "?"} clients connected`}
            />
            <StatTile
              label="Ingestor"
              value={ingestorFresh ? "Running" : "No heartbeat"}
              foot={ing ? `up ${formatDuration(ing.uptime_s)}` : "no stats yet"}
            />
            <StatTile label="Messages" value={ing ? ing.msg_rate.toFixed(1) : "—"} unit="msg/s" foot={ing ? `${compactNumber(ing.received_total)} since start` : ""} />
            <StatTile label="Values stored" value={ing ? ing.row_rate.toFixed(0) : "—"} unit="/s" foot={ing ? `last batch ${ing.last_flush_ms} ms` : ""} />
            <StatTile
              label="Rejected messages"
              value={ing ? compactNumber(ing.invalid_total) : "—"}
              foot={`${s.ingest_errors_1h} in the last hour`}
            />
            <StatTile
              label="Duplicates dropped"
              value={ing ? compactNumber(ing.duplicates_total) : "—"}
              foot={ing && ing.write_errors_total ? `${ing.write_errors_total} write errors` : "no write errors"}
            />
          </div>

          <h2 style={{ margin: "22px 0 10px" }}>Fleet and storage</h2>
          <div className="grid grid-tiles">
            <StatTile label="Devices online" value={`${s.devices_online} / ${s.devices_total}`} />
            <StatTile label="Active alarms" value={s.alarms_active} />
            <StatTile label="Database size" value={formatBytes(s.db_size_bytes)} foot="raw data kept 30 days, compressed after 1 day" />
            <StatTile label="Stored values" value={compactNumber(s.telemetry_rows)} foot="approximate" />
            <StatTile label="Dashboard sessions" value={s.websocket_clients} foot="live WebSocket clients" />
          </div>
        </>
      )}

      {can("admin") && (
        <div className="card section">
          <div className="card-header">
            <h2>Rejected messages</h2>
            <span className="small muted">last 50 · kept 7 days</span>
          </div>
          {errors.data && errors.data.length === 0 && <div className="empty">No rejected messages.</div>}
          {errors.data && errors.data.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Topic</th>
                    <th>Reason</th>
                    <th>Payload</th>
                  </tr>
                </thead>
                <tbody>
                  {errors.data.map((e, i) => (
                    <tr key={i}>
                      <td className="small nowrap">{formatDateTime(e.ts)}</td>
                      <td className="small mono">{e.topic}</td>
                      <td className="small">{e.reason}</td>
                      <td className="small mono" style={{ maxWidth: 360, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={e.payload}>
                        {e.payload}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </>
  );
}
