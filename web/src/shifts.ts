/** Plant shifts, local plant time: two 12-hour shifts, the machine runs 24/7. B runs from 18:00
 *  through 06:00 the next morning. Keep in step with SHIFTS in services/api/app/analytics.py. */
export const SHIFTS = [
  { key: "A", label: "A · 06–18", from: "06:00", to: "18:00" },
  { key: "B", label: "B · 18–06", from: "18:00", to: "06:00" },
] as const;

export type ShiftKey = (typeof SHIFTS)[number]["key"];

export const SHIFT_HOURS = SHIFTS.map((s) => `${s.key} ${s.from.slice(0, 2)}–${s.to.slice(0, 2)}`).join(" · ");
