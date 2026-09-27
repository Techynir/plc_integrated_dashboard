/**
 * Live data over the /api/v1/stream WebSocket.
 * Holds the newest value of every tag per device plus online/status changes, and
 * notifies React through useSyncExternalStore. Reconnects with backoff.
 */
import { useSyncExternalStore } from "react";
import type { Alarm, IngestorStats, TagValue } from "./api";

export interface LiveTag {
  v: TagValue;
  q: number;
  ts: string;
}

export interface LiveDevice {
  values: Record<string, LiveTag>;
  status?: string | null;
  online?: boolean;
  lastSeen?: string;
}

type LiveMessage =
  | { type: "live"; device_id: string; ts: string; status: string | null; values: Record<string, { v: TagValue; q: number }> }
  | { type: "status"; device_id: string; online: boolean; status: string | null; last_seen: string | null }
  | { type: "alarm"; event: "raised" | "cleared" | "ack"; alarm: Alarm }
  | IngestorStats;

export type ConnectionState = "connecting" | "open" | "closed";

class LiveStore {
  devices = new Map<string, LiveDevice>();
  stats: IngestorStats | null = null;
  connection: ConnectionState = "closed";
  private listeners = new Set<() => void>();
  private alarmListeners = new Set<(a: { event: string; alarm: Alarm }) => void>();
  private ws: WebSocket | null = null;
  private retry = 0;
  private stopped = true;
  private pending = false;

  start() {
    if (!this.stopped) return;
    this.stopped = false;
    this.open();
  }

  stop() {
    this.stopped = true;
    this.ws?.close();
    this.ws = null;
  }

  private open() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/api/v1/stream`);
    this.ws = ws;
    this.setConnection("connecting");
    ws.onopen = () => {
      this.retry = 0;
      this.setConnection("open");
    };
    ws.onmessage = (ev) => {
      try {
        this.handle(JSON.parse(ev.data) as LiveMessage);
      } catch {
        /* ignore malformed frames */
      }
    };
    ws.onclose = () => {
      this.ws = null;
      this.setConnection("closed");
      if (this.stopped) return;
      const delay = Math.min(30_000, 1000 * 2 ** this.retry++) * (0.75 + Math.random() * 0.5);
      setTimeout(() => !this.stopped && this.open(), delay);
    };
  }

  private setConnection(state: ConnectionState) {
    this.connection = state;
    this.emit();
  }

  private handle(msg: LiveMessage) {
    if (msg.type === "live") {
      const prev = this.devices.get(msg.device_id) ?? { values: {} };
      const values = { ...prev.values };
      for (const [tag, { v, q }] of Object.entries(msg.values)) values[tag] = { v, q, ts: msg.ts };
      this.devices.set(msg.device_id, {
        ...prev,
        values,
        status: msg.status,
        online: true,
        lastSeen: new Date().toISOString(),
      });
    } else if (msg.type === "status") {
      const prev = this.devices.get(msg.device_id) ?? { values: {} };
      this.devices.set(msg.device_id, {
        ...prev,
        online: msg.online,
        status: msg.status,
        lastSeen: msg.last_seen ?? prev.lastSeen,
      });
    } else if (msg.type === "alarm") {
      this.alarmListeners.forEach((fn) => fn(msg));
      return;
    } else if (msg.type === "stats") {
      this.stats = msg;
    }
    this.emit();
  }

  /** Coalesce bursts (many devices per second) into one React update per frame. */
  private emit() {
    if (this.pending) return;
    this.pending = true;
    requestAnimationFrame(() => {
      this.pending = false;
      this.version++;
      this.listeners.forEach((fn) => fn());
    });
  }

  version = 0;

  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  onAlarm(fn: (a: { event: string; alarm: Alarm }) => void) {
    this.alarmListeners.add(fn);
    return () => {
      this.alarmListeners.delete(fn);
    };
  }
}

export const live = new LiveStore();

export function useLiveDevice(deviceId: string | undefined): LiveDevice | undefined {
  return useSyncExternalStore(live.subscribe, () => (deviceId ? live.devices.get(deviceId) : undefined));
}

/** Re-renders on every live update (throttled to one per animation frame). */
export function useLiveVersion(): number {
  return useSyncExternalStore(live.subscribe, () => live.version);
}

export function useLiveConnection(): ConnectionState {
  return useSyncExternalStore(live.subscribe, () => live.connection);
}

export function useIngestorStats(): IngestorStats | null {
  return useSyncExternalStore(live.subscribe, () => live.stats);
}
