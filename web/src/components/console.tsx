/** Shared widgets for the asset console screens. */
import { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { Tag } from "../api";
import { useAuth } from "../auth";

// ---------------------------------------------------------------- formatting

export function fmtNum(v: number | null | undefined, decimals = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

export function fmtPct(v: number | null | undefined, decimals = 1): string {
  return v === null || v === undefined ? "—" : `${(v * 100).toFixed(decimals)}`;
}

export function fmtDur(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${String(s % 60).padStart(2, "0")} s`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} h ${String(m % 60).padStart(2, "0")} min`;
  return `${Math.floor(h / 24)} d ${h % 24} h`;
}

const toDate = (t: string | number) => new Date(typeof t === "number" && t < 1e12 ? t * 1000 : t);

export function fmtT(t: string | number | null | undefined, seconds = true): string {
  if (t === null || t === undefined) return "—";
  return toDate(t).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}) });
}

export function fmtDT(t: string | number | null | undefined): string {
  if (t === null || t === undefined) return "—";
  return toDate(t).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// ---------------------------------------------------------------- tag state

export type StateCls = "ok" | "dev" | "warn" | "crit" | "comms" | "bad" | "run" | "info";

/** Grey/normal unless the value needs attention; colour always comes with a word. */
export function tagState(tag: Tag, value: unknown, opts: { comms?: boolean; bad?: boolean } = {}): { cls: StateCls; label: string } {
  if (opts.comms) return { cls: "comms", label: "Comms lost" };
  if (opts.bad) return { cls: "bad", label: "Bad quality" };
  if (typeof value !== "number") return { cls: "ok", label: value === null || value === undefined ? "No data" : "Normal" };
  const { limit_dir: d, warn_limit: w, crit_limit: c, min_value: lo, max_value: hi } = tag;
  if (d === "high") {
    if (c !== null && c !== undefined && value > c) return { cls: "crit", label: "Critical" };
    if (w !== null && w !== undefined && value > w) return { cls: "warn", label: "Warning" };
  }
  if (d === "low") {
    if (c !== null && c !== undefined && value < c) return { cls: "crit", label: "Critical" };
    if (w !== null && w !== undefined && value < w) return { cls: "warn", label: "Warning" };
  }
  if (lo !== null && lo !== undefined && hi !== null && hi !== undefined && (value < lo || value > hi)) {
    return { cls: "dev", label: "Off target" };
  }
  return { cls: "ok", label: "Normal" };
}

// ---------------------------------------------------------------- small components

const ICONS: Partial<Record<StateCls, ReactNode>> = {
  ok: <circle cx="6" cy="6" r="4.3" fill="none" stroke="currentColor" strokeWidth="1.5" />,
  warn: (
    <>
      <path d="M6 1 11.5 11H.5Z" fill="currentColor" />
      <rect x="5.3" y="4.4" width="1.4" height="3.6" fill="var(--surface)" />
      <rect x="5.3" y="8.8" width="1.4" height="1.3" fill="var(--surface)" />
    </>
  ),
  crit: (
    <>
      <path d="M3.8.8h4.4l3 3v4.4l-3 3H3.8l-3-3V3.8Z" fill="currentColor" />
      <rect x="5.3" y="2.8" width="1.4" height="4.4" fill="var(--surface)" />
      <rect x="5.3" y="8.2" width="1.4" height="1.4" fill="var(--surface)" />
    </>
  ),
  comms: (
    <>
      <path d="M1 6h3M8 6h3" stroke="currentColor" strokeWidth="1.6" fill="none" />
      <path d="M5 3.5 7 8.5" stroke="currentColor" strokeWidth="1.6" />
    </>
  ),
  bad: <rect x="1.5" y="1.5" width="9" height="9" fill="none" stroke="currentColor" strokeWidth="1.4" strokeDasharray="2 1.5" />,
};

export function Chip({ cls, children, title }: { cls: StateCls; children: ReactNode; title?: string }) {
  const icon = ICONS[cls];
  return (
    <span className={`chip ${cls}`} title={title}>
      {icon && (
        <svg viewBox="0 0 12 12" aria-hidden="true">
          {icon}
        </svg>
      )}
      {children}
    </span>
  );
}

export function Tile({ k, v, unit, s, cls }: { k: string; v: ReactNode; unit?: string; s?: ReactNode; cls?: string }) {
  return (
    <div className={`tile ${cls ?? ""}`}>
      <span className="k">{k}</span>
      <span className="v">
        {v}
        {unit ? <small>{unit}</small> : null}
      </span>
      {s ? <span className="s">{s}</span> : null}
    </div>
  );
}

export function Panel({ title, sub, actions, children, style }: { title: ReactNode; sub?: ReactNode; actions?: ReactNode; children: ReactNode; style?: React.CSSProperties }) {
  return (
    <section className="panel" style={style}>
      <div className="phead">
        <h2>{title}</h2>
        <div className="row">
          {sub ? <span className="sub">{sub}</span> : null}
          {actions}
        </div>
      </div>
      {children}
    </section>
  );
}

export function ScreenHead({ eyebrow, title, desc, actions }: { eyebrow: ReactNode; title: string; desc?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="shead">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1>{title}</h1>
        {desc ? <p>{desc}</p> : null}
      </div>
      {actions}
    </div>
  );
}

/** Explains when a window was moved back to the device's last data, or the state source conflicts. */
export function WindowNotes({ window, stateSource }: { window?: { end: string; anchored: boolean }; stateSource?: string }) {
  const { can } = useAuth();
  return (
    <>
      {window?.anchored && (
        <div className="alert comms">
          <Chip cls="comms">No recent data</Chip>
          <span>
            The PLC has not sent data recently, so this screen shows the period up to its last data, <b>{fmtDT(window.end)}</b>.
          </span>
        </div>
      )}
      {stateSource?.startsWith("CONFLICT") && (
        <div className="alert warning">
          <Chip cls="warn">Check status register</Chip>
          <span>
            {stateSource.replace("CONFLICT: ", "")}. Confirm the status codes with the PLC programmer
            {can("admin") ? (
              <>
                , or choose the running source in <Link to="/config">Asset configuration</Link>
              </>
            ) : null}
            .
          </span>
        </div>
      )}
    </>
  );
}

export function MissingRole({ what }: { what: string }) {
  const { can } = useAuth();
  return (
    <div className="panel empty">
      {what} {can("admin") ? <Link to="/config">Assign roles in Asset configuration</Link> : "An admin can assign roles in Asset configuration"}.
    </div>
  );
}

/** Horizontal bar with critical / warning / normal zones and a marker at the current value. */
export function RangeBar({ tag, value }: { tag: Tag; value: number | null }) {
  const { min_value: lo, max_value: hi, warn_limit: w, crit_limit: c, limit_dir: d } = tag;
  const pts = [lo, hi, w, c].filter((x): x is number => typeof x === "number");
  if (pts.length < 2) return null;
  let a = Math.min(...pts);
  let b = Math.max(...pts);
  const pad = (b - a) * 0.15 || 1;
  a -= pad;
  b += pad;
  const pct = (v: number) => Math.min(100, Math.max(0, ((v - a) / (b - a)) * 100));
  return (
    <div aria-hidden="true">
      <div className="rbar">
        {d === "low" && typeof c === "number" && <i className="z zc" style={{ left: 0, width: `${pct(c)}%` }} />}
        {d === "low" && typeof c === "number" && typeof w === "number" && (
          <i className="z zw" style={{ left: `${pct(c)}%`, width: `${pct(w) - pct(c)}%` }} />
        )}
        {d === "high" && typeof w === "number" && typeof c === "number" && (
          <i className="z zw" style={{ left: `${pct(w)}%`, width: `${pct(c) - pct(w)}%` }} />
        )}
        {d === "high" && typeof c === "number" && <i className="z zc" style={{ left: `${pct(c)}%`, right: 0 }} />}
        {typeof lo === "number" && typeof hi === "number" && (
          <i className="z zn" style={{ left: `${pct(lo)}%`, width: `${pct(hi) - pct(lo)}%` }} />
        )}
      </div>
      <div className="rmark">{value !== null && <b style={{ left: `${pct(value)}%` }} />}</div>
    </div>
  );
}

export function limitText(tag: Tag): string {
  const { limit_dir: d, warn_limit: w, crit_limit: c } = tag;
  if (!d || (w == null && c == null)) return "no limits";
  const s = d === "high" ? ">" : "<";
  return [w != null ? `W ${s}${w}` : null, c != null ? `C ${s}${c}` : null].filter(Boolean).join(" · ");
}

export function normalText(tag: Tag): string {
  return tag.min_value != null && tag.max_value != null ? `Normal ${tag.min_value}–${tag.max_value}` : "no normal range";
}

/** Read a CSS custom property (charts need concrete colours). */
export function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
