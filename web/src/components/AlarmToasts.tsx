import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Alarm } from "../api";
import { live } from "../live";
import { Chip, fmtT } from "./console";
import { CloseIcon } from "./icons";

/** Alarm pop-ups: every newly raised alarm shows in the corner and plays an alert sound (always on).
 *  Warnings go away after a while, critical alarms stay until closed. */

const WARNING_MS = 15_000;
const MAX_SHOWN = 4;

// ---------------------------------------------------------------- tones (Web Audio, no sound files)

let ctx: AudioContext | null = null;

function audio(): AudioContext | null {
  try {
    ctx = ctx ?? new AudioContext();
    if (ctx.state === "suspended") void ctx.resume();
    return ctx;
  } catch {
    return null;
  }
}

// browsers only allow sound after the user has interacted with the page once
if (typeof window !== "undefined") {
  const unlock = () => {
    audio();
    window.removeEventListener("pointerdown", unlock);
    window.removeEventListener("keydown", unlock);
  };
  window.addEventListener("pointerdown", unlock);
  window.addEventListener("keydown", unlock);
}

/** Tones of one alarm; returns how long they take (s). */
function tones(ac: AudioContext, at: number, severity: string): number {
  const critical = severity === "critical";
  const freqs = critical ? [1046, 1046, 1046] : [880, 660];
  const step = critical ? 0.22 : 0.28;
  freqs.forEach((freq, i) => {
    const t0 = at + i * step;
    const osc = ac.createOscillator();
    const gain = ac.createGain();
    osc.type = critical ? "square" : "sine";
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, t0);
    gain.gain.exponentialRampToValueAtTime(critical ? 0.18 : 0.25, t0 + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, t0 + (critical ? 0.18 : 0.24));
    osc.connect(gain).connect(ac.destination);
    osc.start(t0);
    osc.stop(t0 + 0.3);
  });
  return freqs.length * step;
}

let nextFree = 0; // audio-clock time when the previous alarm's tones end

/** Every alarm gets its own sound; alarms arriving together play one after another. */
function beep(severity: string) {
  const ac = audio();
  if (!ac) return;
  const at = Math.max(ac.currentTime + 0.02, nextFree);
  nextFree = at + tones(ac, at, severity) + 0.25;
}

// ---------------------------------------------------------------- the pop-ups

interface Toast {
  alarm: Alarm;
  at: number;
}

export function AlarmToasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const timers = useRef(new Map<number, number>());

  const close = (id: number) => {
    setToasts((ts) => ts.filter((t) => t.alarm.id !== id));
    const tm = timers.current.get(id);
    if (tm) window.clearTimeout(tm);
    timers.current.delete(id);
  };

  useEffect(() => {
    const off = live.onAlarm(({ event, alarm }) => {
      if (event === "raised") {
        setToasts((ts) => [{ alarm, at: Date.now() }, ...ts.filter((t) => t.alarm.id !== alarm.id)]);
        beep(alarm.severity);
        if (alarm.severity !== "critical") {
          timers.current.set(alarm.id, window.setTimeout(() => close(alarm.id), WARNING_MS));
        }
      } else if (event === "ack") {
        close(alarm.id); // acknowledged somewhere: nothing more to tell
      } else if (event === "cleared") {
        setToasts((ts) => ts.map((t) => (t.alarm.id === alarm.id ? { ...t, alarm: { ...t.alarm, cleared_at: alarm.cleared_at } } : t)));
      }
    });
    const tms = timers.current;
    return () => {
      off();
      tms.forEach((tm) => window.clearTimeout(tm));
    };
  }, []);

  if (!toasts.length) return null;
  const shown = toasts.slice(0, MAX_SHOWN);
  const more = toasts.length - shown.length;
  return (
    <div className="toasts" aria-label="New alarms">
      {shown.map(({ alarm: a }) => {
        const crit = a.severity === "critical";
        return (
          <div key={a.id} className={`toast ${crit ? "crit" : "warn"}`} role={crit ? "alert" : "status"} aria-live={crit ? "assertive" : "polite"}>
            <div className="thead">
              <Chip cls={crit ? "crit" : "warn"}>{crit ? "Critical" : "Warning"}</Chip>
              {a.cleared_at && <Chip cls="ok">Cleared</Chip>}
              <span className="muted mono">{fmtT(a.raised_at)}</span>
              <button className="small ghost x" onClick={() => close(a.id)} aria-label="Close">
                <CloseIcon />
              </button>
            </div>
            <b>{a.explain?.what ?? (a.message || a.rule_name)}</b>
            {a.explain?.why && <div className="ctx">{a.explain.why}</div>}
            <div className="tfoot">
              <span className="muted small">{a.device_id}</span>
              <Link className="small" to={`/alarms?asset=${encodeURIComponent(a.device_id)}`} onClick={() => close(a.id)}>
                Open in Alarms & events
              </Link>
            </div>
          </div>
        );
      })}
      {more > 0 && (
        <Link className="toast more" to="/alarms" onClick={() => setToasts([])}>
          + {more} more new alarm{more === 1 ? "" : "s"}
        </Link>
      )}
    </div>
  );
}
