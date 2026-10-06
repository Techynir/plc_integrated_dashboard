import { ReactNode, useEffect, useState } from "react";
import { NavLink, useLocation, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, Alarm } from "../api";
import { useAuth } from "../auth";
import { inMaintenance, mergeLive, useAsset } from "../hooks";
import { useLiveConnection, useLiveDevice } from "../live";
import { formatAge, secondsSince } from "../format";
import { ChangePasswordModal } from "./ChangePassword";
import { useNow } from "./ui";

type Theme = "light" | "dark";

function readTheme(): Theme {
  try {
    const saved = localStorage.getItem("theme");
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    /* storage unavailable */
  }
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyTheme(theme: Theme) {
  document.documentElement.setAttribute("data-theme", theme);
  try {
    localStorage.setItem("theme", theme);
  } catch {
    /* storage unavailable */
  }
}

export function useActiveAlarms() {
  return useQuery({
    queryKey: ["alarms", "active"],
    queryFn: () => api<Alarm[]>("/alarms?state=active"),
    refetchInterval: 30_000,
  });
}

const PLANT_TZ = "Asia/Kolkata";

function Clock() {
  const now = useNow(1000);
  const txt = new Date(now).toLocaleTimeString("en-GB", { timeZone: PLANT_TZ, hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return (
    <span className="clock" title="Plant time (IST)">
      {txt} IST
    </span>
  );
}

/** Data link state of the selected asset: live, no data (comms lost) or not yet commissioned. */
function LinkPill() {
  const { asset } = useAsset();
  const l = useLiveDevice(asset?.device_id);
  const now = useNow(1000);
  const ws = useLiveConnection();
  if (!asset) return null;
  const m = mergeLive(asset, l);
  const age = secondsSince(m.lastSeen, now);
  if (!m.lastSeen) {
    return (
      <span className="link-pill idle" title="This asset has never sent data">
        <span className="dot" />
        Awaiting PLC link
      </span>
    );
  }
  if (ws === "open" && !m.online && inMaintenance(asset.asset_config?.maintenance, now)) {
    return (
      <span className="link-pill idle" title="Weekly scheduled maintenance of the data system">
        <span className="dot" />
        Scheduled maintenance · until {asset.asset_config?.maintenance?.end}
      </span>
    );
  }
  if (!m.online || ws !== "open") {
    return (
      <span className="link-pill down" title={ws !== "open" ? "Live connection to the server lost" : "No data from the PLC gateway"}>
        <span className="dot" />
        {ws !== "open" ? "Dashboard offline" : `No data · last ${formatAge(age)}`}
      </span>
    );
  }
  return (
    <span className="link-pill" title={`Last record ${formatAge(age)}`}>
      <span className="dot" />
      Live · {formatAge(age)}
    </span>
  );
}

function AssetSelect() {
  const { asset, assets, select } = useAsset();
  if (!assets.length) return null;
  return (
    <label className="asset-select">
      <span className="sr-only" style={{ position: "absolute", left: -9999 }}>
        Asset
      </span>
      <select value={asset?.device_id ?? ""} onChange={(e) => select(e.target.value)} aria-label="Asset">
        {assets.map((d) => (
          <option key={d.device_id} value={d.device_id}>
            {d.name || d.device_id}
            {d.simulated ? " (simulated)" : ""}
          </option>
        ))}
      </select>
    </label>
  );
}

/** Sidebar section that can be collapsed; remembers its state and opens itself when one of its pages is active. */
function NavGroup({ id, title, paths, children }: { id: string; title: string; paths: string[]; children: ReactNode }) {
  const key = `plc-nav-${id}`;
  const location = useLocation();
  const activeInside = paths.some((p) => location.pathname === p || (p.endsWith("/") && location.pathname.startsWith(p)));
  const [open, setOpen] = useState(() => {
    try {
      return localStorage.getItem(key) !== "0";
    } catch {
      return true;
    }
  });
  useEffect(() => {
    if (activeInside) setOpen(true);
  }, [activeInside]);
  const toggle = () => {
    setOpen((o) => {
      try {
        localStorage.setItem(key, o ? "0" : "1");
      } catch {
        /* storage unavailable */
      }
      return !o;
    });
  };
  const bodyId = `nav-${id}-items`;
  return (
    <nav className="navgrp" aria-label={title}>
      <h4>
        <button type="button" className="navtoggle" aria-expanded={open} aria-controls={bodyId} onClick={toggle}>
          <span>{title}</span>
          <svg viewBox="0 0 12 12" aria-hidden="true">
            <path d="M3 4.5 6 7.5 9 4.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </h4>
      <div id={bodyId} className="navitems" hidden={!open}>
        {children}
      </div>
    </nav>
  );
}

export function Layout({ children }: { children: ReactNode }) {
  const { user, can, logout } = useAuth();
  const [params] = useSearchParams();
  const kiosk = params.get("kiosk") === "1";
  const [theme, setTheme] = useState<Theme>(readTheme);
  const [menuOpen, setMenuOpen] = useState(false);
  const [changingPassword, setChangingPassword] = useState(false);
  const location = useLocation();
  const alarms = useActiveAlarms();
  const links = useQuery({
    queryKey: ["links"],
    queryFn: () => api<{ simulator_url: string | null }>("/system/links"),
    staleTime: Infinity,
  });
  const active = alarms.data ?? [];
  const unacked = active.filter((a) => !a.acked_at);
  const critical = unacked.some((a) => a.severity === "critical");

  useEffect(() => applyTheme(theme), [theme]);
  useEffect(() => setMenuOpen(false), [location.pathname]);

  if (kiosk) {
    return (
      <div className="app kiosk">
        <main className="main">{children}</main>
      </div>
    );
  }

  // keep the selected asset when moving between screens
  const assetQs = params.get("asset") ? `?asset=${encodeURIComponent(params.get("asset")!)}` : "";
  const link = (to: string, label: string, extra?: ReactNode, keepAsset = true) => (
    <NavLink to={to + (keepAsset ? assetQs : "")} end={to === "/"} className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
      <span>{label}</span>
      {extra}
    </NavLink>
  );

  return (
    <div className="app">
      <header className="topbar">
        <button className="small mobile-bar" onClick={() => setMenuOpen((o) => !o)} aria-label="Menu" aria-expanded={menuOpen}>
          ☰
        </button>
        <NavLink to={"/" + assetQs} className="brand" aria-label="Numerique">
          <img className="logo" src="/Numerique2.png" alt="Numerique" />
        </NavLink>
        <AssetSelect />
        <LinkPill />
        <span className="spacer" />
        <Clock />
        <button
          className="small ghost"
          onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
          title={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
        >
          {theme === "dark" ? "☀" : "☾"}
        </button>
      </header>
      <aside className={`sidebar${menuOpen ? " open" : ""}`}>
        <nav className="navgrp" aria-label="Operations">
          <h4>Operations</h4>
          {link("/", "Plant overview")}
          {link("/trends", "Trends & historian")}
          {link(
            "/alarms",
            "Alarms & events",
            unacked.length > 0 ? (
              <span className={`navbadge${critical ? "" : " w"}`} title={`${unacked.length} unacknowledged`}>
                {unacked.length}
              </span>
            ) : null,
          )}
        </nav>
        <nav className="navgrp" aria-label="Analytics">
          <h4>Analytics</h4>
          {link("/performance", "Performance & downtime")}
          {link("/quality", "Process quality")}
          {link("/health", "Asset health")}
        </nav>
        {can("admin") && (
          <>
            <NavGroup id="platform" title="Platform" paths={["/data-quality", "/config", "/raw", "/system"]}>
              {link("/data-quality", "Data quality & link")}
              {link("/config", "Asset configuration")}
              {link("/raw", "Raw data", undefined, false)}
              {link("/system", "System health", undefined, false)}
            </NavGroup>
            <NavGroup id="admin" title="Admin" paths={["/admin/"]}>
              {link("/admin/devices", "Devices", undefined, false)}
              {link("/admin/rules", "Alarm rules", undefined, false)}
              {link("/admin/users", "Users", undefined, false)}
              {link("/admin/audit", "Audit log", undefined, false)}
              {link("/admin/logs", "Logs", undefined, false)}
              {links.data?.simulator_url && (
                <a className="nav-link" href={links.data.simulator_url} target="_blank" rel="noopener">
                  <span>Simulator</span>
                  <span aria-hidden="true">↗</span>
                </a>
              )}
            </NavGroup>
          </>
        )}
        <div className="sidebar-footer">
          <div>
            <div style={{ color: "var(--ink)", fontWeight: 600 }}>{user.name || user.email}</div>
            <div className="muted">{user.role}</div>
          </div>
          <div className="row">
            <button className="small" onClick={() => setChangingPassword(true)}>
              Change password
            </button>
            <button className="small" onClick={logout}>
              Sign out
            </button>
          </div>
          <NavLink to={`${location.pathname}?kiosk=1${assetQs ? "&" + assetQs.slice(1) : ""}`} className="small">
            Kiosk mode ↗
          </NavLink>
        </div>
      </aside>
      <main className="main">{children}</main>
      {changingPassword && <ChangePasswordModal onClose={() => setChangingPassword(false)} />}
    </div>
  );
}
