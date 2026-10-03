import { useEffect, useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, Device, Tag } from "./api";
import { LiveDevice } from "./live";

export function useDevices() {
  return useQuery({ queryKey: ["devices"], queryFn: () => api<Device[]>("/devices"), refetchInterval: 15_000 });
}

/** Merge the REST snapshot with newer values from the live stream. */
export function mergeLive(d: Device, l: LiveDevice | undefined) {
  return {
    online: l?.online ?? d.online,
    status: l?.status ?? d.status,
    lastSeen: l?.lastSeen ?? d.last_seen,
    value: (t: Tag) => (l?.values[t.tag] ? l.values[t.tag].v : t.value),
    quality: (t: Tag) => (l?.values[t.tag] ? l.values[t.tag].q : t.quality === "BAD" ? 2 : t.quality === "UNCERTAIN" ? 1 : 0),
    ts: (t: Tag) => l?.values[t.tag]?.ts ?? t.ts,
  };
}

const KEY = "plc-asset";

function remembered(): string | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

/**
 * The asset the console screens show. Comes from ?asset= (so links and reloads are stable), else
 * the last one used, else the first real (non-simulated) device.
 */
export function useAsset() {
  const devices = useDevices();
  const [params, setParams] = useSearchParams();
  const list = useMemo(
    () => (devices.data ?? []).slice().sort((a, b) => Number(a.simulated) - Number(b.simulated) || a.device_id.localeCompare(b.device_id)),
    [devices.data],
  );
  const wanted = params.get("asset") || remembered();
  const asset = list.find((d) => d.device_id === wanted) ?? list[0];

  useEffect(() => {
    if (!asset) return;
    try {
      localStorage.setItem(KEY, asset.device_id);
    } catch {
      /* storage unavailable */
    }
  }, [asset]);

  const select = (id: string) => {
    params.set("asset", id);
    setParams(params, { replace: true });
  };
  return { asset, assets: list, loading: devices.isLoading, select };
}

/** Tag playing a role (speed, moisture, ...) on an asset, if one is assigned. */
export function roleTag(tags: Tag[] | undefined, role: string): Tag | undefined {
  return tags?.find((t) => t.role === role);
}

/** Speed above which the machine counts as running (mirrors the API's speed_threshold). */
export function speedThreshold(d: Device, speed: Tag | undefined): number {
  const min = d.asset_config?.running_speed_min;
  if (typeof min === "number") return min;
  return speed?.min_value && speed.min_value > 0 ? 0.5 * speed.min_value : 0;
}

/**
 * Running right now? Follows asset_config.running_source like the analytics: "status" uses the
 * status register, "speed" the speed threshold, "auto" the register unless the analytics found it
 * contradicting speed (conflict), then speed. null = unknown.
 */
export function runningNow(d: Device, tags: Tag[], value: (t: Tag) => unknown, status: string | null, conflict: boolean): boolean | null {
  const mode = d.asset_config?.running_source ?? "auto";
  const speed = roleTag(tags, "speed");
  const statusTag = roleTag(tags, "machine_status");
  const v = speed ? value(speed) : null;
  if (typeof v === "number" && (mode === "speed" || (mode === "auto" && (conflict || !statusTag)))) return v > speedThreshold(d, speed);
  if (status === "RUN") return true;
  if (status === "STOP" || status === "IDLE" || status === "FAULT") return false;
  return null;
}
