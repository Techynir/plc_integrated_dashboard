import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alarm, api, qs } from "../api";
import { useAuth } from "../auth";
import { formatDateTime, formatDuration } from "../format";
import { ErrorText, Loading, Modal, SeverityBadge, useNow } from "../components/ui";

function AckModal({ alarm, onClose }: { alarm: Alarm; onClose: () => void }) {
  const qc = useQueryClient();
  const [comment, setComment] = useState("");
  const ack = useMutation({
    mutationFn: () => api(`/alarms/${alarm.id}/ack`, { method: "POST", body: { comment } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["alarms"] });
      onClose();
    },
  });
  return (
    <Modal
      title="Acknowledge alarm"
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancel</button>
          <button className="primary" onClick={() => ack.mutate()} disabled={ack.isPending}>
            Acknowledge
          </button>
        </>
      }
    >
      <p style={{ marginTop: 0 }}>
        <SeverityBadge severity={alarm.severity} /> {alarm.message}
      </p>
      <label className="field">
        Comment (optional)
        <textarea rows={3} value={comment} onChange={(e) => setComment(e.target.value)} maxLength={500} autoFocus />
      </label>
      <ErrorText error={ack.error} />
    </Modal>
  );
}

export function AlarmTable({ alarms, showDevice = true }: { alarms: Alarm[]; showDevice?: boolean }) {
  const { can } = useAuth();
  const now = useNow(5000);
  const [acking, setAcking] = useState<Alarm | null>(null);
  if (alarms.length === 0) return <div className="empty">No alarms.</div>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Severity</th>
            {showDevice && <th>Device</th>}
            <th>Message</th>
            <th>Raised</th>
            <th>Duration</th>
            <th>State</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {alarms.map((a) => {
            const end = a.cleared_at ? new Date(a.cleared_at).getTime() : now;
            return (
              <tr key={a.id}>
                <td>
                  <SeverityBadge severity={a.severity} />
                </td>
                {showDevice && (
                  <td className="nowrap">
                    <Link to={`/devices/${encodeURIComponent(a.device_id)}`}>{a.device_id}</Link>
                  </td>
                )}
                <td>
                  <div>{a.message}</div>
                  <div className="small muted">{a.rule_name}</div>
                </td>
                <td className="small nowrap">{formatDateTime(a.raised_at)}</td>
                <td className="small nowrap">{formatDuration((end - new Date(a.raised_at).getTime()) / 1000)}</td>
                <td className="small">
                  <div>{a.active ? <strong>Active</strong> : "Cleared"}</div>
                  {a.acked_at && (
                    <div className="muted" title={a.ack_comment ?? ""}>
                      Ack by {a.acked_by}
                      {a.ack_comment ? ` · “${a.ack_comment}”` : ""}
                    </div>
                  )}
                </td>
                <td className="num">
                  {!a.acked_at && can("operator") && (
                    <button className="small" onClick={() => setAcking(a)}>
                      Acknowledge
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {acking && <AckModal alarm={acking} onClose={() => setAcking(null)} />}
    </div>
  );
}

export function Alarms() {
  const [state, setState] = useState<"active" | "all">("active");
  const alarms = useQuery({
    queryKey: ["alarms", state === "active" ? "active" : "history"],
    queryFn: () => api<Alarm[]>(`/alarms${qs({ state, limit: 500 })}`),
    refetchInterval: 30_000,
  });
  const unacked = (alarms.data ?? []).filter((a) => !a.acked_at && a.active).length;
  return (
    <>
      <div className="page-header">
        <div>
          <h1>Alarms</h1>
          <div className="sub">{state === "active" ? `${alarms.data?.length ?? 0} active · ${unacked} unacknowledged` : "Last 500 alarms"}</div>
        </div>
        <div className="segmented">
          <button className={state === "active" ? "on" : ""} onClick={() => setState("active")}>
            Active
          </button>
          <button className={state === "all" ? "on" : ""} onClick={() => setState("all")}>
            History
          </button>
        </div>
      </div>
      <div className="card">
        {alarms.isLoading ? <Loading /> : <AlarmTable alarms={alarms.data ?? []} />}
        <ErrorText error={alarms.error} />
      </div>
    </>
  );
}
