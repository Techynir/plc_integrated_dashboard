import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { ErrorText } from "./ui";
import { CloseIcon, WarningIcon } from "./icons";

type DataType = "int16" | "uint16" | "int32" | "uint32" | "float32" | "float64" | "bool";

interface Register {
  address: number;
  tag: string;
  data_type: DataType;
  value_labels: Record<string, string> | null;
  is_status: boolean;
  unit: string;
  display_name: string;
  valid_min: number | null;
  valid_max: number | null;
}

const TYPES: { value: DataType; label: string; width: number }[] = [
  { value: "int16", label: "16-bit integer", width: 1 },
  { value: "uint16", label: "16-bit unsigned", width: 1 },
  { value: "int32", label: "32-bit integer", width: 2 },
  { value: "uint32", label: "32-bit unsigned", width: 2 },
  { value: "float32", label: "32-bit real", width: 2 },
  { value: "float64", label: "64-bit real", width: 4 },
  { value: "bool", label: "Boolean", width: 1 },
];

const labelsToText = (l: Record<string, string> | null) =>
  l ? Object.entries(l).map(([k, v]) => `${k}=${v}`).join(", ") : "";
function textToLabels(text: string): Record<string, string> | null {
  const out: Record<string, string> = {};
  for (const part of text.split(/[,;]/)) {
    const m = part.match(/^\s*(-?\d+)\s*=\s*(.+?)\s*$/);
    if (m) out[m[1]] = m[2];
  }
  return Object.keys(out).length ? out : null;
}

const EXAMPLE = `Machine_Status - 400001 - 16Bit Integer 0=Stopped, 1=Running
Machine_Speed - 400002 - 32 Bit Real`;

/** Admin editor: which Modbus register holds which value, for gateway messages
 *  like {"PM3032_DATA":[{"full_addr":"400002","data":"[276.0, ...]"}]}. */
export function RegisterMapEditor({ deviceId }: { deviceId: string }) {
  const qc = useQueryClient();
  const saved = useQuery({
    queryKey: ["register-map", deviceId],
    queryFn: () => api<Register[]>(`/devices/${encodeURIComponent(deviceId)}/register-map`),
  });
  const [rows, setRows] = useState<Register[] | null>(null);
  const [open, setOpen] = useState(false);
  const [paste, setPaste] = useState("");
  const [notes, setNotes] = useState<{ warnings: string[]; errors: string[] } | null>(null);
  useEffect(() => {
    if (saved.data && rows === null) setRows(saved.data);
  }, [saved.data, rows]);

  const parse = useMutation({
    mutationFn: () =>
      api<{ registers: Register[]; warnings: string[]; errors: string[] }>(
        `/devices/${encodeURIComponent(deviceId)}/register-map/parse`,
        { method: "POST", body: { text: paste } },
      ),
    onSuccess: (r) => {
      setNotes({ warnings: r.warnings, errors: r.errors });
      if (r.registers.length) setRows(r.registers);
    },
  });
  const save = useMutation({
    mutationFn: () =>
      api<Register[]>(`/devices/${encodeURIComponent(deviceId)}/register-map`, { method: "PUT", body: { registers: rows ?? [] } }),
    onSuccess: (r) => {
      setRows(r);
      setNotes(null);
      qc.invalidateQueries({ queryKey: ["register-map", deviceId] });
      qc.invalidateQueries({ queryKey: ["device", deviceId] });
      qc.invalidateQueries({ queryKey: ["devices"] });
    },
  });

  const list = rows ?? [];
  const update = (i: number, patch: Partial<Register>) =>
    setRows(list.map((r, j) => (j === i ? { ...r, ...patch } : patch.is_status ? { ...r, is_status: false } : r)));
  const dirty = JSON.stringify(list) !== JSON.stringify(saved.data ?? []);

  if (!open && (saved.data?.length ?? 0) === 0) {
    return (
      <div className="card section card-pad row">
        <div>
          <h2>Register map</h2>
          <div className="small muted">For PLCs that publish Modbus register blocks (full_addr + data). Not set up.</div>
        </div>
        <span className="spacer" />
        <button onClick={() => setOpen(true)}>Set up register map</button>
      </div>
    );
  }

  return (
    <div className="card section">
      <div className="card-header" style={{ flexWrap: "wrap" }}>
        <div>
          <h2>Register map</h2>
          <div className="small muted">
            Which Modbus register holds which value. A block starting at <code>full_addr</code> fills consecutive registers
            (32-bit values use 2). Values outside the valid range, or codes without a label, are treated as bad reads.
          </div>
        </div>
        <div className="row">
          {dirty && <span className="small muted">unsaved changes</span>}
          <button className="primary" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
            Save register map
          </button>
        </div>
      </div>
      <div className="card-pad">
        <details open={list.length === 0}>
          <summary className="small">Paste a mapping list (one register per line)</summary>
          <textarea
            rows={6}
            style={{ width: "100%", marginTop: 8, fontFamily: "var(--mono)", fontSize: 12.5 }}
            placeholder={EXAMPLE}
            value={paste}
            onChange={(e) => setPaste(e.target.value)}
          />
          <div className="row" style={{ marginTop: 6 }}>
            <button className="small" disabled={!paste.trim() || parse.isPending} onClick={() => parse.mutate()}>
              Read list into the table
            </button>
            <span className="small muted">Format: Name - Address - Type [labels], e.g. "16Bit Integer 0=Stopped, 1=Running"</span>
          </div>
        </details>
        {notes && (notes.errors.length > 0 || notes.warnings.length > 0) && (
          <div style={{ marginTop: 10 }}>
            {notes.errors.map((e) => (
              <div key={e} className="error-text">
                <WarningIcon size={14} /> {e}
              </div>
            ))}
            {notes.warnings.map((w) => (
              <div key={w} className="small" style={{ color: "var(--ink-2)" }}>ℹ {w}</div>
            ))}
          </div>
        )}
        <ErrorText error={parse.error || save.error} />
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Register</th>
              <th>Tag name</th>
              <th>Type</th>
              <th>Display name</th>
              <th>Unit</th>
              <th title="Physically possible values. A read outside this range (e.g. while the PLC restarts) is rejected with its whole block.">
                Valid range
              </th>
              <th>Value labels</th>
              <th title="This value sets the device status (Running/Stopped)">Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((r, i) => (
              <tr key={i}>
                <td>
                  <input type="number" style={{ width: 100 }} value={r.address} onChange={(e) => update(i, { address: Number(e.target.value) })} />
                </td>
                <td>
                  <input className="mono" value={r.tag} onChange={(e) => update(i, { tag: e.target.value })} />
                </td>
                <td>
                  <select value={r.data_type} onChange={(e) => update(i, { data_type: e.target.value as DataType })}>
                    {TYPES.map((t) => (
                      <option key={t.value} value={t.value}>
                        {t.label}
                      </option>
                    ))}
                  </select>
                </td>
                <td>
                  <input value={r.display_name} placeholder={r.tag.replace(/_/g, " ")} onChange={(e) => update(i, { display_name: e.target.value })} />
                </td>
                <td>
                  <input style={{ width: 70 }} value={r.unit} onChange={(e) => update(i, { unit: e.target.value })} />
                </td>
                <td className="nowrap">
                  <input type="number" step="any" style={{ width: 80 }} placeholder="min" value={r.valid_min ?? ""}
                    onChange={(e) => update(i, { valid_min: e.target.value === "" ? null : Number(e.target.value) })} />
                  {" – "}
                  <input type="number" step="any" style={{ width: 80 }} placeholder="max" value={r.valid_max ?? ""}
                    onChange={(e) => update(i, { valid_max: e.target.value === "" ? null : Number(e.target.value) })} />
                </td>
                <td>
                  <input
                    defaultValue={labelsToText(r.value_labels)}
                    placeholder={r.data_type.includes("int") || r.data_type === "bool" ? "0=Stopped, 1=Running" : ""}
                    onBlur={(e) => update(i, { value_labels: textToLabels(e.target.value) })}
                  />
                </td>
                <td style={{ textAlign: "center" }}>
                  <input type="radio" name={`status-${deviceId}`} checked={r.is_status} onChange={() => update(i, { is_status: true })} />
                </td>
                <td>
                  <button className="small ghost" onClick={() => setRows(list.filter((_, j) => j !== i))} aria-label="Remove">
                    <CloseIcon />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="card-pad row">
        <button
          className="small"
          onClick={() => {
            const last = list[list.length - 1];
            const width = last ? TYPES.find((t) => t.value === last.data_type)?.width ?? 1 : 0;
            setRows([...list, { address: last ? last.address + width : 400001, tag: "", data_type: "float32", value_labels: null, is_status: false, unit: "", display_name: "", valid_min: null, valid_max: null }]);
          }}
        >
          + Add register
        </button>
        {list.some((r) => r.is_status) && (
          <button className="small ghost" onClick={() => setRows(list.map((r) => ({ ...r, is_status: false })))}>
            No status register
          </button>
        )}
      </div>
    </div>
  );
}
