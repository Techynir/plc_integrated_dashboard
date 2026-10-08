/** Types and queries for /assets/{id}/… (the analytics behind the console screens). */
import { useQuery } from "@tanstack/react-query";
import { api, Device, History, qs } from "./api";
import { FindingSpan, Insight } from "./components/rules";

export interface Win {
  start: string;
  end: string;
  anchored: boolean;
}

export interface PublicTag {
  tag: string;
  label: string;
  unit: string;
  decimals: number;
  min_value: number | null;
  max_value: number | null;
  limit_dir: "high" | "low" | null;
  warn_limit: number | null;
  crit_limit: number | null;
  value_labels: Record<string, string> | null;
}

export interface Totals {
  run: number;
  stop: number;
  comms: number;
}

export interface Summary {
  window: Win;
  state_source: string;
  totals: Totals;
  availability: number | null;
  stops: number;
  completeness: number | null;
  records: number;
  health: { score: number | null; state: "good" | "watch" | "act" | "nodata" };
  alarms: { active: number; unacked: number; critical: number };
  roles: Record<string, PublicTag | null>;
}

export interface StopEvent {
  start: number;
  end: number | null;
  seconds: number;
  reason: string;
  shift: string;
}

export interface Performance {
  window: Win;
  state_source: string;
  totals: Totals;
  availability: number | null;
  stops: StopEvent[];
  longest_stop_s: number | null;
  avg_speed_running: number | null;
  speed_target: number | null;
  /** Speed while running ÷ rated speed (above 1 when the crew runs faster than rated). */
  performance: number | null;
  /** availability × performance */
  overall: number | null;
  /** lowest and highest speed ÷ rated while running in the window */
  performance_range: { min: number; max: number } | null;
  rated_actual: { tag: string; label: string; unit: string; actual: number; rated: number; ratio: number }[];
  speed_tag: PublicTag | null;
  segments: { state: "run" | "stop" | "comms"; start: number; end: number }[];
  shifts: {
    shift: string;
    hours: string;
    run_s: number;
    stop_s: number;
    comms_s: number;
    availability: number | null;
    stops: number;
    avg_speed: number | null;
    performance: number | null;
    moisture_in_range: number | null;
    alarms: number;
  }[];
  reasons: string[];
  timezone: string;
}

export interface QualityData {
  window: Win;
  moisture_tag: PublicTag | null;
  steam_tag: PublicTag | null;
  missing?: string;
  minute_means?: { t: number; v: number; flag: number }[];
  limits?: { cl: number; sigma: number; ucl: number; lcl: number; no_variation?: boolean } | null;
  spec?: { lsl: number | null; usl: number | null };
  capability?: { mean: number | null; sd: number | null; cp: number | null; cpk: number | null; in_spec: number | null; n: number };
  out_of_control?: number;
  run_rule?: number;
  histogram?: { lo: number; hi: number; count: number }[];
  scatter?: { t: number; x: number; y: number }[];
  regression?: { m: number; b: number; r: number; rmse: number; n: number } | null;
  moisture_model?: { a: number; b: number } | null;
  predicted?: { t: number; v: number }[];
  steam_limit?: number | null;
  warn_limit?: number | null;
  findings?: FindingSpan[];
  insights?: { control: Insight[]; steam: Insight[] };
}

export interface HealthData {
  anchored: boolean;
  end: string;
  overall: number | null;
  state: "good" | "watch" | "act" | "nodata";
  components: { bearing: number | null; drive: number | null; dryer: number | null };
  inputs: { vibration: number | null; steam_pressure: number | null; current: number | null; speed: number | null; residual: number | null; sigma: number };
  model: { m: number; b: number; r: number; rmse: number; n: number } | null;
  tags: Record<"vibration" | "motor_current" | "speed" | "steam_pressure", PublicTag | null>;
  daily_vibration: { day: string; offset: number; mean: number | null; minutes: number }[];
  projection: { m: number; b: number; per_month: number; days_to_warning: number | null } | null;
  short_term: { slope_per_min: number | null; current: number | null; minutes_to_warning: number | null } | null;
  load_signature: { t: number; x: number; y: number; residual: number | null }[];
  vibration_vs_speed: { t: number; x: number; y: number; above: boolean }[];
  vibration_regression: { m: number; b: number; rmse: number } | null;
  rule_model: { m: number; b: number; limit: number } | null;
  vibration_usual: { usual: number; ref_speed: number; step: number | null } | null;
  insights: { trend: Insight[]; load: Insight[]; vibration: Insight[] };
}

export interface CheckItem {
  id: number;
  title: string;
  detail: string;
  status: "open" | "assumed" | "done";
  updated_by: string | null;
  updated_at: string;
}

export interface DataQuality {
  window: Win;
  interval_s: number;
  expected_per_tag: number;
  poll_success_1h: number | null;
  comms_lost_s: number;
  comms_events: { start: number; end: number | null; seconds: number; missing_records: number; planned?: boolean }[];
  maintenance?: { weekday: string; start: string; end: string; tz: string } | null;
  bad_reads: number;
  completeness: number | null;
  update_interval: { p50_ms: number | null; p95_ms: number | null; series: [number, number][]; burst: { period_s: number; messages: number } | null };
  comms_timeout_s: number;
  per_tag: { tag: string; label: string; good: number; bad: number; missing: number; completeness: number | null }[];
  registers: { address: number; tag: string; data_type: string; value_num: number | null; value_text: string | null; quality: number | null; ts: string | null }[];
  byte_order: "ABCD" | "CDAB" | "BADC" | "DCBA";
  checklist: CheckItem[];
}

export interface TagStats {
  tag: string;
  label: string;
  unit: string;
  decimals: number;
  running_only: boolean;
  min: number | null;
  avg: number | null;
  max: number | null;
  sd: number | null;
  in_normal: number | null;
  valid: number | null;
  samples: number;
}

const enc = encodeURIComponent;

export function useDevice(id: string | undefined, refetchMs: number | false = 15_000) {
  return useQuery({
    queryKey: ["device", id],
    queryFn: () => api<Device>(`/devices/${enc(id!)}`),
    enabled: !!id,
    refetchInterval: refetchMs,
  });
}

export function useSummary(id: string | undefined, hours = 24) {
  return useQuery({
    queryKey: ["asset", id, "summary", hours],
    queryFn: () => api<Summary>(`/assets/${enc(id!)}/summary${qs({ hours })}`),
    enabled: !!id,
    refetchInterval: 60_000,
  });
}

export function useAnalytics<T>(id: string | undefined, what: string, hours: number | null, refetchMs = 60_000) {
  return useQuery({
    queryKey: ["asset", id, what, hours],
    queryFn: () => api<T>(`/assets/${enc(id!)}/${what}${qs({ hours })}`),
    enabled: !!id,
    refetchInterval: refetchMs,
    placeholderData: (prev) => prev,
  });
}

/** Tag history for [from, to] (ms). */
export function useHistory(id: string | undefined, tags: string[], from: number, to: number, refetchMs: number | false) {
  return useQuery({
    queryKey: ["history", id, tags.join(","), Math.round(from / 5000), Math.round(to / 5000)],
    queryFn: () =>
      api<History>(`/devices/${enc(id!)}/history${qs({ tags: tags.join(","), from: new Date(from).toISOString(), to: new Date(to).toISOString() })}`),
    enabled: !!id && tags.length > 0,
    refetchInterval: refetchMs,
    placeholderData: (prev) => prev,
  });
}

/** Window end for a live screen: now, or the asset's last data when it has been silent longer than the window. */
export function windowEnd(lastSeen: string | null | undefined, spanMs: number, now: number): { end: number; anchored: boolean } {
  const last = lastSeen ? new Date(lastSeen).getTime() : null;
  if (last !== null && now - last > spanMs) return { end: last + 1000, anchored: true };
  return { end: now, anchored: false };
}
