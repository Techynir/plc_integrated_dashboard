import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, qs } from "../api";
import { useDevices } from "../hooks";
import { ErrorText, Loading } from "../components/ui";

type RawStatus = "ok" | "rejected" | "duplicate" | "ignored";

interface RawMessage {
  ts: string;
  device_id: string;
  topic: string;
  payload: string;
  status: RawStatus;
  detail: string;
}

const STATUS: Record<RawStatus, { label: string; color: string }> = {
  ok: { label: "Parsed", color: "var(--good)" },
  rejected: { label: "Rejected", color: "var(--critical)" },
  duplicate: { label: "Duplicate", color: "var(--offline)" },
  ignored: { label: "Ignored", color: "var(--offline)" },
};

function time(iso: string): string {
  const d = new Date(iso);
  return (
    d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }) +
    "." +
    String(d.getMilliseconds()).padStart(3, "0")
  );
}

/** Telemetry exactly as the PLCs sent it, next to what the ingestor made of it. */
export function RawData() {
  const [params, setParams] = useSearchParams();
  const device = params.get("device") ?? "";
  const status = (params.get("status") ?? "") as RawStatus | "";
  const [limit, setLimit] = useState(50);
  const [live, setLive] = useState(true);
  const devices = useDevices();

  const setFilter = (key: string, value: string) => {
    if (value) params.set(key, value);
    else params.delete(key);
    setParams(params, { replace: true });
  };

  const raw = useQuery({
    queryKey: ["raw", device, status, limit],
    queryFn: () => api<RawMessage[]>(`/raw-messages${qs({ device_id: device, status, limit })}`),
    refetchInterval: live ? 2000 : false,
    placeholderData: (prev) => prev,
  });
  const rows = raw.data ?? [];

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Raw data</h1>
          <div className="sub">Messages exactly as received from the PLCs over MQTT, newest first · kept 7 days</div>
        </div>
        <button className={live ? "primary" : ""} onClick={() => setLive((l) => !l)}>
          {live ? "Live ● pause" : "Paused ▶ resume"}
        </button>
      </div>

      <div className="row" style={{ marginBottom: 14 }}>
        <label className="field">
          Device
          <select value={device} onChange={(e) => setFilter("device", e.target.value)}>
            <option value="">All devices</option>
            {(devices.data ?? []).map((d) => (
              <option key={d.device_id} value={d.device_id}>
                {d.name && d.name !== d.device_id ? `${d.name} (${d.device_id})` : d.device_id}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          Result
          <select value={status} onChange={(e) => setFilter("status", e.target.value)}>
            <option value="">All</option>
            <option value="ok">Parsed</option>
            <option value="rejected">Rejected</option>
            <option value="duplicate">Duplicate</option>
            <option value="ignored">Ignored</option>
          </select>
        </label>
        <label className="field">
          Show
          <select value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
            <option value={50}>Last 50</option>
            <option value={200}>Last 200</option>
            <option value={1000}>Last 1000</option>
          </select>
        </label>
      </div>

      <div className="card">
        <ErrorText error={raw.error} />
        {raw.isLoading && <Loading />}
        {raw.data && rows.length === 0 && <div className="empty">No messages match these filters in the last 7 days.</div>}
        {rows.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Received</th>
                  {!device && <th>Device</th>}
                  <th>Raw payload</th>
                  <th>Result</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((m, i) => {
                  const s = STATUS[m.status];
                  return (
                    <tr key={`${m.ts}-${m.device_id}-${i}`}>
                      <td className="small nowrap" style={{ verticalAlign: "top" }} title={m.topic}>
                        {time(m.ts)}
                      </td>
                      {!device && (
                        <td className="small nowrap" style={{ verticalAlign: "top" }}>
                          <Link to={`/live?asset=${encodeURIComponent(m.device_id)}`}>{m.device_id}</Link>
                        </td>
                      )}
                      <td style={{ verticalAlign: "top", maxWidth: 560 }}>
                        <code style={{ whiteSpace: "pre-wrap", wordBreak: "break-all" }}>{m.payload}</code>
                        <div className="small muted mono">{m.topic}</div>
                      </td>
                      <td className="small" style={{ verticalAlign: "top", maxWidth: 420 }}>
                        <span className="badge" style={{ marginBottom: 4 }}>
                          <span className="dot" style={{ background: s.color }} />
                          {s.label}
                        </span>
                        <div className={m.status === "rejected" ? "error-text" : "secondary"} style={{ wordBreak: "break-word" }}>
                          {m.detail}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
