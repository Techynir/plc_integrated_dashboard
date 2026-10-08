import { StrictMode, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes, useLocation, useParams, useSearchParams } from "react-router-dom";
import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "./api";
import { AuthProvider, useAuth, useAuthConfig, useMe } from "./auth";
import { live } from "./live";
import { Layout } from "./components/Layout";
import { Loading } from "./components/ui";
import { Login } from "./pages/Login";
import { LoggedOut } from "./pages/LoggedOut";
import { Overview } from "./pages/console/Overview";
import { Trends } from "./pages/console/Trends";
import { Alarms } from "./pages/console/Alarms";
import { Performance } from "./pages/console/Performance";
import { Quality } from "./pages/console/Quality";
import { Health } from "./pages/console/Health";
import { DataQuality } from "./pages/console/DataQuality";
import { Config } from "./pages/console/Config";
import { System } from "./pages/System";
import { RawData } from "./pages/RawData";
import { AdminDevices } from "./pages/admin/Devices";
import { AdminRules } from "./pages/admin/Rules";
import { AdminAudit, AdminUsers } from "./pages/admin/Users";
import { AdminLogs } from "./pages/admin/Logs";
import "@fontsource/barlow/400.css";
import "@fontsource/barlow/500.css";
import "@fontsource/barlow/600.css";
import "@fontsource/barlow-condensed/500.css";
import "@fontsource/barlow-condensed/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "./theme.css";
import { AlarmToasts } from "./components/AlarmToasts";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
      refetchOnWindowFocus: true,
    },
  },
});

// A 401 anywhere means the session expired: drop back to the login screen.
queryClient.getQueryCache().subscribe((event) => {
  const err = event.query.state.error;
  if (err instanceof ApiError && err.status === 401 && event.query.queryKey[0] !== "me") {
    queryClient.setQueryData(["me"], null);
  }
});

/** Old device links (/devices/:id) open that asset on the plant overview. */
function DeviceRedirect() {
  const { id } = useParams();
  return <Navigate to={`/?asset=${encodeURIComponent(id ?? "")}`} replace />;
}

/** Live view now sits on the plant overview. */
function LiveRedirect() {
  const [params] = useSearchParams();
  const asset = params.get("asset");
  return <Navigate to={asset ? `/?asset=${encodeURIComponent(asset)}` : "/"} replace />;
}

function AdminOnly({ children }: { children: JSX.Element }) {
  return useAuth().can("admin") ? children : <Navigate to="/" replace />;
}

function Authenticated() {
  const qc = useQueryClient();
  useEffect(() => {
    live.start();
    const off = live.onAlarm(() => {
      qc.invalidateQueries({ queryKey: ["alarms"] });
      qc.invalidateQueries({ queryKey: ["devices"] });
    });
    return () => {
      off();
      live.stop();
    };
  }, [qc]);

  return (
    <>
      <AlarmToasts />
    <Layout>
      <Routes>
        <Route path="/" element={<Overview />} />
        <Route path="/live" element={<LiveRedirect />} />
        <Route path="/trends" element={<Trends />} />
        <Route path="/alarms" element={<Alarms />} />
        <Route path="/performance" element={<Performance />} />
        <Route path="/quality" element={<Quality />} />
        <Route path="/health" element={<Health />} />
        <Route path="/data-quality" element={<AdminOnly><DataQuality /></AdminOnly>} />
        <Route path="/config" element={<AdminOnly><Config /></AdminOnly>} />
        <Route path="/devices/:id" element={<DeviceRedirect />} />
        <Route path="/raw" element={<AdminOnly><RawData /></AdminOnly>} />
        <Route path="/system" element={<AdminOnly><System /></AdminOnly>} />
        <Route path="/admin/devices" element={<AdminOnly><AdminDevices /></AdminOnly>} />
        <Route path="/admin/rules" element={<AdminOnly><AdminRules /></AdminOnly>} />
        <Route path="/admin/users" element={<AdminOnly><AdminUsers /></AdminOnly>} />
        <Route path="/admin/audit" element={<AdminOnly><AdminAudit /></AdminOnly>} />
        <Route path="/admin/logs" element={<AdminOnly><AdminLogs /></AdminOnly>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
    </>
  );
}

/** After sign-in: go back to where the user was heading (a dashboard path or the simulator). */
function AfterLogin() {
  const location = useLocation();
  const config = useAuthConfig();
  const next = new URLSearchParams(location.search).get("next") || "/";
  if (next.startsWith("/") && !next.startsWith("//")) return <Navigate to={next} replace />;
  if (config.isLoading) return <Loading />;
  const allowed = [config.data?.simulator_url].filter(Boolean).map((u) => new URL(u!).origin);
  try {
    if (allowed.includes(new URL(next).origin)) {
      window.location.replace(next);
      return <Loading />;
    }
  } catch {
    /* not a URL */
  }
  return <Navigate to="/" replace />; // never redirect to foreign sites
}

function App() {
  const me = useMe();
  const location = useLocation();
  if (me.isLoading) return <Loading />;
  if (location.pathname === "/logged-out") return me.data ? <Navigate to="/" replace /> : <LoggedOut />;
  if (!me.data) {
    if (location.pathname === "/login") return <Login />;
    const next = location.pathname + location.search;
    return <Navigate to={next === "/" ? "/login" : `/login?next=${encodeURIComponent(next)}`} replace />;
  }
  if (location.pathname === "/login") return <AfterLogin />;
  return (
    <AuthProvider user={me.data}>
      <Authenticated />
    </AuthProvider>
  );
}

// A page restored from the back/forward cache (e.g. "Back" after signing out) must re-check the session.
window.addEventListener("pageshow", (event) => {
  if (event.persisted) window.location.reload();
});

// Apply the saved theme before first paint.
try {
  const saved = localStorage.getItem("theme");
  if (saved === "light" || saved === "dark") document.documentElement.setAttribute("data-theme", saved);
} catch {
  /* storage unavailable */
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
