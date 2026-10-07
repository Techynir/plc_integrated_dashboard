import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, qs, Tag } from "../../api";
import { TagStats, useDevice, useHistory, windowEnd } from "../../assetApi";
import { useAsset } from "../../hooks";
import { Chip, fmtDT, fmtNum, fmtPct, fmtT, ScreenHead } from "../../components/console";
import { Lanes, seriesColor } from "../../components/charts";
import { Loading, useNow } from "../../components/ui";
import { lanesFor, orderTags } from "./Live";

const RANGES = [
  { key: "5m", label: "5 min", ms: 5 * 60_000 },
  { key: "15m", label: "15 min", ms: 15 * 60_000 },
  { key: "1h", label: "1 h", ms: 3600_000 },
  { key: "8h", label: "8 h", ms: 8 * 3600_000 },
  { key: "24h", label: "24 h", ms: 24 * 3600_000 },
];

/** Plant shifts, local time. C runs from 22:00 through 06:00 the next morning. */
const SHIFTS = [
  { key: "A", label: "A · 06–14", from: "06:00", to: "14:00" },
  { key: "B", label: "B · 14–22", from: "14:00", to: "22:00" },
  { key: "C", label: "C · 22–06", from: "22:00", to: "06:00" },
] as const;

type ShiftKey = (typeof SHIFTS)[number]["key"];

function ymd(ms: number): string {
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function atLocal(date: string, time: string): number {
  const [y, m, d] = date.split("-").map(Number);
  const [hh, mm] = time.split(":").map(Number);
  return new Date(y, m - 1, d, hh, mm, 0, 0).getTime();
}

/** One calendar day, a clock range on that day, or a shift. A clock that passes midnight ends the next morning. */
function customWindow(date: string, timeFrom: string, timeTo: string): { from: number; to: number } | null {
  if (!date) return null;
  if (!timeFrom && !timeTo) {
    const from = atLocal(date, "00:00");
    return { from, to: from + 86_400_000 };
  }
  const from = atLocal(date, timeFrom || "00:00");
  let to = atLocal(date, timeTo || "23:59");
  if (to <= from) to += 86_400_000;
  return { from, to };
}

export function Trends() {
  const { asset, loading } = useAsset();
  const id = asset?.device_id;
  const detail = useDevice(id, 60_000);
  const [range, setRange] = useState("1h");
  const [date, setDate] = useState("");
  const [timeFrom, setTimeFrom] = useState("");
  const [timeTo, setTimeTo] = useState("");
  const [shift, setShift] = useState<ShiftKey | "">("");
  const [picked, setPicked] = useState<string[] | null>(null);
  const now = useNow(10_000);
  const tags = useMemo(() => orderTags((detail.data?.tags ?? []).filter((t) => t.data_type === "number")), [detail.data]);
  // colour follows the tag (its position in the asset's tag list), never the selection
  const colorOf = useMemo(() => {
    const idx = new Map(tags.filter((t) => t.role !== "machine_status").map((t, i) => [t.tag, i]));
    return (t: Tag) => seriesColor(idx.get(t.tag) ?? -1);
  }, [tags]);

  useEffect(() => setPicked(null), [id]);
  const selected = picked ?? tags.filter((t) => t.role !== "machine_status").map((t) => t.tag);
  const shown = tags.filter((t) => selected.includes(t.tag));

  const span = RANGES.find((r) => r.key === range)!.ms;
  const live = windowEnd(asset?.last_seen, span, Math.floor(now / 10_000) * 10_000);
  const custom = customWindow(date, timeFrom, timeTo);
  const from = custom ? custom.from : live.end - span;
  const end = custom ? custom.to : live.end;
  const anchored = custom ? false : live.anchored;
  const watching = custom ? end > now : !anchored;
  const hist = useHistory(id, shown.map((t) => t.tag), from, end, watching ? (end - from <= 3600_000 ? 10_000 : 60_000) : false);
  const stats = useQuery({
    queryKey: ["asset", id, "stats", shown.map((t) => t.tag).join(","), Math.round(from / 60_000), Math.round(end / 60_000)],
    queryFn: () =>
      api<{ tags: TagStats[] }>(`/assets/${encodeURIComponent(id!)}/stats${qs({ tags: shown.map((t) => t.tag).join(","), from: new Date(from).toISOString(), to: new Date(end).toISOString() })}`),
    enabled: !!id && shown.length > 0,
    placeholderData: (prev) => prev,
  });

  if (loading || detail.isLoading) return <Loading />;
  if (!asset) return <div className="panel empty">No asset selected.</div>;

  const dayOfData = ymd(asset.last_seen ? new Date(asset.last_seen).getTime() : now);
  const usePreset = (key: string) => {
    setRange(key);
    setDate("");
    setTimeFrom("");
    setTimeTo("");
    setShift("");
  };
  const useShift = (key: ShiftKey) => {
    const s = SHIFTS.find((x) => x.key === key)!;
    setShift(key);
    setDate((d) => d || dayOfData);
    setTimeFrom(s.from);
    setTimeTo(s.to);
  };
  const useDate = (value: string) => {
    setDate(value);
    if (!value) {
      setTimeFrom("");
      setTimeTo("");
      setShift("");
    }
  };
  const useTime = (which: "from" | "to", value: string) => {
    setShift("");
    if (which === "from") setTimeFrom(value);
    else setTimeTo(value);
    setDate((d) => d || dayOfData);
  };
  const toggle = (tag: string, on: boolean) => {
    const next = on ? [...selected, tag] : selected.filter((t) => t !== tag);
    if (next.length) setPicked(next); // keep at least one signal
  };
  const csv = `/api/v1/devices/${encodeURIComponent(asset.device_id)}/export.csv${qs({ tags: shown.map((t) => t.tag).join(","), from: new Date(from).toISOString(), to: new Date(end).toISOString() })}`;
  const records = stats.data?.tags.reduce((n, s) => Math.max(n, s.samples), 0) ?? 0;

  return (
    <div className="screen">
      <ScreenHead
        eyebrow={`Historian · ${asset.name || asset.device_id}`}
        title="Trends & historian"
      />
      {anchored && (
        <div className="alert comms">
          <Chip cls="comms">No recent data</Chip>
          <span>
            Showing the {RANGES.find((r) => r.key === range)!.label} up to the last record, <b>{fmtDT(asset.last_seen)}</b>.
          </span>
        </div>
      )}
      <section className="panel">
        <div className="toolbar" style={{ marginBottom: 10 }}>
          <div className="segmented" role="group" aria-label="Time range">
            {RANGES.map((r) => (
              <button key={r.key} type="button" className={!custom && range === r.key ? "on" : ""} aria-pressed={!custom && range === r.key} onClick={() => usePreset(r.key)}>
                {r.label}
              </button>
            ))}
          </div>
          <label className="filter">
            Date
            <input type="date" value={date} max={ymd(now)} onChange={(e) => useDate(e.target.value)} />
          </label>
          <label className="filter">
            From
            <input type="time" value={timeFrom} onChange={(e) => useTime("from", e.target.value)} />
          </label>
          <label className="filter">
            To
            <input type="time" value={timeTo} onChange={(e) => useTime("to", e.target.value)} />
          </label>
          <div className="segmented" role="group" aria-label="Shift">
            {SHIFTS.map((s) => (
              <button key={s.key} type="button" className={shift === s.key ? "on" : ""} aria-pressed={shift === s.key} onClick={() => useShift(s.key)}>
                {s.label}
              </button>
            ))}
          </div>
          <div className="tagchips" role="group" aria-label="Tags">
            {tags.map((t) => (
              <label key={t.tag}>
                <input type="checkbox" checked={selected.includes(t.tag)} onChange={(e) => toggle(t.tag, e.target.checked)} />
                <i className="swatch" style={{ background: colorOf(t) }} aria-hidden="true" />
                {t.display_name || t.tag}
              </label>
            ))}
          </div>
          <span className="spacer" />
          <a className="btn" href={csv} download>
            Export CSV
          </a>
        </div>
        <div className="legend" style={{ marginBottom: 8 }}>
          <span>
            <i style={{ background: "var(--band)", border: "1px solid var(--good)" }} />
            Normal range
          </span>
          <span>
            <i style={{ background: "var(--warning)", height: 2 }} />
            Warning
          </span>
          <span>
            <i style={{ background: "var(--critical)", height: 2 }} />
            Critical
          </span>
          <span>
            <i style={{ background: "repeating-linear-gradient(135deg, var(--comms) 0 2px, transparent 2px 5px)" }} />
            Communication lost
          </span>
          <span>
            <i style={{ background: "var(--trace-fill)", border: "1px solid var(--trace)" }} />
            Min–max envelope (long ranges)
          </span>
        </div>
        <Lanes
          lanes={lanesFor(shown, hist.data?.series, undefined, colorOf)}
          from={from}
          to={end}
          gapMs={Math.max((hist.data?.bucket_s ?? 0) * 2000, Math.max(15, asset.asset_config?.comms_timeout_s ?? 15) * 1000)}
          laneHeight={shown.length > 4 ? 72 : 96}
        />
      </section>
      <section className="panel">
        <div className="phead">
          <h2>Statistics for the selected range</h2>
          <span className="sub">
            {fmtDT(from)} → {fmtT(end)} · {records.toLocaleString("en-IN")} records · running periods only for min / avg / max
          </span>
        </div>
        {stats.data?.tags.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Tag</th>
                  <th className="num">Min</th>
                  <th className="num">Avg</th>
                  <th className="num">Max</th>
                  <th className="num">Std dev</th>
                  <th className="num">In normal range</th>
                  <th className="num">Valid samples</th>
                </tr>
              </thead>
              <tbody>
                {stats.data.tags.map((s) => {
                  const t = tags.find((x) => x.tag === s.tag);
                  return (
                    <tr key={s.tag}>
                      <td>
                        {t && <i className="swatch" style={{ background: colorOf(t), marginRight: 8, verticalAlign: "middle" }} aria-hidden="true" />}
                        {s.label} <span className="muted">({s.unit})</span>
                      </td>
                      <td className="num">{fmtNum(s.min, s.decimals)}</td>
                      <td className="num">{fmtNum(s.avg, s.decimals)}</td>
                      <td className="num">{fmtNum(s.max, s.decimals)}</td>
                      <td className="num">{fmtNum(s.sd, s.decimals + 1)}</td>
                      <td className="num">{s.in_normal != null ? `${fmtPct(s.in_normal)} %` : "—"}</td>
                      <td className="num">{s.valid != null ? `${fmtPct(s.valid, 2)} %` : "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty">{stats.isLoading ? "Calculating…" : "No data in this range."}</div>
        )}
      </section>
    </div>
  );
}
