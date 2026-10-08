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
import { MenuIcon, MoonIcon, SunIcon } from "./icons";
import { MobileNav, NavSection, TopNav, UserMenu } from "./TopNav";

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
  const badge = unacked.length > 0 ? (
    <span className={`navbadge${critical ? "" : " w"}`} title={`${unacked.length} unacknowledged`}>
      {unacked.length}
    </span>
  ) : null;
  const sections: NavSection[] = [
    {
      id: "operations",
      title: "Operations",
      badge,
      items: [
        { to: "/", label: "Plant overview", desc: "Live state, key figures and every signal at a glance" },
        { to: "/trends", label: "Trends & historian", desc: "Any signal over time, with statistics and export" },
        { to: "/alarms", label: "Alarms & events", desc: "Active alarms, acknowledgement and history", badge },
      ],
    },
    {
      id: "analytics",
      title: "Analytics",
      items: [
        { to: "/performance", label: "Performance & downtime", desc: "Availability, performance, stops and shifts" },
        { to: "/quality", label: "Process quality", desc: "Paper moisture control chart and capability" },
        { to: "/health", label: "Asset health", desc: "Bearing, drive and dryer condition" },
      ],
    },
    ...(can("admin")
      ? [
          {
            id: "platform",
            title: "Platform",
            items: [
              { to: "/data-quality", label: "Data quality & link", desc: "Completeness, update rate and outages" },
              { to: "/config", label: "Asset configuration", desc: "Connection, tags, limits and roles" },
              { to: "/raw", label: "Raw data", desc: "Messages exactly as the gateway sent them", keepAsset: false },
              { to: "/system", label: "System health", desc: "Pipeline, database and broker", keepAsset: false },
            ],
          },
          {
            id: "admin",
            title: "Admin",
            items: [
              { to: "/admin/devices", label: "Devices", desc: "PLCs, their logins and register maps", keepAsset: false },
              { to: "/admin/rules", label: "Alarm rules", desc: "Limit rules and the built-in process rules", keepAsset: false },
              { to: "/admin/users", label: "Users", desc: "Accounts, roles and passwords", keepAsset: false },
              { to: "/admin/audit", label: "Audit log", desc: "Who changed what, and when", keepAsset: false },
              { to: "/admin/logs", label: "Logs", desc: "Service logs", keepAsset: false },
              ...(links.data?.simulator_url
                ? [{ to: links.data.simulator_url, label: "Simulator", desc: "Web simulator for test devices", external: true }]
                : []),
            ],
          },
        ]
      : []),
  ];

  return (
    <div className="app">
      <header className="appbar">
        <div className="topbar">
          <button className="small mobile-bar" onClick={() => setMenuOpen((o) => !o)} aria-label="Menu" aria-expanded={menuOpen}>
            <MenuIcon />
          </button>
          <NavLink to={"/" + assetQs} className="brand" aria-label="Numerique">
            <img className="logo" src="/Numerique2.png" alt="Numerique" />
          </NavLink>
          <AssetSelect />
          <LinkPill />
          <span className="spacer" />
          <Clock />
          <button
            className="small ghost iconbtn"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
            title={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
          >
            {theme === "dark" ? <SunIcon /> : <MoonIcon />}
          </button>
          <UserMenu
            name={user.name}
            login={user.email}
            role={user.role}
            kioskTo={`${location.pathname}?kiosk=1${assetQs ? "&" + assetQs.slice(1) : ""}`}
            onChangePassword={() => setChangingPassword(true)}
            onSignOut={logout}
          />
        </div>
        <TopNav sections={sections} assetQs={assetQs} />
      </header>
      {menuOpen && <MobileNav sections={sections} assetQs={assetQs} onNavigate={() => setMenuOpen(false)} />}
      <main className="main">{children}</main>
      {changingPassword && <ChangePasswordModal onClose={() => setChangingPassword(false)} />}
    </div>
  );
}
