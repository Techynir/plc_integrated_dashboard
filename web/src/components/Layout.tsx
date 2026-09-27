import { ReactNode, useEffect, useState } from "react";
import { NavLink, useLocation, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, Alarm } from "../api";
import { useAuth } from "../auth";
import { useLiveConnection } from "../live";
import { ChangePasswordModal } from "./ChangePassword";

type Theme = "system" | "light" | "dark";

function readTheme(): Theme {
  try {
    return (localStorage.getItem("theme") as Theme) || "system";
  } catch {
    return "system";
  }
}

function applyTheme(theme: Theme) {
  if (theme === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", theme);
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

export function Layout({ children }: { children: ReactNode }) {
  const { user, can, logout } = useAuth();
  const [params] = useSearchParams();
  const kiosk = params.get("kiosk") === "1";
  const [theme, setTheme] = useState<Theme>(readTheme);
  const [menuOpen, setMenuOpen] = useState(false);
  const [changingPassword, setChangingPassword] = useState(false);
  const location = useLocation();
  const conn = useLiveConnection();
  const alarms = useActiveAlarms();
  const links = useQuery({
    queryKey: ["links"],
    queryFn: () => api<{ simulator_url: string | null }>("/system/links"),
    staleTime: Infinity,
  });
  const active = alarms.data?.length ?? 0;

  useEffect(() => applyTheme(theme), [theme]);
  useEffect(() => setMenuOpen(false), [location.pathname]);

  if (kiosk) {
    return (
      <div className="app kiosk">
        <main className="main">{children}</main>
      </div>
    );
  }

  const link = (to: string, label: string, extra?: ReactNode) => (
    <NavLink to={to} end={to === "/"} className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
      <span>{label}</span>
      {extra}
    </NavLink>
  );

  return (
    <div className="app">
      <div className="mobile-bar">
        <button className="small" onClick={() => setMenuOpen((o) => !o)} aria-label="Menu">
          ☰
        </button>
        <strong>PLC Dashboard</strong>
      </div>
      <aside className={`sidebar${menuOpen ? " open" : ""}`}>
        <div className="brand">
          <img src="/favicon.svg" alt="" />
          PLC Dashboard
        </div>
        {link("/", "Overview")}
        {link(
          "/alarms",
          "Alarms",
          active > 0 ? (
            <span className="badge sev-critical" style={{ padding: "0 7px" }}>
              {active}
            </span>
          ) : null,
        )}
        {link("/raw", "Raw data")}
        {link("/system", "System health")}
        {can("admin") && links.data?.simulator_url && (
          <a className="nav-link" href={links.data.simulator_url} target="_blank" rel="noopener">
            <span>Simulator</span>
            <span aria-hidden="true">↗</span>
          </a>
        )}
        {can("admin") && (
          <>
            <div className="nav-section">Admin</div>
            {link("/admin/devices", "Devices")}
            {link("/admin/rules", "Alarm rules")}
            {link("/admin/users", "Users")}
            {link("/admin/audit", "Audit log")}
            {link("/admin/logs", "Logs")}
          </>
        )}
        <div className="sidebar-footer">
          <div title="Live data connection">
            <span
              className="conn-dot"
              style={{ background: conn === "open" ? "var(--good)" : conn === "connecting" ? "var(--warning)" : "var(--offline)" }}
            />
            Live: {conn === "open" ? "connected" : conn === "connecting" ? "connecting…" : "disconnected"}
          </div>
          <div>
            <div style={{ color: "var(--ink)", fontWeight: 500 }}>{user.name || user.email}</div>
            <div className="muted">{user.role}</div>
          </div>
          <div className="row">
            <select value={theme} onChange={(e) => setTheme(e.target.value as Theme)} aria-label="Theme">
              <option value="system">System theme</option>
              <option value="light">Light</option>
              <option value="dark">Dark</option>
            </select>
            <button className="small" onClick={logout}>
              Sign out
            </button>
          </div>
          <button className="small ghost" style={{ justifyContent: "flex-start", padding: "2px 0" }} onClick={() => setChangingPassword(true)}>
            Change password
          </button>
          <NavLink to="/?kiosk=1" className="small">
            Kiosk mode ↗
          </NavLink>
        </div>
      </aside>
      <main className="main">{children}</main>
      {changingPassword && <ChangePasswordModal onClose={() => setChangingPassword(false)} />}
    </div>
  );
}
