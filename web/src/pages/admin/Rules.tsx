import { FormEvent, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlarmRule, api, Device, RuleType, Severity } from "../../api";
import { useDevices } from "../../hooks";
import { ErrorText, Loading, Modal, SeverityBadge } from "../../components/ui";

const TYPE_LABELS: Record<RuleType, string> = {
  high: "Value above threshold",
  low: "Value below threshold",
  equals: "Value equals",
  fault: "Device reports FAULT",
  offline: "Device offline",
  stopped: "Machine stopped",
  process: "Process rule (built in)",
};
/** Types an admin can create; the others are made from tag limits or built in. */
const CREATABLE: RuleType[] = ["high", "low", "equals", "fault", "offline"];

function describe(r: AlarmRule): string {
  switch (r.rule_type) {
    case "high":
      return `${r.tag} > ${r.threshold}${r.deadband ? ` (clears below ${r.threshold! - r.deadband})` : ""}`;
    case "low":
      return `${r.tag} < ${r.threshold}${r.deadband ? ` (clears above ${r.threshold! + r.deadband})` : ""}`;
    case "equals":
      return `${r.tag} = ${r.threshold}`;
    case "fault":
      return "status = FAULT";
    case "offline":
      return `offline > ${r.threshold}s`;
    case "stopped":
      return "status = STOP";
    case "process":
      return r.message;
  }
}

type RuleForm = {
  name: string;
  device_id: string;
  tag: string;
  rule_type: RuleType;
  threshold: string;
  deadband: string;
  severity: Severity;
  message: string;
  webhook_url: string;
  enabled: boolean;
};

function RuleModal({ rule, devices, onClose }: { rule?: AlarmRule; devices: Device[]; onClose: () => void }) {
  const qc = useQueryClient();
  const [form, setForm] = useState<RuleForm>({
    name: rule?.name ?? "",
    device_id: rule?.device_id ?? "",
    tag: rule?.tag ?? "",
    rule_type: rule?.rule_type ?? "high",
    threshold: rule?.threshold != null ? String(rule.threshold) : "",
    deadband: String(rule?.deadband ?? 0),
    severity: rule?.severity ?? "warning",
    message: rule?.message ?? "",
    webhook_url: rule?.webhook_url ?? "",
    enabled: rule?.enabled ?? true,
  });
  const device = useQuery({
    queryKey: ["device", form.device_id],
    queryFn: () => api<Device>(`/devices/${encodeURIComponent(form.device_id)}`),
    enabled: !!form.device_id,
  });
  const knownTags = form.device_id
    ? (device.data?.tags ?? []).map((t) => t.tag)
    : [...new Set(devices.flatMap((d) => (d.preview_tags ?? []).map((t) => t.tag)))];
  const needsTag = ["high", "low", "equals"].includes(form.rule_type);
  const needsThreshold = form.rule_type !== "fault";

  const save = useMutation({
    mutationFn: () => {
      const body = {
        ...form,
        device_id: form.device_id || null,
        tag: needsTag ? form.tag : null,
        threshold: needsThreshold && form.threshold !== "" ? Number(form.threshold) : null,
        deadband: Number(form.deadband || 0),
        webhook_url: form.webhook_url || null,
      };
      return rule ? api(`/alarm-rules/${rule.id}`, { method: "PUT", body }) : api("/alarm-rules", { method: "POST", body });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["rules"] });
      qc.invalidateQueries({ queryKey: ["alarms"] });
      onClose();
    },
  });
  const set = (k: keyof RuleForm) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });
  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    save.mutate();
  };

  return (
    <Modal
      title={rule ? "Edit alarm rule" : "New alarm rule"}
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancel</button>
          <button className="primary" onClick={() => submit()} disabled={save.isPending}>
            Save rule
          </button>
        </>
      }
    >
      <form className="form-grid" onSubmit={submit}>
        <label className="field full">
          Name
          <input value={form.name} onChange={set("name")} placeholder="Boiler over-pressure" required />
        </label>
        <label className="field">
          Condition
          <select value={form.rule_type} onChange={set("rule_type")}>
            {CREATABLE.map((t) => (
              <option key={t} value={t}>
                {TYPE_LABELS[t]}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          Applies to
          <select value={form.device_id} onChange={set("device_id")}>
            <option value="">All devices</option>
            {devices.map((d) => (
              <option key={d.device_id} value={d.device_id}>
                {d.name || d.device_id}
              </option>
            ))}
          </select>
        </label>
        {needsTag && (
          <label className="field">
            Tag
            <input list="rule-tags" value={form.tag} onChange={set("tag")} required />
            <datalist id="rule-tags">
              {knownTags.map((t) => (
                <option key={t} value={t} />
              ))}
            </datalist>
          </label>
        )}
        {needsThreshold && (
          <label className="field">
            {form.rule_type === "offline" ? "Offline for more than (s)" : form.rule_type === "equals" ? "Value (booleans: 1 = ON, 0 = OFF)" : "Threshold"}
            <input type="number" step="any" value={form.threshold} onChange={set("threshold")} required={form.rule_type !== "offline"} placeholder={form.rule_type === "offline" ? "60" : ""} />
          </label>
        )}
        {(form.rule_type === "high" || form.rule_type === "low") && (
          <label className="field">
            Deadband
            <input type="number" min={0} step="any" value={form.deadband} onChange={set("deadband")} />
            <span className="hint">Alarm clears only after the value moves this far back, avoiding flapping</span>
          </label>
        )}
        <label className="field">
          Severity
          <select value={form.severity} onChange={set("severity")}>
            <option value="critical">Critical</option>
            <option value="warning">Warning</option>
            <option value="info">Info</option>
          </select>
        </label>
        <label className="field full">
          Message (optional)
          <input value={form.message} onChange={set("message")} placeholder="{device}: {tag} is {value} (limit {threshold})" />
          <span className="hint">Placeholders: {"{device} {tag} {value} {threshold}"}. Leave empty for an automatic message.</span>
        </label>
        <label className="field full">
          Webhook URL (optional)
          <input type="url" value={form.webhook_url} onChange={set("webhook_url")} placeholder="https://chat.googleapis.com/v1/spaces/…" />
          <span className="hint">Receives {'{"text": "..."}'} when the alarm is raised — works with Google Chat and Slack incoming webhooks</span>
        </label>
        <label className="check full">
          <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
          Enabled
        </label>
      </form>
      <ErrorText error={save.error} />
    </Modal>
  );
}

export function AdminRules() {
  const qc = useQueryClient();
  const devices = useDevices();
  const rules = useQuery({ queryKey: ["rules"], queryFn: () => api<AlarmRule[]>("/alarm-rules") });
  const [editing, setEditing] = useState<AlarmRule | "new" | null>(null);
  const remove = useMutation({
    mutationFn: (id: number) => api(`/alarm-rules/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["rules"] }),
  });
  const deviceName = (id: string | null) =>
    id ? devices.data?.find((d) => d.device_id === id)?.name || id : "All devices";

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Alarm rules</h1>
          <div className="sub">Conditions evaluated on every incoming message</div>
        </div>
        <button className="primary" onClick={() => setEditing("new")}>
          New rule
        </button>
      </div>
      <ErrorText error={remove.error} />
      <div className="card">
        {rules.isLoading && <Loading />}
        {rules.data && rules.data.length === 0 && <div className="empty">No alarm rules yet.</div>}
        {rules.data && rules.data.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Rule</th>
                  <th>Condition</th>
                  <th>Applies to</th>
                  <th>Severity</th>
                  <th>Notify</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rules.data.map((r) => (
                  <tr key={r.id} className={r.enabled ? undefined : "stale"}>
                    <td style={{ fontWeight: 500 }}>
                      {r.name}
                      {!r.enabled && <span className="small muted"> · disabled</span>}
                    </td>
                    <td className="mono small">{describe(r)}</td>
                    <td className="small">{deviceName(r.device_id)}</td>
                    <td>
                      <SeverityBadge severity={r.severity} />
                    </td>
                    <td className="small">{r.webhook_url ? "Webhook" : "—"}</td>
                    <td className="num nowrap">
                      {r.managed_by ? (
                        <span className="small muted">{r.managed_by.startsWith("process:") ? "Built in" : "From tag limits"}</span>
                      ) : (
                        <>
                          <button className="small ghost" onClick={() => setEditing(r)}>
                            Edit
                          </button>
                          <button className="small ghost danger" onClick={() => confirm(`Delete rule "${r.name}"?`) && remove.mutate(r.id)}>
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
        <RuleModal rule={editing === "new" ? undefined : editing} devices={devices.data ?? []} onClose={() => setEditing(null)} />
      )}
    </>
  );
}
