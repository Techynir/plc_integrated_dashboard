import { ReactNode, useEffect, useState } from "react";
import type { Severity } from "../api";

/** Current time, re-rendering every `ms` (for "5s ago" style labels and staleness). */
export function useNow(ms = 1000): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}

const icons = {
  ok: <path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />,
  warn: (
    <>
      <path d="M8 2l6.5 11.5h-13z" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
      <path d="M8 6.5v3.2M8 11.6v.1" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </>
  ),
  off: <path d="M4 8h8" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />,
  info: (
    <>
      <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="1.6" />
      <path d="M8 7.2v3.8M8 4.9v.1" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </>
  ),
};

export function Icon({ name }: { name: keyof typeof icons }) {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      {icons[name]}
    </svg>
  );
}

interface StatusSpec {
  label: string;
  color: string;
  icon: keyof typeof icons;
}

export function deviceStatus(online: boolean | undefined, status: string | null | undefined, enabled = true): StatusSpec {
  if (!enabled) return { label: "Disabled", color: "var(--offline)", icon: "off" };
  if (!online) return { label: "Offline", color: "var(--offline)", icon: "off" };
  switch (status) {
    case "FAULT":
      return { label: "Fault", color: "var(--critical)", icon: "warn" };
    case "IDLE":
      return { label: "Idle", color: "var(--warning)", icon: "info" };
    case "MAINT":
      return { label: "Maintenance", color: "var(--warning)", icon: "info" };
    case "STOP":
      return { label: "Stopped", color: "var(--serious)", icon: "off" };
    default:
      return { label: status === "RUN" ? "Running" : "Online", color: "var(--good)", icon: "ok" };
  }
}

/** Status is never color-alone: dot + icon + label. */
export function StatusBadge({ online, status, enabled }: { online?: boolean; status?: string | null; enabled?: boolean }) {
  const s = deviceStatus(online, status, enabled);
  return (
    <span className="badge" title={s.label}>
      <span className="dot" style={{ background: s.color }} />
      <Icon name={s.icon} />
      {s.label}
    </span>
  );
}

/** Marks devices fed by the web simulator, so they are never mistaken for real PLCs. */
export function SimulatedBadge() {
  return (
    <span className="badge" title="Data from the web simulator, not a real PLC" style={{ borderStyle: "dashed" }}>
      <span className="dot" style={{ background: "var(--serious)" }} />
      Simulated
    </span>
  );
}

export function SeverityBadge({ severity }: { severity: Severity }) {
  const color = { critical: "var(--critical)", warning: "var(--serious)", info: "var(--accent)" }[severity];
  return (
    <span className={`badge sev-${severity}`}>
      <span className="dot" style={{ background: color }} />
      <Icon name={severity === "info" ? "info" : "warn"} />
      {severity[0].toUpperCase() + severity.slice(1)}
    </span>
  );
}

export function StatTile({ label, value, unit, foot }: { label: string; value: ReactNode; unit?: string; foot?: ReactNode }) {
  return (
    <div className="card tile">
      <div className="tile-label">{label}</div>
      <div className="tile-value">
        {value}
        {unit ? <span className="unit">{unit}</span> : null}
      </div>
      {foot ? <div className="tile-foot">{foot}</div> : null}
    </div>
  );
}

export function Modal({
  title,
  onClose,
  children,
  footer,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <div className="card-header">
          <h2>{title}</h2>
          <button className="ghost small" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <div className="modal-body">{children}</div>
        {footer ? <div className="modal-footer">{footer}</div> : null}
      </div>
    </div>
  );
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="small"
      onClick={async () => {
        await navigator.clipboard?.writeText(text);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }}
    >
      {copied ? "Copied" : label}
    </button>
  );
}

export function ErrorText({ error }: { error: unknown }) {
  if (!error) return null;
  return <div className="error-text">{error instanceof Error ? error.message : String(error)}</div>;
}

export function Loading() {
  return <div className="empty">Loading…</div>;
}
