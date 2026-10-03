import { FormEvent, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, Credentials, Device } from "../../api";
import { formatAge, secondsSince } from "../../format";
import { useDevices } from "../../hooks";
import { CopyButton, ErrorText, Loading, Modal, SimulatedBadge, StatusBadge, useNow } from "../../components/ui";

function samplePayload(deviceId: string): string {
  return JSON.stringify(
    {
      schema_version: 1,
      device_id: deviceId,
      ts: new Date().toISOString(),
      seq: 1,
      status: "RUN",
      tags: { temperature_c: 72.4, pressure_bar: 3.12, motor_running: true },
    },
    null,
    2,
  );
}

function CredentialsModal({ result, onClose }: { result: Credentials; onClose: () => void }) {
  const { credentials: cr, connection: c } = result;
  const pubCmd =
    `mosquitto_pub -h ${c.host} -p ${c.port} --cafile plc-dashboard-ca.crt \\\n` +
    `  -u ${cr.username} -P '${cr.password}' -i ${c.client_id} -q 1 \\\n` +
    `  -t ${c.telemetry_topic} \\\n` +
    `  -m '${JSON.stringify({ schema_version: 1, device_id: cr.username, tags: { temperature_c: 72.4 } })}'`;
  const handoff = [
    `Broker host: ${c.host}`,
    `Port: ${c.port} (MQTT over TLS 1.2+)`,
    `CA certificate: download from the dashboard (Admin → Devices)`,
    `Client ID: ${c.client_id}`,
    `Username: ${cr.username}`,
    `Password: ${cr.password}`,
    `Telemetry topic: ${c.telemetry_topic} (QoS 1, JSON)`,
    `Status topic: ${c.status_topic} — publish "online" (retained) after connecting`,
    `Last Will: topic ${c.last_will.topic}, payload "offline", QoS 1, retained`,
  ].join("\n");

  return (
    <Modal title={`Connection details · ${cr.username}`} onClose={onClose} footer={<button className="primary" onClick={onClose}>Done</button>}>
      <div className="alert warning" style={{ marginBottom: 14 }}>
        Copy the password now — it is shown only once. Rotate the credentials to issue a new one.
      </div>
      <dl className="details">
        <dt>Host</dt>
        <dd>{c.host}</dd>
        <dt>Port</dt>
        <dd>{c.port} (TLS)</dd>
        <dt>Client ID</dt>
        <dd>{c.client_id}</dd>
        <dt>Username</dt>
        <dd>{cr.username}</dd>
        <dt>Password</dt>
        <dd>
          <span className="secret">{cr.password}</span> <CopyButton text={cr.password} />
        </dd>
        <dt>Telemetry topic</dt>
        <dd>{c.telemetry_topic}</dd>
        <dt>Status topic</dt>
        <dd>{c.status_topic}</dd>
        <dt>Last Will</dt>
        <dd>
          "{c.last_will.payload}" → {c.last_will.topic} (QoS 1, retained)
        </dd>
        <dt>CA certificate</dt>
        <dd>
          <a href={c.ca_certificate_url} download>
            plc-dashboard-ca.crt
          </a>
        </dd>
      </dl>
      <div className="row" style={{ margin: "14px 0 6px" }}>
        <h3>Example payload</h3>
        <span className="spacer" />
        <CopyButton text={samplePayload(cr.username)} />
      </div>
      <div className="codeblock">{samplePayload(cr.username)}</div>
      <div className="row" style={{ margin: "14px 0 6px" }}>
        <h3>Test from a terminal</h3>
        <span className="spacer" />
        <CopyButton text={pubCmd} />
      </div>
      <div className="codeblock">{pubCmd}</div>
      <div className="row" style={{ marginTop: 14 }}>
        <CopyButton text={handoff} label="Copy all details for the PLC programmer" />
      </div>
    </Modal>
  );
}

interface DeviceForm {
  device_id: string;
  name: string;
  site: string;
  line: string;
  description: string;
  expected_interval_s: string;
}

function DeviceFormModal({ device, onClose, onCreated }: { device?: Device; onClose: () => void; onCreated: (c: Credentials) => void }) {
  const qc = useQueryClient();
  const [form, setForm] = useState<DeviceForm>({
    device_id: device?.device_id ?? "",
    name: device?.name ?? "",
    site: device?.site ?? "",
    line: device?.line ?? "",
    description: device?.description ?? "",
    expected_interval_s: String(device?.expected_interval_s ?? 1),
  });
  const save = useMutation({
    mutationFn: async () => {
      const body = { ...form, expected_interval_s: Number(form.expected_interval_s) };
      if (device) {
        const { device_id: _ignored, ...changes } = body;
        void _ignored;
        return api(`/devices/${encodeURIComponent(device.device_id)}`, { method: "PATCH", body: changes });
      }
      return api<Credentials>("/devices", { method: "POST", body });
    },
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: ["devices"] });
      qc.invalidateQueries({ queryKey: ["device"] });
      if (!device) onCreated(result as Credentials);
      onClose();
    },
  });
  const set = (k: keyof DeviceForm) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });
  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    save.mutate();
  };

  return (
    <Modal
      title={device ? `Edit ${device.device_id}` : "Add device"}
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancel</button>
          <button className="primary" onClick={() => submit()} disabled={save.isPending}>
            {device ? "Save" : "Create and generate credentials"}
          </button>
        </>
      }
    >
      <form className="form-grid" onSubmit={submit}>
        <label className="field full">
          Device ID
          <input value={form.device_id} onChange={set("device_id")} disabled={!!device} placeholder="plc-line1-01" pattern="[A-Za-z0-9_-]{1,64}" required />
          <span className="hint">Letters, digits, - and _. Also the MQTT username; cannot be changed later.</span>
        </label>
        <label className="field">
          Display name
          <input value={form.name} onChange={set("name")} placeholder="Filler 1" />
        </label>
        <label className="field">
          Publish interval (s)
          <input type="number" min={0.1} step="any" value={form.expected_interval_s} onChange={set("expected_interval_s")} />
          <span className="hint">Used for offline and stale detection</span>
        </label>
        <label className="field">
          Site
          <input value={form.site} onChange={set("site")} placeholder="pune" pattern="[A-Za-z0-9_-]*" />
        </label>
        <label className="field">
          Line
          <input value={form.line} onChange={set("line")} placeholder="line1" pattern="[A-Za-z0-9_-]*" />
        </label>
        <label className="field full">
          Description
          <input value={form.description} onChange={set("description")} />
        </label>
      </form>
      <ErrorText error={save.error} />
    </Modal>
  );
}

export function AdminDevices() {
  const qc = useQueryClient();
  const devices = useDevices();
  const now = useNow(5000);
  const [params] = useSearchParams();
  const focus = params.get("focus");
  const [editing, setEditing] = useState<Device | "new" | null>(null);
  const [creds, setCreds] = useState<Credentials | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<Device | null>(null);
  const [purge, setPurge] = useState(false);

  const action = useMutation({
    mutationFn: async ({ kind, d }: { kind: "rotate" | "toggle" | "delete"; d: Device }) => {
      const path = `/devices/${encodeURIComponent(d.device_id)}`;
      if (kind === "rotate") return api<Credentials>(`${path}/credentials`, { method: "POST" });
      if (kind === "toggle") return api(path, { method: "PATCH", body: { enabled: !d.enabled } });
      return api(`${path}${purge ? "?purge_data=true" : ""}`, { method: "DELETE" });
    },
    onSuccess: (result, { kind }) => {
      qc.invalidateQueries({ queryKey: ["devices"] });
      if (kind === "rotate") setCreds(result as Credentials);
      if (kind === "delete") setConfirmDelete(null);
    },
  });

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Devices</h1>
          <div className="sub">Register PLCs and issue their MQTT credentials</div>
        </div>
        <div className="row">
          <a className="btn" href="/api/v1/broker/ca.crt" download>
            Download CA certificate
          </a>
          <button className="primary" onClick={() => setEditing("new")}>
            Add device
          </button>
        </div>
      </div>
      <ErrorText error={action.error} />
      <div className="card">
        {devices.isLoading && <Loading />}
        {devices.data && devices.data.length === 0 && <div className="empty">No devices yet.</div>}
        {devices.data && devices.data.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Device</th>
                  <th>Site / line</th>
                  <th>Status</th>
                  <th>Last message</th>
                  <th className="num">Messages</th>
                  <th>Credentials</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {devices.data.map((d) => (
                  <tr key={d.device_id} style={focus === d.device_id ? { background: "var(--accent-soft)" } : undefined}>
                    <td>
                      <Link to={`/live?asset=${encodeURIComponent(d.device_id)}`} style={{ fontWeight: 500 }}>
                        {d.name || d.device_id}
                      </Link>
                      <div className="small muted mono">{d.device_id}</div>
                    </td>
                    <td className="small">{[d.site, d.line].filter(Boolean).join(" / ") || "—"}</td>
                    <td>
                      <div className="row" style={{ gap: 4 }}>
                        <StatusBadge online={d.online} status={d.status} enabled={d.enabled} />
                        {d.simulated && <SimulatedBadge />}
                      </div>
                    </td>
                    <td className="small nowrap">{formatAge(secondsSince(d.last_seen, now))}</td>
                    <td className="num small">{d.msg_count.toLocaleString()}</td>
                    <td className="small">{d.has_credentials ? "Issued" : <span className="muted">none</span>}</td>
                    <td className="num nowrap">
                      {d.simulated ? (
                        <span className="small muted">managed in the simulator</span>
                      ) : (
                      <>
                      <button className="small ghost" onClick={() => setEditing(d)}>
                        Edit
                      </button>
                      <button className="small ghost" onClick={() => action.mutate({ kind: "rotate", d })} title="Issue a new password; the old one stops working">
                        {d.has_credentials ? "Rotate password" : "Issue credentials"}
                      </button>
                      <button className="small ghost" onClick={() => action.mutate({ kind: "toggle", d })}>
                        {d.enabled ? "Disable" : "Enable"}
                      </button>
                      <button className="small ghost danger" onClick={() => { setPurge(false); setConfirmDelete(d); }}>
                        Delete
                      </button>
                      </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {editing && (
        <DeviceFormModal device={editing === "new" ? undefined : editing} onClose={() => setEditing(null)} onCreated={setCreds} />
      )}
      {creds && <CredentialsModal result={creds} onClose={() => setCreds(null)} />}
      {confirmDelete && (
        <Modal
          title={`Delete ${confirmDelete.device_id}?`}
          onClose={() => setConfirmDelete(null)}
          footer={
            <>
              <button onClick={() => setConfirmDelete(null)}>Cancel</button>
              <button className="primary" style={{ background: "var(--critical)", borderColor: "var(--critical)" }} onClick={() => action.mutate({ kind: "delete", d: confirmDelete })}>
                Delete device
              </button>
            </>
          }
        >
          <p style={{ marginTop: 0 }}>
            The device's MQTT credentials are revoked immediately and its configuration, tags and alarm rules are removed.
          </p>
          <label className="check">
            <input type="checkbox" checked={purge} onChange={(e) => setPurge(e.target.checked)} />
            Also delete all stored telemetry for this device
          </label>
        </Modal>
      )}
    </>
  );
}
