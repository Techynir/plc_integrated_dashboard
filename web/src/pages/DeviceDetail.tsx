import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Device, History, qs, Tag } from "../api";
import { useAuth } from "../auth";
import { formatAge, formatDateTime, formatValue, secondsSince } from "../format";
import { useLiveDevice } from "../live";
import { mergeLive } from "./Overview";
import { TrendChart } from "../components/TrendChart";
import { ErrorText, Loading, Modal, SeverityBadge, SimulatedBadge, StatusBadge, useNow } from "../components/ui";
import { AlarmTable } from "./Alarms";
import type { Alarm } from "../api";

const PRESETS = [
  { key: "15m", label: "15m", seconds: 900 },
  { key: "1h", label: "1h", seconds: 3600 },
  { key: "8h", label: "8h", seconds: 8 * 3600 },
  { key: "24h", label: "24h", seconds: 86400 },
  { key: "7d", label: "7d", seconds: 7 * 86400 },
  { key: "30d", label: "30d", seconds: 30 * 86400 },
] as const;
type PresetKey = (typeof PRESETS)[number]["key"] | "custom";

function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function KpiTile({ tag, value, ts, staleAfter, now }: { tag: Tag; value: Tag["value"]; ts: string | null; staleAfter: number; now: number }) {
  const age = secondsSince(ts, now);
  const stale = age === null || age > staleAfter;
  const hasRange = typeof value === "number" && tag.min_value !== null && tag.max_value !== null && tag.max_value > tag.min_value;
  const pct = hasRange ? Math.min(100, Math.max(0, ((value - tag.min_value!) / (tag.max_value! - tag.min_value!)) * 100)) : 0;
  const outOfRange = hasRange && (value < tag.min_value! || value > tag.max_value!);
  return (
    <div className="card tile">
      <div className="tile-label">{tag.display_name}</div>
      <div className={`tile-value${stale ? " stale" : ""}`}>
        {formatValue(value, tag)}
        {tag.unit && tag.data_type === "number" ? <span className="unit">{tag.unit}</span> : null}
      </div>
      {hasRange && (
        <div className={`meter${outOfRange ? " warn" : ""}`} title={`Range ${tag.min_value} – ${tag.max_value}`}>
          <span style={{ width: `${pct}%` }} />
        </div>
      )}
      <div className="tile-foot">{stale ? `Stale · ${formatAge(age)}` : formatAge(age)}</div>
    </div>
  );
}

function TagEditModal({ deviceId, tag, onClose }: { deviceId: string; tag: Tag; onClose: () => void }) {
  const qc = useQueryClient();
  const [form, setForm] = useState({
    display_name: tag.display_name === tag.tag ? "" : tag.display_name,
    unit: tag.unit,
    data_type: tag.data_type,
    value_scale: String(tag.value_scale),
    value_offset: String(tag.value_offset),
    min_value: tag.min_value === null ? "" : String(tag.min_value),
    max_value: tag.max_value === null ? "" : String(tag.max_value),
    decimals: String(tag.decimals),
    pinned: tag.pinned,
  });
  const save = useMutation({
    mutationFn: () =>
      api(`/devices/${encodeURIComponent(deviceId)}/tags/${encodeURIComponent(tag.tag)}`, {
        method: "PATCH",
        body: {
          display_name: form.display_name,
          unit: form.unit,
          data_type: form.data_type,
          value_scale: Number(form.value_scale),
          value_offset: Number(form.value_offset),
          min_value: form.min_value === "" ? null : Number(form.min_value),
          max_value: form.max_value === "" ? null : Number(form.max_value),
          decimals: Number(form.decimals),
          pinned: form.pinned,
        },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["device", deviceId] });
      qc.invalidateQueries({ queryKey: ["devices"] });
      onClose();
    },
  });
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });

  return (
    <Modal
      title={`Configure tag ${tag.tag}`}
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancel</button>
          <button className="primary" onClick={() => save.mutate()} disabled={save.isPending}>
            Save
          </button>
        </>
      }
    >
      <form className="form-grid" onSubmit={(e: FormEvent) => (e.preventDefault(), save.mutate())}>
        <label className="field">
          Display name
          <input value={form.display_name} placeholder={tag.tag} onChange={set("display_name")} />
        </label>
        <label className="field">
          Unit
          <input value={form.unit} placeholder="e.g. °C, bar, rpm" onChange={set("unit")} />
        </label>
        <label className="field">
          Data type
          <select value={form.data_type} onChange={set("data_type")}>
            <option value="number">Number</option>
            <option value="boolean">Boolean (0/1)</option>
            <option value="string">Text</option>
          </select>
        </label>
        <label className="field">
          Decimals
          <input type="number" min={0} max={6} value={form.decimals} onChange={set("decimals")} />
        </label>
        <label className="field">
          Scale
          <input type="number" step="any" value={form.value_scale} onChange={set("value_scale")} />
          <span className="hint">stored = raw × scale + offset (applies to new data)</span>
        </label>
        <label className="field">
          Offset
          <input type="number" step="any" value={form.value_offset} onChange={set("value_offset")} />
        </label>
        <label className="field">
          Expected minimum
          <input type="number" step="any" value={form.min_value} onChange={set("min_value")} />
        </label>
        <label className="field">
          Expected maximum
          <input type="number" step="any" value={form.max_value} onChange={set("max_value")} />
          <span className="hint">Min/max draw a gauge on the KPI tile</span>
        </label>
        <label className="check full">
          <input type="checkbox" checked={form.pinned} onChange={(e) => setForm({ ...form, pinned: e.target.checked })} />
          Pin as KPI (shown on the overview card and at the top of this page)
        </label>
      </form>
      <ErrorText error={save.error} />
    </Modal>
  );
}

function Trends({ device, tags }: { device: Device; tags: Tag[] }) {
  const trendable = tags.filter((t) => t.data_type !== "string");
  const defaults = useMemo(() => {
    const pinned = trendable.filter((t) => t.pinned).map((t) => t.tag);
    return (pinned.length ? pinned : trendable.filter((t) => t.data_type === "number").map((t) => t.tag)).slice(0, 3);
  }, [trendable.map((t) => t.tag).join(",")]); // eslint-disable-line react-hooks/exhaustive-deps
  const [selected, setSelected] = useState<string[] | null>(null);
  const chosen = selected ?? defaults;
  const [preset, setPreset] = useState<PresetKey>("1h");
  const [custom, setCustom] = useState(() => ({
    from: toLocalInput(new Date(Date.now() - 86400_000)),
    to: toLocalInput(new Date()),
  }));
  const live = preset === "15m" || preset === "1h";

  const range = () => {
    if (preset === "custom") return { from: new Date(custom.from).toISOString(), to: new Date(custom.to).toISOString() };
    const seconds = PRESETS.find((p) => p.key === preset)!.seconds;
    const to = new Date();
    return { from: new Date(to.getTime() - seconds * 1000).toISOString(), to: to.toISOString() };
  };

  const history = useQuery({
    queryKey: ["history", device.device_id, chosen.join(","), preset, preset === "custom" ? custom : null],
    queryFn: () => api<History>(`/devices/${encodeURIComponent(device.device_id)}/history${qs({ tags: chosen.join(","), ...range() })}`),
    enabled: chosen.length > 0,
    refetchInterval: live ? 5000 : preset === "8h" || preset === "24h" ? 60_000 : false,
    placeholderData: (prev) => prev,
  });

  const exportUrl = () =>
    `/api/v1/devices/${encodeURIComponent(device.device_id)}/export.csv${qs({ tags: chosen.join(","), ...range() })}`;

  const toggle = (tag: string) =>
    setSelected(chosen.includes(tag) ? chosen.filter((t) => t !== tag) : [...chosen, tag].slice(-6));

  const h = history.data;
  return (
    <div className="card section">
      <div className="card-header" style={{ flexWrap: "wrap" }}>
        <h2>Trends</h2>
        <div className="row">
          <div className="segmented" role="group" aria-label="Time range">
            {PRESETS.map((p) => (
              <button key={p.key} className={preset === p.key ? "on" : ""} onClick={() => setPreset(p.key)}>
                {p.label}
              </button>
            ))}
            <button className={preset === "custom" ? "on" : ""} onClick={() => setPreset("custom")}>
              Custom
            </button>
          </div>
          <a className="btn small" href={exportUrl()} download>
            Download CSV
          </a>
        </div>
      </div>
      <div className="card-pad" style={{ paddingBottom: 4 }}>
        {preset === "custom" && (
          <div className="row" style={{ marginBottom: 10 }}>
            <input type="datetime-local" value={custom.from} onChange={(e) => setCustom({ ...custom, from: e.target.value })} />
            <span className="muted">to</span>
            <input type="datetime-local" value={custom.to} onChange={(e) => setCustom({ ...custom, to: e.target.value })} />
          </div>
        )}
        <div className="row">
          <span className="small secondary">Tags</span>
          {trendable.map((t) => (
            <button key={t.tag} className={`chip${chosen.includes(t.tag) ? " on" : ""}`} onClick={() => toggle(t.tag)}>
              {t.display_name}
            </button>
          ))}
        </div>
        <div className="small muted" style={{ marginTop: 8 }}>
          {h
            ? `${h.resolution === "raw" ? "Raw data" : h.resolution === "1m" ? "1-minute rollups" : "1-hour rollups"}, ${h.bucket_s}s buckets${live ? " · refreshing every 5s" : ""} · scroll to zoom · shaded band = min–max`
            : " "}
        </div>
      </div>
      <ErrorText error={history.error} />
      {chosen.length === 0 && <div className="empty">Select one or more tags to plot.</div>}
      {h &&
        chosen.map((name) => {
          const tag = tags.find((t) => t.tag === name);
          if (!tag) return null;
          const points = h.series[name] ?? [];
          return (
            <div key={name} style={{ borderTop: "1px solid var(--border)" }}>
              <div className="trend-title">
                <h3>{tag.display_name}</h3>
                <span className="small muted">{tag.unit}</span>
              </div>
              {points.length === 0 ? (
                <div className="empty">No data in this range.</div>
              ) : (
                <TrendChart
                  points={points}
                  from={new Date(h.from).getTime()}
                  to={new Date(h.to).getTime()}
                  bucketS={h.bucket_s}
                  expectedIntervalS={device.expected_interval_s}
                  dataType={tag.data_type}
                  unit={tag.unit}
                  decimals={tag.decimals}
                  group={`trends-${device.device_id}`}
                  label={tag.display_name}
                />
              )}
            </div>
          );
        })}
    </div>
  );
}

export function DeviceDetail() {
  const { id = "" } = useParams();
  const { can } = useAuth();
  const now = useNow(1000);
  const liveDev = useLiveDevice(id);
  const [editing, setEditing] = useState<Tag | null>(null);
  const device = useQuery({
    queryKey: ["device", id],
    queryFn: () => api<Device>(`/devices/${encodeURIComponent(id)}`),
    refetchInterval: 30_000,
  });
  const alarms = useQuery({
    queryKey: ["alarms", "device", id],
    queryFn: () => api<Alarm[]>(`/alarms${qs({ state: "all", device_id: id, limit: 50 })}`),
    refetchInterval: 30_000,
  });
  useEffect(() => {
    document.title = `${device.data?.name || id} · PLC Dashboard`;
    return () => {
      document.title = "PLC Dashboard";
    };
  }, [device.data?.name, id]);

  if (device.isLoading) return <Loading />;
  if (device.error || !device.data) return <ErrorText error={device.error ?? "Device not found"} />;

  const d = device.data;
  const tags = d.tags ?? [];
  const m = mergeLive(d, liveDev);
  const staleAfter = Math.max(15, 3 * d.expected_interval_s);
  const pinned = tags.filter((t) => t.pinned);
  const kpis = (pinned.length ? pinned : tags.filter((t) => t.data_type === "number")).slice(0, 8);
  const active = (alarms.data ?? []).filter((a) => a.active);

  return (
    <>
      <div className="page-header">
        <div>
          <div className="small">
            <Link to="/">Overview</Link> <span className="muted">/ {[d.site, d.line].filter(Boolean).join(" / ")}</span>
          </div>
          <div className="row" style={{ marginTop: 4 }}>
            <h1>{d.name || d.device_id}</h1>
            <StatusBadge online={m.online} status={m.status} enabled={d.enabled} />
            {d.simulated && <SimulatedBadge />}
          </div>
          <div className="sub small">
            <span className="mono">{d.device_id}</span> · last message {formatAge(secondsSince(m.lastSeen, now))}
            {d.description ? ` · ${d.description}` : ""}
          </div>
        </div>
        <div className="row">
          <Link className="btn small" to={`/raw?device=${encodeURIComponent(d.device_id)}`}>
            Raw data
          </Link>
          {can("admin") && (
            <Link className="btn small" to={`/admin/devices?focus=${encodeURIComponent(d.device_id)}`}>
              Manage device
            </Link>
          )}
        </div>
      </div>

      {active.length > 0 && (
        <Link to="/alarms" className={`alert ${active.some((a) => a.severity === "critical") ? "critical" : "warning"}`} style={{ color: "inherit" }}>
          <SeverityBadge severity={active.some((a) => a.severity === "critical") ? "critical" : active[0].severity} />
          <span>
            <strong>{active.length} active alarm{active.length > 1 ? "s" : ""}</strong> · {active[0].message}
          </span>
        </Link>
      )}

      {kpis.length > 0 ? (
        <div className="grid grid-tiles">
          {kpis.map((t) => (
            <KpiTile key={t.tag} tag={t} value={m.value(t)} ts={m.ts(t)} staleAfter={staleAfter} now={now} />
          ))}
        </div>
      ) : (
        <div className="card empty">No data received from this device yet.</div>
      )}

      {tags.length > 0 && <Trends device={d} tags={tags} />}

      <div className="card section">
        <div className="card-header">
          <h2>All tags</h2>
          <span className="small muted">
            {tags.length} tags · {d.msg_count.toLocaleString()} messages · {d.seq_gaps} missed (seq gaps)
          </span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Tag</th>
                <th className="num">Value</th>
                <th>Unit</th>
                <th>Quality</th>
                <th>Updated</th>
                {can("admin") && <th />}
              </tr>
            </thead>
            <tbody>
              {tags.map((t) => {
                const age = secondsSince(m.ts(t), now);
                const q = liveDev?.values[t.tag] ? ["GOOD", "UNCERTAIN", "BAD"][liveDev.values[t.tag].q] : t.quality;
                return (
                  <tr key={t.tag}>
                    <td>
                      <div style={{ fontWeight: 500 }}>
                        {t.display_name} {t.pinned && <span title="Pinned KPI">★</span>}
                      </div>
                      {t.display_name !== t.tag && <div className="small muted mono">{t.tag}</div>}
                      {!t.configured && <div className="small muted">new · not configured</div>}
                    </td>
                    <td className={`num${age === null || age > staleAfter ? " stale" : ""}`} style={{ fontWeight: 600 }}>
                      {formatValue(m.value(t), t)}
                    </td>
                    <td className="secondary">{t.unit}</td>
                    <td>
                      {q && q !== "GOOD" ? (
                        <span className="badge">
                          <span className="dot" style={{ background: q === "BAD" ? "var(--critical)" : "var(--warning)" }} />
                          {q}
                        </span>
                      ) : (
                        <span className="muted small">{q ?? "—"}</span>
                      )}
                    </td>
                    <td className="small secondary nowrap" title={formatDateTime(m.ts(t))}>
                      {formatAge(age)}
                    </td>
                    {can("admin") && (
                      <td className="num">
                        <button className="small ghost" onClick={() => setEditing(t)}>
                          Configure
                        </button>
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card section">
        <div className="card-header">
          <h2>Recent alarms</h2>
        </div>
        <AlarmTable alarms={alarms.data ?? []} showDevice={false} />
      </div>

      {editing && <TagEditModal deviceId={d.device_id} tag={editing} onClose={() => setEditing(null)} />}
    </>
  );
}
