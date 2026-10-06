export type Role = "viewer" | "operator" | "admin";
/** What a tag means to the analytics screens (Asset configuration). */
export type Role_ = "machine_status" | "speed" | "motor_current" | "steam_pressure" | "moisture" | "vibration";
export type DataType = "number" | "boolean" | "string";
export type Quality = "GOOD" | "UNCERTAIN" | "BAD";
export type Severity = "critical" | "warning" | "info";
export type TagValue = number | boolean | string | null;

export interface User {
  id: number;
  email: string;
  name: string;
  role: Role;
  disabled?: boolean;
  created_at?: string;
  last_login_at?: string | null;
}

export interface Tag {
  tag: string;
  display_name: string;
  unit: string;
  data_type: DataType;
  value_scale: number;
  value_offset: number;
  min_value: number | null;
  max_value: number | null;
  decimals: number;
  pinned: boolean;
  configured: boolean;
  value_labels?: Record<string, string> | null;
  valid_min?: number | null;
  valid_max?: number | null;
  limit_dir?: "high" | "low" | null;
  warn_limit?: number | null;
  crit_limit?: number | null;
  role?: Role_ | null;
  suppress_when_stopped?: boolean;
  guidance?: string;
  value: TagValue;
  quality: Quality | null;
  ts: string | null;
}

export interface Connection {
  host: string;
  port: number;
  tls: boolean;
  ca_certificate_url: string;
  username: string;
  client_id: string;
  telemetry_topic: string;
  status_topic: string;
  qos: number;
  last_will: { topic: string; payload: string; qos: number; retain: boolean };
}

export interface AssetConfig {
  protocol?: string;
  gateway?: string;
  plc_ip?: string;
  port?: number | string;
  unit_id?: number | string;
  function_code?: string;
  block_read?: string;
  poll_interval_ms?: number;
  timeout?: string;
  timestamp_source?: string;
  on_read_failure?: string;
  byte_order?: "ABCD" | "CDAB" | "BADC" | "DCBA";
  running_source?: "auto" | "status" | "speed";
  running_speed_min?: number | null;
  comms_timeout_s?: number | null;
  speed_target?: number | null;
  /** weekly scheduled maintenance of the data system: no data expected, no offline alarm */
  maintenance?: { weekday: string; start: string; end: string; tz: string } | null;
  rules?: unknown;
}

export interface Device {
  device_id: string;
  name: string;
  site: string;
  line: string;
  description: string;
  expected_interval_s: number;
  enabled: boolean;
  has_credentials: boolean;
  online: boolean;
  status: string | null;
  last_seen: string | null;
  last_seq: number | null;
  seq_gaps: number;
  msg_count: number;
  created_at: string;
  simulated: boolean;
  asset_type?: string;
  asset_config?: AssetConfig;
  active_alarms: number;
  top_severity: Severity | null;
  tag_count?: number;
  preview_tags?: Tag[];
  tags?: Tag[];
  connection?: Connection;
}

export interface Alarm {
  id: number;
  rule_id: number | null;
  rule_name: string;
  device_id: string;
  tag: string | null;
  severity: Severity;
  message: string;
  trigger_value: number | null;
  raised_at: string;
  cleared_at: string | null;
  acked_at: string | null;
  acked_by: string | null;
  ack_comment: string | null;
  context?: string;
  explain?: { what: string; why: string; next: string } | null;
  active: boolean;
}

export type RuleType = "high" | "low" | "equals" | "fault" | "offline" | "stopped" | "process";

export interface AlarmRule {
  id: number;
  name: string;
  device_id: string | null;
  tag: string | null;
  rule_type: RuleType;
  threshold: number | null;
  deadband: number;
  severity: Severity;
  message: string;
  webhook_url: string | null;
  enabled: boolean;
  managed_by?: string | null;
}

export interface Credentials {
  credentials: { username: string; password: string };
  connection: Connection;
}

export interface History {
  resolution: "raw" | "1m" | "1h";
  bucket_s: number;
  from: string;
  to: string;
  series: Record<string, [number, number | null, number | null, number | null][]>;
}

export interface IngestorStats {
  type: "stats";
  ts: string;
  uptime_s: number;
  received_total: number;
  invalid_total: number;
  duplicates_total: number;
  rows_written_total: number;
  write_errors_total: number;
  dropped_rows_total: number;
  msg_rate: number;
  row_rate: number;
  buffered_rows: number;
  last_flush_ms: number;
  devices_online: number;
  devices_total: number;
}

export interface SystemStats {
  devices_total: number;
  devices_online: number;
  alarms_active: number;
  ingest_errors_1h: number;
  db_size_bytes: number;
  telemetry_rows: number;
  ingestor: IngestorStats | null;
  broker: { connected: boolean; clients_connected?: string; msgs_received_1min?: string; uptime?: string };
  websocket_clients: number;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

function errorMessage(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d) => {
          const loc = Array.isArray(d.loc) ? d.loc.filter((p: unknown) => p !== "body").join(".") : "";
          return loc ? `${loc}: ${d.msg}` : d.msg;
        })
        .join("; ");
    }
  }
  return fallback;
}

export async function api<T>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const res = await fetch(`/api/v1${path}`, {
    method: init.method ?? "GET",
    headers: init.body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
    credentials: "same-origin",
  });
  if (res.status === 204) return undefined as T;
  const body = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, errorMessage(body, `${res.status} ${res.statusText}`));
  return body as T;
}

export function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
}
