import { useMemo } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Alarm, api, qs, Tag } from "../../api";
import { Performance, useAnalytics, useDevice, useHistory, windowEnd } from "../../assetApi";
import { useAuth } from "../../auth";
import { mergeLive, roleTag, runningNow, useAsset } from "../../hooks";
import { useLiveDevice } from "../../live";
import { formatValue } from "../../format";
import { Chip, fmtDT, fmtDur, fmtNum, fmtT, limitText, normalText, Panel, RangeBar, ScreenHead, tagState } from "../../components/console";
import { Lane, Lanes, Sparkline } from "../../components/charts";
import { Loading, useNow } from "../../components/ui";

export const ROLE_ORDER = ["machine_status", "speed", "motor_current", "steam_pressure", "moisture", "vibration"];

export function orderTags(tags: Tag[]): Tag[] {
  const rank = (t: Tag) => (t.role ? ROLE_ORDER.indexOf(t.role) : 99);
  return tags.slice().sort((a, b) => rank(a) - rank(b) || Number(b.pinned) - Number(a.pinned) || a.tag.localeCompare(b.tag));
}

/** Lane definitions for numeric tags from a /history response. */
export function lanesFor(
  tags: Tag[],
  series: Record<string, [number, number | null, number | null, number | null][]> | undefined,
  current?: (t: Tag) => string,
  colorOf?: (t: Tag) => string | undefined,
): Lane[] {
  return tags.map((t) => ({
    color: colorOf?.(t),
    key: t.tag,
    label: t.display_name || t.tag,
    unit: t.unit,
    decimals: t.decimals,
    points: series?.[t.tag] ?? [],
    normal: t.min_value != null && t.max_value != null ? [t.min_value, t.max_value] : null,
    warn: t.warn_limit,
    crit: t.crit_limit,
    dir: t.limit_dir,
    step: !!t.value_labels || t.role === "machine_status",
    valueLabels: t.value_labels,
    sub: `${t.tag}${t.unit ? ` · ${t.unit}` : ""}`,
    current: current?.(t),
  }));
}

function RawRecord({ deviceId }: { deviceId: string }) {
  const { can } = useAuth();
  const raw = useQuery({
    queryKey: ["raw", deviceId, "latest"],
    queryFn: () => api<{ ts: string; topic: string; payload: string }[]>(`/raw-messages${qs({ device_id: deviceId, status: "ok", limit: 1 })}`),
    refetchInterval: 5000,
  });
  const r = raw.data?.[0];
  let pretty = r?.payload ?? "";
  try {
    pretty = JSON.stringify(JSON.parse(pretty), null, 2);
  } catch {
    /* show as received */
  }
  return (
    <Panel
      title="Latest stored record"
      sub={r ? `Exactly what the gateway sent · ${fmtDT(r.ts)}` : "none in the last 7 days"}
      actions={
        can("admin") && (
          <Link className="btn small" to={`/raw?device=${encodeURIComponent(deviceId)}`}>
            All raw data
          </Link>
        )
      }
    >
      {r ? <pre className="jsonbox" style={{ maxHeight: 260, margin: 0 }}>{pretty}</pre> : <div className="empty">No record received recently.</div>}
    </Panel>
  );
}

export function Live() {
  const { asset, loading } = useAsset();
  const { can } = useAuth();
  const id = asset?.device_id;
  const detail = useDevice(id);
  const l = useLiveDevice(id);
  const now = useNow(1000);
  const perf = useAnalytics<Performance>(id, "performance", 1, 30_000);
  const alarms = useQuery({
    queryKey: ["alarms", "active", id],
    queryFn: () => api<Alarm[]>(`/alarms${qs({ state: "active", device_id: id })}`),
    enabled: !!id,
    refetchInterval: 15_000,
  });

  const span = 15 * 60_000;
  const { end, anchored } = windowEnd(asset?.last_seen, span, Math.floor(now / 5000) * 5000);
  // machine status is shown only in the ribbon, not as a value card or lane
  const tags = useMemo(
    () => orderTags((detail.data?.tags ?? []).filter((t) => t.data_type === "number" && t.role !== "machine_status")),
    [detail.data],
  );
  const hist = useHistory(id, tags.map((t) => t.tag), end - span, end, anchored ? false : 5000);

  if (loading || (id && detail.isLoading)) return <Loading />;
  if (!asset || !detail.data) return <div className="panel empty">No asset selected.</div>;

  const d = detail.data;
  const m = mergeLive(d, l);
  const allTags = d.tags ?? [];
  const cardTags = orderTags(allTags.filter((t) => t.role !== "machine_status"));
  const conflict = !!perf.data?.state_source.startsWith("CONFLICT");
  const running = runningNow(d, allTags, m.value, m.status, conflict);
  const lastSeg = perf.data?.segments.filter((s) => s.state !== "comms").pop();
  const comms = !m.online;
  const activeAlarms = alarms.data ?? [];
  const hasCrit = activeAlarms.some((a) => a.severity === "critical");
  const speedTag = roleTag(allTags, "speed");
  const speedPts = (speedTag && hist.data?.series[speedTag.tag]) || [];
  const speedVals = speedPts.map((p) => p[1]).filter((v): v is number => typeof v === "number");
  const speedAvg = speedVals.length ? speedVals.reduce((a, b) => a + b, 0) / speedVals.length : null;
  const target = d.asset_config?.speed_target;

  const state = !m.lastSeen ? "awaiting" : comms ? "comms" : running === false ? "stopped" : "running";
  const stateText = { awaiting: "Awaiting PLC link", comms: "Communication lost", stopped: "Stopped", running: running ? "Running" : "Online" }[state];
  const since =
    lastSeg && !perf.data?.window.anchored
      ? lastSeg.start <= new Date(perf.data!.window.start).getTime() / 1000 + 1
        ? "over 1 h"
        : fmtDur(now / 1000 - lastSeg.start)
      : "—";

  return (
    <div className="screen">
      <ScreenHead
        eyebrow={`${d.asset_type || "Asset"} · ${d.name || d.device_id}`}
        title="Live view"
        desc="Values from the PLC as they arrive. Grey means normal. Colour appears only when something needs attention."
      />
      <div className={`banner${state === "stopped" ? " stopped" : state === "comms" || state === "awaiting" ? " comms" : hasCrit ? " alarm" : " running"}`} role="status" aria-live="polite">
        <div className="state">
          <i />
          {stateText}
        </div>
        <div className="bmeta">
          <div>
            {comms ? "Last good value" : "In this state for"}
            <b>{comms ? fmtDT(m.lastSeen) : since}</b>
          </div>
          <div>
            Speed target
            <b>{target ? `${fmtNum(target, 1)} ${speedTag?.unit ?? ""}` : "not set"}</b>
          </div>
          <div>
            Avg speed · 15 min
            <b>{speedAvg !== null ? `${fmtNum(speedAvg, 1)} ${speedTag?.unit ?? ""}` : "—"}</b>
          </div>
          <div>
            Active alarms
            <b>
              {activeAlarms.length}
              {hasCrit ? " · critical" : ""}
            </b>
          </div>
          <div>
            Last update
            <b>
              {fmtT(m.lastSeen)} · {comms ? "Communication Lost" : "Good"}
            </b>
          </div>
        </div>
      </div>
      {conflict && (
        <div className="alert warning">
          <Chip cls="warn">Check status register</Chip>
          <span>
            The status register says <b>Stopped</b> while the speed shows the machine running, so the state above is taken from speed. Confirm the status codes with
            the PLC programmer
            {can("admin") ? (
              <>
                {" "}
                (see <Link to={`/data-quality?asset=${encodeURIComponent(d.device_id)}`}>Data quality</Link>)
              </>
            ) : null}
            .
          </span>
        </div>
      )}
      {anchored && (
        <div className="alert comms">
          <Chip cls="comms">No recent data</Chip>
          <span>
            Showing the 15 minutes up to the last record, <b>{fmtDT(m.lastSeen)}</b>. Values below are the last known values.
          </span>
        </div>
      )}

      <div className="kpis">
        {cardTags.map((t) => {
          const v = m.value(t);
          const bad = m.quality(t) === 2;
          const st = tagState(t, v, { comms, bad });
          const cls = st.cls === "warn" ? "warn" : st.cls === "crit" ? "crit" : st.cls === "comms" ? "comms" : st.cls === "bad" ? "bad" : "";
          const spark = (hist.data?.series[t.tag] ?? []).map((p) => [p[0], p[1]] as [number, number | null]);
          return (
            <div key={t.tag} className={`kpi ${cls}`}>
              <div className="kh">
                <div>
                  <div className="kl">{t.display_name || t.tag}</div>
                  <div className="kt">{t.tag}</div>
                </div>
                <Chip cls={st.cls}>{st.label}</Chip>
              </div>
              <div className="kv2">
                {formatValue(v, t)}
                {t.unit && !t.value_labels ? <small>{t.unit}</small> : null}
              </div>
              {typeof v === "number" && <RangeBar tag={t} value={v} />}
              {t.data_type === "number" && spark.length > 1 && <Sparkline points={spark} range={t.min_value != null && t.max_value != null ? [t.min_value, t.max_value] : null} ariaLabel={`${t.display_name || t.tag}, last 15 minutes`} />}
              <div className="kf">
                <span>{normalText(t)}</span>
                <span>{limitText(t)}</span>
              </div>
            </div>
          );
        })}
      </div>

      <div className="grid g-7-5">
        <Panel title="Last 15 minutes" sub="Shaded band = normal range · dashed = warning / critical">
          {tags.length === 0 ? (
            <div className="empty">No numeric tags yet.</div>
          ) : (
            <Lanes
              lanes={lanesFor(tags, hist.data?.series, (t) => (comms ? "comms lost" : `${formatValue(m.value(t), t)} ${t.unit}`))}
              from={end - span}
              to={end}
              gapMs={Math.max(15, d.asset_config?.comms_timeout_s ?? 15) * 1000}
            />
          )}
        </Panel>
        <div className="grid" style={{ alignContent: "start" }}>
          <Panel
            title={`Active alarms · ${d.name || d.device_id}`}
            actions={
              <Link className="sub" to={`/alarms?asset=${encodeURIComponent(d.device_id)}`}>
                Open alarm list
              </Link>
            }
          >
            {activeAlarms.length ? (
              <div className="alarmlist">
                {activeAlarms.slice(0, 5).map((a) => (
                  <div className="check" key={a.id}>
                    <Chip cls={a.severity === "critical" ? "crit" : a.severity === "warning" ? "warn" : "info"}>
                      {a.severity === "critical" ? "Critical" : a.severity === "warning" ? "Warning" : "Info"}
                    </Chip>
                    <div>
                      <b>{a.message || a.rule_name}</b> <span className="muted mono">since {fmtT(a.raised_at)}</span>
                      {a.context && <div className="ctx">{a.context}</div>}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="muted" style={{ margin: 0 }}>
                No active alarms.
              </p>
            )}
          </Panel>
          <RawRecord deviceId={d.device_id} />
        </div>
      </div>
    </div>
  );
}
