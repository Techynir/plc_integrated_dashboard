import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, AssetConfig, Device, Role_, Tag } from "../../api";
import { useDevice } from "../../assetApi";
import { useAuth } from "../../auth";
import { useAsset } from "../../hooks";
import { Panel, ScreenHead } from "../../components/console";
import { RegisterMapEditor } from "../../components/RegisterMap";
import { ErrorText, Loading } from "../../components/ui";
import { orderTags } from "./Live";

const ROLES: { value: Role_; label: string }[] = [
  { value: "machine_status", label: "Machine status" },
  { value: "speed", label: "Speed" },
  { value: "motor_current", label: "Motor current" },
  { value: "steam_pressure", label: "Steam pressure" },
  { value: "moisture", label: "Moisture" },
  { value: "vibration", label: "Vibration" },
];

type Field = { key: keyof AssetConfig; label: string; type?: "number" | "select"; options?: string[]; hint?: string };

const CONNECTION: Field[] = [
  { key: "protocol", label: "Protocol", hint: "e.g. Modbus TCP" },
  { key: "gateway", label: "Gateway", hint: "make / model" },
  { key: "plc_ip", label: "PLC IP address" },
  { key: "port", label: "Port" },
  { key: "unit_id", label: "Unit ID" },
  { key: "function_code", label: "Function code", hint: "e.g. 03 Read holding registers" },
  { key: "block_read", label: "Block read", hint: "e.g. 400001–400011" },
  { key: "poll_interval_ms", label: "Poll interval (ms)", type: "number" },
  { key: "timeout", label: "Read timeout" },
  { key: "byte_order", label: "Float32 byte order", type: "select", options: ["ABCD", "CDAB", "BADC", "DCBA"] },
  { key: "timestamp_source", label: "Timestamp source", hint: "e.g. server receive time" },
  { key: "on_read_failure", label: "On read failure", hint: "e.g. skip record" },
];

const ANALYTICS: Field[] = [
  { key: "running_source", label: "Running state from", type: "select", options: ["auto", "status", "speed"], hint: "auto = status register unless it contradicts speed" },
  { key: "running_speed_min", label: "Running above speed", type: "number", hint: "blank = half the normal minimum speed" },
  { key: "comms_timeout_s", label: "Comms timeout (s)", type: "number", hint: "silence longer than this = no data (default 15)" },
  { key: "speed_target", label: "Speed target", type: "number", hint: "for Performance; blank = none" },
];

type Row = {
  tag: string;
  display_name: string;
  unit: string;
  role: Role_ | "";
  min_value: string;
  max_value: string;
  limit_dir: "" | "high" | "low";
  warn_limit: string;
  crit_limit: string;
  suppress_when_stopped: boolean;
  guidance: string;
};

const s = (v: number | null | undefined) => (v === null || v === undefined ? "" : String(v));
const n = (v: string) => (v.trim() === "" ? null : Number(v));

function toRow(t: Tag): Row {
  return {
    tag: t.tag,
    display_name: t.display_name ?? "",
    unit: t.unit ?? "",
    role: t.role ?? "",
    min_value: s(t.min_value),
    max_value: s(t.max_value),
    limit_dir: t.limit_dir ?? "",
    warn_limit: s(t.warn_limit),
    crit_limit: s(t.crit_limit),
    suppress_when_stopped: t.suppress_when_stopped ?? true,
    guidance: t.guidance ?? "",
  };
}

function ConnectionForm({ d, readOnly }: { d: Device; readOnly: boolean }) {
  const qc = useQueryClient();
  const [cfg, setCfg] = useState<AssetConfig>(d.asset_config ?? {});
  const [type, setType] = useState(d.asset_type ?? "");
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    setCfg(d.asset_config ?? {});
    setType(d.asset_type ?? "");
  }, [d.device_id]); // eslint-disable-line react-hooks/exhaustive-deps
  const save = useMutation({
    mutationFn: () => api(`/devices/${encodeURIComponent(d.device_id)}`, { method: "PATCH", body: { asset_type: type, asset_config: cfg } }),
    onSuccess: () => {
      setSaved(true);
      qc.invalidateQueries({ queryKey: ["device", d.device_id] });
      qc.invalidateQueries({ queryKey: ["devices"] });
      qc.invalidateQueries({ queryKey: ["asset", d.device_id] });
    },
  });
  const field = (f: Field) => {
    const v = cfg[f.key];
    const set = (val: string) => {
      setSaved(false);
      setCfg({ ...cfg, [f.key]: f.type === "number" ? n(val) : val || undefined });
    };
    return (
      <label key={f.key}>
        <span>{f.label}</span>
        {f.type === "select" ? (
          <select value={String(v ?? f.options![0])} onChange={(e) => set(e.target.value)} disabled={readOnly}>
            {f.options!.map((o) => (
              <option key={o}>{o}</option>
            ))}
          </select>
        ) : (
          <input type={f.type === "number" ? "number" : "text"} step="any" value={v == null ? "" : String(v)} onChange={(e) => set(e.target.value)} disabled={readOnly} />
        )}
        {f.hint && <small className="hint">{f.hint}</small>}
      </label>
    );
  };
  return (
    <>
      <Panel title="Connection" sub="PLC → gateway details, for the record and for commissioning">
        <div className="cfg">
          <label>
            <span>Asset type</span>
            <input value={type} onChange={(e) => (setType(e.target.value), setSaved(false))} disabled={readOnly} placeholder="e.g. Paper machine" />
          </label>
          {CONNECTION.map(field)}
        </div>
      </Panel>
      <Panel
        title="Analytics settings"
        sub="how running time and outages are worked out"
        actions={
          !readOnly && (
            <>
              {saved && <span className="small" style={{ color: "var(--good-ink)" }}>Saved</span>}
              <button className="primary small" onClick={() => save.mutate()} disabled={save.isPending}>
                Save connection & settings
              </button>
            </>
          )
        }
      >
        <div className="cfg">{ANALYTICS.map(field)}</div>
        <ErrorText error={save.error} />
      </Panel>
    </>
  );
}

function TagTable({ d, readOnly }: { d: Device; readOnly: boolean }) {
  const qc = useQueryClient();
  const [rows, setRows] = useState<Row[]>([]);
  const [dirty, setDirty] = useState(false);
  useEffect(() => {
    if (!dirty) setRows(orderTags(d.tags ?? []).map(toRow));
  }, [d, dirty]);
  const save = useMutation({
    mutationFn: () =>
      api<Tag[]>(`/devices/${encodeURIComponent(d.device_id)}/tag-settings`, {
        method: "PUT",
        body: {
          tags: rows.map((r) => ({
            tag: r.tag,
            display_name: r.display_name,
            unit: r.unit,
            role: r.role || null,
            min_value: n(r.min_value),
            max_value: n(r.max_value),
            limit_dir: r.limit_dir || null,
            warn_limit: n(r.warn_limit),
            crit_limit: n(r.crit_limit),
            suppress_when_stopped: r.suppress_when_stopped,
            guidance: r.guidance,
          })),
        },
      }),
    onSuccess: () => {
      setDirty(false);
      qc.invalidateQueries({ queryKey: ["device", d.device_id] });
      qc.invalidateQueries({ queryKey: ["devices"] });
      qc.invalidateQueries({ queryKey: ["asset", d.device_id] });
      qc.invalidateQueries({ queryKey: ["rules"] });
    },
  });
  const set = (i: number, patch: Partial<Row>) => {
    setDirty(true);
    setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  };
  const used = new Map(rows.filter((r) => r.role).map((r) => [r.role, r.tag]));

  return (
    <Panel
      title="Tag mapping & limits"
      sub="Roles tell the analytics which signal is which. Warning/critical limits become alarm rules automatically (3 s delay)."
      actions={
        !readOnly && (
          <>
            {dirty && (
              <button className="small ghost" onClick={() => setDirty(false)}>
                Discard
              </button>
            )}
            <button className="primary small" onClick={() => save.mutate()} disabled={!dirty || save.isPending}>
              Save tags & limits
            </button>
          </>
        )
      }
    >
      <ErrorText error={save.error} />
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Tag</th>
              <th>Name</th>
              <th>Unit</th>
              <th>Role</th>
              <th>Normal min</th>
              <th>Normal max</th>
              <th>Limit</th>
              <th>Warning</th>
              <th>Critical</th>
              <th title="Hold this tag's alarms while the machine is stopped">Hold when stopped</th>
              <th>Operator guidance</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={r.tag}>
                <td className="mono small">{r.tag}</td>
                <td>
                  <input value={r.display_name} onChange={(e) => set(i, { display_name: e.target.value })} disabled={readOnly} style={{ width: 150 }} aria-label={`${r.tag} name`} />
                </td>
                <td>
                  <input value={r.unit} onChange={(e) => set(i, { unit: e.target.value })} disabled={readOnly} style={{ width: 64 }} aria-label={`${r.tag} unit`} />
                </td>
                <td>
                  <select value={r.role} onChange={(e) => set(i, { role: e.target.value as Role_ | "" })} disabled={readOnly} aria-label={`${r.tag} role`}>
                    <option value="">—</option>
                    {ROLES.map((ro) => (
                      <option key={ro.value} value={ro.value} disabled={used.has(ro.value) && used.get(ro.value) !== r.tag}>
                        {ro.label}
                      </option>
                    ))}
                  </select>
                </td>
                {(["min_value", "max_value"] as const).map((k) => (
                  <td key={k}>
                    <input type="number" step="any" value={r[k]} onChange={(e) => set(i, { [k]: e.target.value })} disabled={readOnly} aria-label={`${r.tag} ${k}`} />
                  </td>
                ))}
                <td>
                  <select value={r.limit_dir} onChange={(e) => set(i, { limit_dir: e.target.value as Row["limit_dir"] })} disabled={readOnly} aria-label={`${r.tag} limit direction`}>
                    <option value="">none</option>
                    <option value="high">high (above)</option>
                    <option value="low">low (below)</option>
                  </select>
                </td>
                {(["warn_limit", "crit_limit"] as const).map((k) => (
                  <td key={k}>
                    <input type="number" step="any" value={r[k]} onChange={(e) => set(i, { [k]: e.target.value })} disabled={readOnly || !r.limit_dir} aria-label={`${r.tag} ${k}`} />
                  </td>
                ))}
                <td style={{ textAlign: "center" }}>
                  <input type="checkbox" checked={r.suppress_when_stopped} onChange={(e) => set(i, { suppress_when_stopped: e.target.checked })} disabled={readOnly} aria-label={`${r.tag} hold alarms when stopped`} />
                </td>
                <td>
                  <input value={r.guidance} onChange={(e) => set(i, { guidance: e.target.value })} disabled={readOnly} style={{ width: 240 }} placeholder="What to check when this alarms" aria-label={`${r.tag} guidance`} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

export function Config() {
  const { asset } = useAsset();
  const { can } = useAuth();
  const detail = useDevice(asset?.device_id, false);
  if (!asset) return <div className="panel empty">No asset selected.</div>;
  if (detail.isLoading || !detail.data) return <Loading />;
  const readOnly = !can("admin");
  return (
    <div className="screen">
      <ScreenHead
        eyebrow={`Platform · ${asset.device_id}`}
        title="Asset configuration"
        desc={readOnly ? "How this asset is connected and mapped. Only admins can change it." : "Connection details, which tag plays which role, normal ranges and alarm limits, and the Modbus register map."}
      />
      <ConnectionForm d={detail.data} readOnly={readOnly} />
      <TagTable d={detail.data} readOnly={readOnly} />
      {!readOnly && (
        <Panel title="Register map" sub="which Modbus register holds which value">
          <RegisterMapEditor deviceId={asset.device_id} />
        </Panel>
      )}
    </div>
  );
}
