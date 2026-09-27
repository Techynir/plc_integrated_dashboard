import { FormEvent, useEffect, useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { api, qs } from "../../api";
import { ErrorText, Loading } from "../../components/ui";

interface LogEntry {
  ts: string;
  component: string;
  level: "error" | "warning" | "info";
  message: string;
  fields: Record<string, unknown> | null;
}

interface LogPage {
  entries: LogEntry[];
  next_page_token: string | null;
  components: string[];
}

const COMPONENTS = ["api", "ingestor", "mosquitto", "web", "simulator-ui", "db", "init", "certs"];
const COMPONENT_HELP: Record<string, string> = {
  api: "REST API and live stream",
  ingestor: "MQTT → database, alarms",
  mosquitto: "MQTT broker (connections, logins)",
  web: "HTTPS proxy and web app",
  "simulator-ui": "Web simulator",
  db: "PostgreSQL / TimescaleDB",
  init: "Deploy-time setup",
  certs: "Certificate generation",
};
const RANGES = ["15m", "1h", "6h", "24h", "7d", "30d"];
const LEVEL_STYLE = {
  error: { color: "var(--critical)", label: "Error" },
  warning: { color: "var(--serious)", label: "Warning" },
  info: { color: "var(--offline)", label: "Info" },
};

function time(iso: string): string {
  const d = new Date(iso);
  return (
    d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }) +
    "." +
    String(d.getMilliseconds()).padStart(3, "0")
  );
}

function Message({ entry }: { entry: LogEntry }) {
  const [open, setOpen] = useState(false);
  const f = entry.fields;
  const main = f ? String(f.message ?? f.msg ?? entry.message) : entry.message;
  const extra = f ? Object.entries(f).filter(([k]) => !["message", "msg", "time", "ts"].includes(k)) : [];
  return (
    <div>
      <code style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{main}</code>
      {extra.length > 0 && (
        <div className="row small muted" style={{ gap: 10, marginTop: 2 }}>
          {extra.slice(0, open ? extra.length : 4).map(([k, v]) => (
            <span key={k} className="mono">
              {k}=<span style={{ color: "var(--ink-2)" }}>{typeof v === "string" ? v : JSON.stringify(v)}</span>
            </span>
          ))}
          {(extra.length > 4 || String(f?.exception ?? "").length > 0) && (
            <button className="small ghost" onClick={() => setOpen((o) => !o)}>
              {open ? "less" : "more"}
            </button>
          )}
        </div>
      )}
      {open && typeof f?.exception === "string" && (
        <pre className="codeblock" style={{ marginTop: 6, whiteSpace: "pre-wrap" }}>{f.exception}</pre>
      )}
    </div>
  );
}

export function AdminLogs() {
  const [component, setComponent] = useState("");
  const [level, setLevel] = useState("all");
  const [range, setRange] = useState("1h");
  const [draft, setDraft] = useState("");
  const [q, setQ] = useState("");
  const [live, setLive] = useState(false);

  const logs = useInfiniteQuery({
    queryKey: ["logs", component, level, range, q],
    queryFn: ({ pageParam }) =>
      api<LogPage>(`/system/logs${qs({ component, level, range, q, page_token: pageParam as string | undefined, limit: 100 })}`),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_page_token ?? undefined,
    refetchInterval: live ? 10_000 : false,
    retry: false,
  });

  useEffect(() => {
    if (live) setRange((r) => (["15m", "1h"].includes(r) ? r : "15m"));
  }, [live]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setQ(draft.trim());
  };
  const entries = logs.data?.pages.flatMap((p) => p.entries) ?? [];

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Logs</h1>
          <div className="sub">Output of every component, from Google Cloud Logging (kept 30 days), newest first</div>
        </div>
        <button className={live ? "primary" : ""} onClick={() => setLive((l) => !l)} title="Refresh every 10 seconds">
          {live ? "Live ● pause" : "Live ▶"}
        </button>
      </div>

      <form className="row" style={{ marginBottom: 14, alignItems: "flex-end" }} onSubmit={submit}>
        <label className="field">
          Component
          <select value={component} onChange={(e) => setComponent(e.target.value)}>
            <option value="">All components</option>
            {COMPONENTS.map((c) => (
              <option key={c} value={c}>
                {c} — {COMPONENT_HELP[c]}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          Level
          <select value={level} onChange={(e) => setLevel(e.target.value)}>
            <option value="all">All</option>
            <option value="warning">Warnings and errors</option>
            <option value="error">Errors only</option>
          </select>
        </label>
        <label className="field">
          Period
          <div className="segmented">
            {RANGES.map((r) => (
              <button type="button" key={r} className={range === r ? "on" : ""} onClick={() => setRange(r)}>
                {r}
              </button>
            ))}
          </div>
        </label>
        <label className="field" style={{ flex: 1, minWidth: 220 }}>
          Search
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="text, or key=value (e.g. device_id=plc-01, logger=ingestor)"
          />
        </label>
        <button type="submit" className="primary">
          Search
        </button>
        {q && (
          <button type="button" onClick={() => { setDraft(""); setQ(""); }}>
            Clear
          </button>
        )}
      </form>

      <div className="card">
        {logs.isLoading && <Loading />}
        <ErrorText error={logs.error} />
        {logs.data && entries.length === 0 && <div className="empty">No log lines match these filters in this period.</div>}
        {entries.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Component</th>
                  <th>Level</th>
                  <th>Message</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e, i) => {
                  const lv = LEVEL_STYLE[e.level];
                  return (
                    <tr key={`${e.ts}-${i}`}>
                      <td className="small nowrap" style={{ verticalAlign: "top" }}>{time(e.ts)}</td>
                      <td className="small nowrap" style={{ verticalAlign: "top" }}>
                        <button className="small ghost mono" title="Show only this component" onClick={() => setComponent(e.component)}>
                          {e.component}
                        </button>
                      </td>
                      <td style={{ verticalAlign: "top" }}>
                        <span className="badge">
                          <span className="dot" style={{ background: lv.color }} />
                          {lv.label}
                        </span>
                      </td>
                      <td style={{ verticalAlign: "top" }}>
                        <Message entry={e} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {logs.hasNextPage && (
          <div className="row" style={{ padding: 12, justifyContent: "center" }}>
            <button onClick={() => logs.fetchNextPage()} disabled={logs.isFetchingNextPage}>
              {logs.isFetchingNextPage ? "Loading…" : "Load older"}
            </button>
          </div>
        )}
      </div>
    </>
  );
}
