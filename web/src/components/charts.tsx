/**
 * ECharts wrappers for the console screens. Colours come from CSS tokens so light/dark both work;
 * every chart redraws when the theme changes.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import * as echarts from "echarts/core";
import { BarChart, CustomChart, LineChart, ScatterChart } from "echarts/charts";
import { GridComponent, MarkAreaComponent, MarkLineComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { EChartsCoreOption } from "echarts/core";
import { fmtNum, fmtT } from "./console";

echarts.use([LineChart, ScatterChart, BarChart, CustomChart, GridComponent, TooltipComponent, MarkAreaComponent, MarkLineComponent, CanvasRenderer]);

export interface Tokens {
  ink: string;
  ink2: string;
  muted: string;
  grid: string;
  axis: string;
  surface: string;
  surface2: string;
  trace: string;
  traceFill: string;
  band: string;
  bandLine: string;
  good: string;
  warning: string;
  critical: string;
  comms: string;
  blue: string;
  teal: string;
  navy: string;
  mono: string;
  font: string;
}

/** Categorical series colours (validated order: blue, orange, aqua, violet, magenta). Tags beyond
 *  five fall back to the neutral trace colour instead of repeating a hue. */
export const SERIES_VARS = ["--cat-1", "--cat-2", "--cat-3", "--cat-4", "--cat-5"];

export function seriesColor(index: number): string {
  return index >= 0 && index < SERIES_VARS.length ? `var(${SERIES_VARS[index]})` : "var(--trace)";
}

/** Resolve "var(--x)" to a concrete colour (canvas charts need real values). */
export function resolveColor(c: string): string {
  const m = c.match(/^var\((--[\w-]+)\)$/);
  return m ? getComputedStyle(document.documentElement).getPropertyValue(m[1]).trim() : c;
}

function withAlpha(color: string, a: number): string {
  const m = color.match(/^#([0-9a-f]{6})$/i);
  if (!m) return color;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

function readTokens(): Tokens {
  const css = getComputedStyle(document.documentElement);
  const v = (n: string) => css.getPropertyValue(n).trim();
  return {
    ink: v("--ink"), ink2: v("--ink-2"), muted: v("--ink-muted"), grid: v("--grid"), axis: v("--axis"),
    surface: v("--surface"), surface2: v("--surface-2"), trace: v("--trace"), traceFill: v("--trace-fill"),
    band: v("--band"), bandLine: v("--band-line"), good: v("--good"), warning: v("--warning"),
    critical: v("--critical"), comms: v("--comms"), blue: v("--series-1"), teal: v("--teal"), navy: v("--navy"),
    mono: v("--mono"), font: v("--font"),
  };
}

/** Theme tokens that follow the OS theme and the in-app toggle (data-theme). */
export function useTokens(): Tokens {
  const [tokens, setTokens] = useState(readTokens);
  useEffect(() => {
    const update = () => setTokens(readTokens());
    const mq = matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", update);
    const mo = new MutationObserver(update);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => {
      mq.removeEventListener("change", update);
      mo.disconnect();
    };
  }, []);
  return tokens;
}

/** Diagonal hatch used for "no data" (comms lost) spans, so gaps are never mistaken for zero. */
type Pattern = { image: HTMLCanvasElement; repeat: "repeat" } | string;

function hatch(color: string): Pattern {
  const c = document.createElement("canvas");
  c.width = c.height = 8;
  const g = c.getContext("2d");
  if (!g) return color;
  g.strokeStyle = color;
  g.globalAlpha = 0.55;
  g.lineWidth = 1.5;
  g.beginPath();
  g.moveTo(0, 8);
  g.lineTo(8, 0);
  g.stroke();
  return { image: c, repeat: "repeat" };
}

export function EChart({ option, height, ariaLabel }: { option: EChartsCoreOption; height: number; ariaLabel: string }) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  useEffect(() => {
    if (!el.current) return;
    chart.current = echarts.init(el.current, undefined, { renderer: "canvas" });
    const ro = new ResizeObserver(() => chart.current?.resize());
    ro.observe(el.current);
    return () => {
      ro.disconnect();
      chart.current?.dispose();
      chart.current = null;
    };
  }, []);
  useEffect(() => {
    chart.current?.setOption(option, { notMerge: true, lazyUpdate: true });
  }, [option]);
  return <div ref={el} style={{ width: "100%", height }} role="img" aria-label={ariaLabel} />;
}

/** Enough decimals that ticks across a span of this size are distinguishable. */
function spanDecimals(span: number): number {
  if (!(span > 0)) return 2;
  return Math.min(6, Math.max(0, Math.ceil(-Math.log10(span / 5)) + 1));
}

function axisCommon(t: Tokens) {
  return {
    axisLine: { lineStyle: { color: t.axis } },
    axisTick: { show: false },
    axisLabel: { color: t.muted, fontFamily: t.mono, fontSize: 10.5 },
    splitLine: { lineStyle: { color: t.grid } },
  };
}

function tooltipBase(t: Tokens) {
  return {
    backgroundColor: t.surface,
    borderColor: t.axis,
    textStyle: { color: t.ink, fontFamily: t.font, fontSize: 12 },
    extraCssText: "box-shadow: 0 6px 18px rgba(0,0,0,.18); border-radius: 8px;",
  };
}

// ---------------------------------------------------------------- lanes (stacked strip charts)

export interface Lane {
  key: string;
  label: string;
  unit: string;
  decimals: number;
  /** [ms, avg, min?, max?] — min/max draw an envelope when the data is bucketed */
  points: [number, number | null, number | null, number | null][];
  normal?: [number, number] | null;
  warn?: number | null;
  crit?: number | null;
  dir?: "high" | "low" | null;
  /** step line for discrete signals (status codes) */
  step?: boolean;
  valueLabels?: Record<string, string> | null;
  current?: string;
  /** OK when the live value is in its normal state, NG when not (the whole row turns red), STOP while the
   *  machine is stopped and the value is not expected to be normal (e.g. crawl speed during a tear). */
  verdict?: "OK" | "NG" | "STOP";
  /** Where an NG verdict opens, usually Alarms & events for this tag. */
  verdictHref?: string;
  sub?: string;
  /** series colour (CSS value or var(--…)); default = neutral trace */
  color?: string;
}

/** Break the line where samples are missing longer than gapMs, and return those spans for hatching. */
function splitGaps(points: Lane["points"], gapMs: number, from: number, to: number) {
  const out: Lane["points"] = [];
  const gaps: [number, number][] = [];
  let prev = from;
  for (const p of points) {
    if (p[0] - prev > gapMs) {
      gaps.push([prev, p[0]]);
      if (out.length) out.push([prev + 1, null, null, null]);
    }
    out.push(p);
    prev = p[0];
  }
  if (to - prev > gapMs) gaps.push([prev, to]);
  return { pts: out, gaps };
}

export function Lanes({
  lanes,
  from,
  to,
  gapMs,
  laneHeight = 74,
  showValues = true,
}: {
  lanes: Lane[];
  from: number;
  to: number;
  gapMs: number;
  laneHeight?: number;
  showValues?: boolean;
}) {
  const t = useTokens();
  const option = useMemo(() => {
    const pattern = hatch(t.comms);
    const top = 6;
    const axisH = 22;
    const grids = lanes.map((l, i) => ({
      left: 52, right: 14, top: top + i * laneHeight, height: laneHeight - 14,
      ...(l.verdict === "NG" ? { show: true, backgroundColor: withAlpha(t.critical, 0.1), borderWidth: 0 } : {}),
    }));
    const xAxes = lanes.map((_, i) => ({
      type: "time", gridIndex: i, min: from, max: to, ...axisCommon(t),
      axisLabel: { ...axisCommon(t).axisLabel, show: i === lanes.length - 1, hideOverlap: true, formatter: (v: number) => fmtT(v, to - from < 3600_000) },
      splitLine: { show: true, lineStyle: { color: t.grid } },
    }));
    const yAxes = lanes.map((l, i) => {
      const vals = l.points.flatMap((p) => [p[1], p[2], p[3]]).filter((v): v is number => typeof v === "number");
      const refs = [...(l.normal ?? []), l.warn, l.crit].filter((v): v is number => typeof v === "number");
      const all = vals.length ? [...vals, ...(l.normal ?? [])] : refs;
      let lo = all.length ? Math.min(...all) : 0;
      let hi = all.length ? Math.max(...all) : 1;
      // keep the nearer limit in view so a value drifting towards it is obvious
      if (l.dir === "high" && typeof l.warn === "number" && l.warn - hi < (hi - lo || 1) * 1.5) hi = Math.max(hi, l.warn);
      if (l.dir === "low" && typeof l.warn === "number" && lo - l.warn < (hi - lo || 1) * 1.5) lo = Math.min(lo, l.warn);
      const pad = (hi - lo) * 0.12 || Math.abs(hi) * 0.05 || 0.5;
      return {
        type: "value", gridIndex: i, scale: true, min: l.step ? undefined : lo - pad, max: l.step ? undefined : hi + pad,
        minInterval: l.step ? 1 : undefined, splitNumber: 2, ...axisCommon(t),
        axisLabel: { ...axisCommon(t).axisLabel, showMinLabel: !!l.step, showMaxLabel: !!l.step, formatter: (v: number) => (l.valueLabels?.[String(v)] ?? fmtNum(v, Math.min(l.decimals, 2))).slice(0, 7) },
      };
    });
    const series: object[] = [];
    lanes.forEach((l, i) => {
      const lineColor = l.color ? resolveColor(l.color) : t.trace;
      const { pts, gaps } = splitGaps(l.points, gapMs, from, to);
      const hasEnv = pts.some((p) => p[2] !== null && p[3] !== null && p[2] !== p[3]);
      const markLines = [
        typeof l.warn === "number" ? { yAxis: l.warn, lineStyle: { color: t.warning, type: "dashed", width: 1.2 }, label: { show: false } } : null,
        typeof l.crit === "number" ? { yAxis: l.crit, lineStyle: { color: t.critical, type: "dashed", width: 1.2 }, label: { show: false } } : null,
      ].filter(Boolean);
      const areas: object[] = [];
      if (l.normal) areas.push([{ yAxis: l.normal[0], itemStyle: { color: t.band } }, { yAxis: l.normal[1] }]);
      gaps.forEach(([a, b]) => areas.push([{ xAxis: a, itemStyle: { color: pattern } }, { xAxis: b }]));
      if (hasEnv) {
        series.push(
          { type: "line", xAxisIndex: i, yAxisIndex: i, data: pts.map((p) => [p[0], p[2]]), stack: `env${i}`, symbol: "none", lineStyle: { opacity: 0 }, silent: true, connectNulls: false, tooltip: { show: false } },
          { type: "line", xAxisIndex: i, yAxisIndex: i, data: pts.map((p) => [p[0], p[3] !== null && p[2] !== null ? p[3] - p[2] : null]), stack: `env${i}`, symbol: "none", lineStyle: { opacity: 0 }, areaStyle: { color: l.color ? withAlpha(lineColor, 0.16) : t.traceFill }, silent: true, connectNulls: false, tooltip: { show: false } },
        );
      }
      series.push({
        name: l.label, type: "line", xAxisIndex: i, yAxisIndex: i, data: pts.map((p) => [p[0], p[1]]),
        step: l.step ? "end" : undefined, showSymbol: false, symbolSize: 6, connectNulls: false,
        lineStyle: { color: lineColor, width: 1.75 }, itemStyle: { color: lineColor },
        markLine: markLines.length ? { symbol: "none", silent: true, data: markLines, animation: false } : undefined,
        markArea: areas.length ? { silent: true, data: areas, animation: false } : undefined,
      });
    });
    const unitOf = new Map(lanes.map((l) => [l.label, l]));
    return {
      animation: false,
      grid: grids,
      xAxis: xAxes,
      yAxis: yAxes,
      axisPointer: { link: [{ xAxisIndex: "all" }], lineStyle: { color: t.muted } },
      tooltip: {
        trigger: "axis", ...tooltipBase(t),
        formatter: (ps: { seriesName: string; value: [number, number | null] }[]) => {
          if (!ps.length) return "";
          const head = `<div style="font-family:${t.mono};font-size:11px;color:${t.muted}">${new Date(ps[0].value[0]).toLocaleString()}</div>`;
          return head + ps
            .filter((p) => unitOf.has(p.seriesName))
            .map((p) => {
              const l = unitOf.get(p.seriesName)!;
              const v = p.value[1];
              const txt = v === null ? "no data" : l.valueLabels?.[String(v)] ?? `${fmtNum(v, l.decimals)} ${l.unit}`;
              return `<div style="display:flex;justify-content:space-between;gap:14px"><span>${l.label}</span><b style="font-family:${t.mono};font-weight:500">${txt}</b></div>`;
            })
            .join("");
        },
      },
      series,
      _h: axisH,
    } as EChartsCoreOption;
  }, [lanes, from, to, gapMs, laneHeight, t]);

  const height = lanes.length * laneHeight + 26;
  const readout = lanes.some((l) => l.verdict);
  return (
    <div
      className={`lanes${readout ? " with-verdict" : ""}`}
      style={showValues ? undefined : { gridTemplateColumns: "1fr" }}
    >
      {showValues && (
        <div className="labels" style={{ paddingTop: 0 }}>
          {lanes.map((l) => (
            <div key={l.key} className={`ll${l.verdict === "NG" ? " ng" : ""}`} style={{ height: laneHeight }}>
              <b>
                {l.color && <i className="swatch" style={{ background: l.color }} />}
                {l.label}
              </b>
              <span>{l.sub ?? l.unit}</span>
              {l.current !== undefined && !readout && <span className="lv">{l.current}</span>}
            </div>
          ))}
        </div>
      )}
      <EChart option={option} height={height} ariaLabel={`Trend of ${lanes.map((l) => l.label).join(", ")}`} />
      {readout && (
        <div className="readout">
          {lanes.map((l) => (
            <div key={l.key} className={`rv${l.verdict === "NG" ? " ng" : ""}`} style={{ height: laneHeight }}>
              <span className="lv">{l.current || "—"}</span>
              {l.verdict === "NG" && l.verdictHref ? (
                <Link to={l.verdictHref} className="verdict ng" title={`Show ${l.label} in Alarms & events`}>
                  NG
                </Link>
              ) : (
                <span className={`verdict ${l.verdict === "OK" ? "ok" : l.verdict === "STOP" ? "stop" : "ng"}`}
                  title={l.verdict === "STOP" ? "Machine stopped: this value is not expected to be normal now" : undefined}>
                  {l.verdict ?? "NG"}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- machine state strip

export interface StateSeg {
  state: "run" | "stop" | "comms";
  start: number; // epoch seconds
  end: number;
}

export function StateStrip({ segments, from, to, height = 64 }: { segments: StateSeg[]; from: number; to: number; height?: number }) {
  const t = useTokens();
  const option = useMemo(() => {
    const STATES = ["run", "stop", "comms"] as const;
    const color = [withAlpha(t.trace, 0.55), t.warning, t.comms];
    const label = ["Running", "Stopped", "Communication lost"];
    return {
      animation: false,
      grid: { left: 10, right: 14, top: 6, height: height - 32 },
      xAxis: { type: "time", min: from * 1000, max: to * 1000, ...axisCommon(t), axisLabel: { ...axisCommon(t).axisLabel, hideOverlap: true, formatter: (v: number) => fmtT(v, false) } },
      yAxis: { type: "value", min: 0, max: 1, show: false },
      tooltip: {
        ...tooltipBase(t),
        formatter: (p: { value: [number, number, number] }) =>
          `<b>${label[p.value[2]]}</b><br/><span style="font-family:${t.mono};font-size:11px">${fmtT(p.value[0])} – ${fmtT(p.value[1])} · ${Math.round((p.value[1] - p.value[0]) / 60000)} min</span>`,
      },
      series: [
        {
          type: "custom",
          data: segments.map((s) => [s.start * 1000, s.end * 1000, STATES.indexOf(s.state)]),
          renderItem: (_: unknown, api: { value: (i: number) => number | string; coord: (v: number[]) => number[]; size: (v: number[]) => number[] }) => {
            const a = api.coord([api.value(0) as number, 1]);
            const b = api.coord([api.value(1) as number, 0]);
            return {
              type: "rect",
              shape: { x: a[0], y: a[1], width: Math.max(1, b[0] - a[0]), height: b[1] - a[1] },
              style: { fill: color[api.value(2) as number] },
            };
          },
          encode: { x: [0, 1] },
        },
      ],
    } as EChartsCoreOption;
  }, [segments, from, to, height, t]);
  return (
    <>
      <EChart option={option} height={height} ariaLabel="Machine state over time" />
    </>
  );
}

export function StateLegend() {
  return (
    <div className="legend">
      <span><i style={{ background: "var(--trace)", opacity: 0.55 }} />Running</span>
      <span><i style={{ background: "var(--warning)" }} />Stopped</span>
      <span><i style={{ background: "var(--comms)" }} />Communication lost</span>
    </div>
  );
}

// ---------------------------------------------------------------- sparkline

export function Sparkline({ points, color, range, height = 44, ariaLabel }: { points: [number, number | null][]; color?: string; range?: [number, number] | null; height?: number; ariaLabel: string }) {
  const t = useTokens();
  const option = useMemo(() => {
    const c = color ? resolveColor(color) : t.trace;
    const vals = points.map((p) => p[1]).filter((v): v is number => typeof v === "number").concat(range ?? []);
    const lo = vals.length ? Math.min(...vals) : 0;
    const hi = vals.length ? Math.max(...vals) : 1;
    const pad = (hi - lo) * 0.15 || Math.abs(hi) * 0.01 || 1;
    return {
      animation: false,
      grid: { left: 2, right: 6, top: 4, bottom: 4 },
      xAxis: { type: "time", show: false },
      yAxis: { type: "value", show: false, min: lo - pad, max: hi + pad },
      series: [{ type: "line", data: points, showSymbol: false, connectNulls: false, lineStyle: { color: c, width: 1.5 }, silent: true }],
    } as EChartsCoreOption;
  }, [points, color, range, t]);
  return <EChart option={option} height={height} ariaLabel={ariaLabel} />;
}

// ---------------------------------------------------------------- SPC (I chart of 1-minute means)

export function SpcChart({
  points,
  limits,
  spec,
  unit,
  decimals,
  predicted,
  warn,
  areas: ruleAreas,
}: {
  points: { t: number; v: number; flag: number }[];
  limits: { cl: number; ucl: number; lcl: number; no_variation?: boolean } | null;
  spec: { lsl: number | null; usl: number | null };
  unit: string;
  decimals: number;
  /** moisture expected from steam pressure (process rule), drawn dashed */
  predicted?: { t: number; v: number }[];
  /** warning limit, drawn as a line */
  warn?: number | null;
  /** times a process rule was active, shaded */
  areas?: { start: number; end: number }[];
}) {
  const t = useTokens();
  const option = useMemo(() => {
    const lines: object[] = [];
    if (limits && !limits.no_variation) {
      lines.push(
        { yAxis: limits.cl, name: "CL", lineStyle: { color: t.ink2, type: "solid", width: 1 } },
        { yAxis: limits.ucl, name: "UCL", lineStyle: { color: t.critical, type: "dashed", width: 1.2 } },
        { yAxis: limits.lcl, name: "LCL", lineStyle: { color: t.critical, type: "dashed", width: 1.2 } },
      );
    }
    if (warn != null) lines.push({ yAxis: warn, name: "Warning", lineStyle: { color: t.warning, type: "solid", width: 1.2 } });
    const t0 = points.length ? points[0].t : 0;
    const t1 = points.length ? points[points.length - 1].t + 60 : 0;
    const pred = (predicted ?? []).filter((p) => p.t >= t0 && p.t <= t1);
    const ref = [...points.map((p) => p.v), ...pred.map((p) => p.v), spec.lsl, spec.usl, warn, ...(limits && !limits.no_variation ? [limits.ucl, limits.lcl] : [])].filter((v): v is number => typeof v === "number");
    const yLo = ref.length ? Math.min(...ref) : 0;
    const yHi = ref.length ? Math.max(...ref) : 1;
    const yPad = (yHi - yLo) * 0.1 || 0.1;
    const areas = spec.lsl !== null && spec.usl !== null ? [[{ yAxis: spec.lsl, itemStyle: { color: t.band } }, { yAxis: spec.usl }]] : [];
    return {
      animation: false,
      grid: { left: 52, right: 60, top: 14, bottom: 26 },
      xAxis: { type: "time", ...axisCommon(t), axisLabel: { ...axisCommon(t).axisLabel, hideOverlap: true, formatter: (v: number) => fmtT(v, false) } },
      yAxis: { type: "value", min: +(yLo - yPad).toPrecision(4), max: +(yHi + yPad).toPrecision(4), ...axisCommon(t), axisLabel: { ...axisCommon(t).axisLabel, showMinLabel: false, showMaxLabel: false } },
      tooltip: { trigger: "axis", ...tooltipBase(t), valueFormatter: (v: number) => `${fmtNum(v, decimals + 1)} ${unit}` },
      series: [
        {
          name: "1-min mean", type: "line", data: points.map((p) => [p.t * 1000, p.v]), symbolSize: 5, showSymbol: points.length < 240,
          lineStyle: { color: t.trace, width: 1.4 }, itemStyle: { color: t.trace },
          markLine: lines.length ? { symbol: "none", silent: true, data: lines, label: { position: "end", color: t.muted, fontFamily: t.mono, fontSize: 10, formatter: "{b}" } } : undefined,
          markArea: areas.length ? { silent: true, data: areas } : undefined,
        },
        ...(ruleAreas?.length
          ? [{
              name: "Rule active", type: "line", data: [], silent: true,
              markArea: { silent: true, itemStyle: { color: withAlpha(t.warning, 0.16) }, data: ruleAreas.map((a) => [{ xAxis: a.start * 1000 }, { xAxis: a.end * 1000 }]) },
            }]
          : []),
        ...(pred.length
          ? [{
              name: "Expected from steam", type: "line", data: pred.map((p) => [p.t * 1000, p.v]), symbol: "none",
              lineStyle: { color: t.warning, type: "dashed", width: 1.3 }, itemStyle: { color: t.warning },
            }]
          : []),
        {
          name: "Out of control", type: "scatter", symbolSize: 9,
          data: points.filter((p) => p.flag > 0).map((p) => [p.t * 1000, p.v, p.flag]),
          itemStyle: { color: (p: { value: number[] }) => (p.value[2] === 2 ? t.critical : t.warning), borderColor: t.surface, borderWidth: 2 },
        },
      ],
    } as EChartsCoreOption;
  }, [points, limits, spec, unit, decimals, predicted, warn, ruleAreas, t]);
  return <EChart option={option} height={240} ariaLabel="Moisture control chart" />;
}

// ---------------------------------------------------------------- histogram

export function Histogram({ bins, spec, mean, unit }: { bins: { lo: number; hi: number; count: number }[]; spec: { lsl: number | null; usl: number | null }; mean: number | null; unit: string }) {
  const t = useTokens();
  const option = useMemo(() => {
    const lines: object[] = [];
    if (spec.lsl !== null) lines.push({ xAxis: spec.lsl, name: "LSL", lineStyle: { color: t.critical, type: "dashed" } });
    if (spec.usl !== null) lines.push({ xAxis: spec.usl, name: "USL", lineStyle: { color: t.critical, type: "dashed" } });
    if (mean !== null) lines.push({ xAxis: mean, name: "mean", lineStyle: { color: t.ink2, type: "solid" } });
    return {
      animation: false,
      grid: { left: 44, right: 16, top: 18, bottom: 26 },
      xAxis: { type: "value", scale: true, ...axisCommon(t), splitLine: { show: false } },
      yAxis: { type: "value", ...axisCommon(t), minInterval: 1 },
      tooltip: {
        ...tooltipBase(t),
        formatter: (p: { value: [number, number, number, number] }) =>
          `<span style="font-family:${t.mono}">${fmtNum(p.value[2], 2)}–${fmtNum(p.value[3], 2)} ${unit}</span><br/><b>${p.value[1]}</b> samples`,
      },
      series: [
        {
          type: "custom",
          data: bins.map((b) => [(b.lo + b.hi) / 2, b.count, b.lo, b.hi]),
          renderItem: (_: unknown, api: { value: (i: number) => number; coord: (v: number[]) => number[] }) => {
            const a = api.coord([api.value(2), api.value(1)]);
            const b = api.coord([api.value(3), 0]);
            return { type: "rect", shape: { x: a[0] + 1, y: a[1], width: Math.max(1, b[0] - a[0] - 2), height: b[1] - a[1], r: [3, 3, 0, 0] }, style: { fill: t.blue } };
          },
          encode: { x: [2, 3], y: 1 },
          markLine: { symbol: "none", silent: true, data: lines, label: { color: t.muted, fontFamily: t.mono, fontSize: 10, formatter: "{b}" } },
        },
      ],
    } as EChartsCoreOption;
  }, [bins, spec, mean, unit, t]);
  return <EChart option={option} height={220} ariaLabel="Distribution histogram" />;
}

// ---------------------------------------------------------------- scatter with optional model line

export function Scatter({
  points,
  model,
  band,
  xLabel,
  yLabel,
  highlight,
  height = 240,
  marks,
}: {
  points: { x: number; y: number; t?: number; hi?: boolean; recent?: boolean }[];
  model?: { m: number; b: number } | null;
  band?: number | null;
  xLabel: string;
  yLabel: string;
  highlight?: string;
  height?: number;
  /** reference lines with a short label, e.g. the steam pressure below which paper gets wet */
  marks?: { x?: number; y?: number; label: string }[];
}) {
  const t = useTokens();
  const option = useMemo(() => {
    const xs = points.map((p) => p.x).concat((marks ?? []).flatMap((m) => (m.x != null ? [m.x] : [])));
    const x0 = xs.length ? Math.min(...xs) : 0;
    const x1 = xs.length ? Math.max(...xs) : 1;
    const line = model ? [[x0, model.m * x0 + model.b], [x1, model.m * x1 + model.b]] : [];
    const ys = points.map((p) => p.y).concat(band && model ? line.flatMap(([, y]) => [y - band, y + band]) : []).concat((marks ?? []).flatMap((m) => (m.y != null ? [m.y] : [])));
    const y0 = ys.length ? Math.min(...ys) : 0;
    const y1 = ys.length ? Math.max(...ys) : 1;
    // variation below 0.001 % of the value is float noise: treat it as a constant signal
    const flat = (lo: number, hi: number) => hi - lo < 1e-5 * Math.max(1, Math.abs(hi));
    const yPad = flat(y0, y1) ? Math.max(Math.abs(y1) * 0.02, 0.1) : (y1 - y0) * 0.1;
    const xPad = flat(x0, x1) ? Math.max(Math.abs(x1) * 0.01, 0.1) : (x1 - x0) * 0.05;
    const series: object[] = [];
    if (model && line.length && band) {
      series.push(
        { type: "line", data: line.map(([x, y]) => [x, y - band]), stack: "band", symbol: "none", lineStyle: { opacity: 0 }, silent: true, tooltip: { show: false } },
        { type: "line", data: line.map(([x]) => [x, 2 * band]), stack: "band", symbol: "none", lineStyle: { opacity: 0 }, areaStyle: { color: t.band }, silent: true, tooltip: { show: false } },
      );
    }
    if (model && line.length) {
      series.push({ name: "model", type: "line", data: line, symbol: "none", lineStyle: { color: t.muted, type: "dashed", width: 1.2 }, silent: true, tooltip: { show: false } });
    }
    series.push({
      name: "minutes", type: "scatter", symbolSize: 6, data: points.filter((p) => !p.hi && !p.recent).map((p) => [p.x, p.y, p.t]), itemStyle: { color: t.trace, opacity: 0.55 },
      markLine: marks?.length
        ? {
            symbol: "none", silent: true,
            data: marks.map((m) => ({ ...(m.x != null ? { xAxis: m.x } : { yAxis: m.y }), name: m.label, lineStyle: { color: t.warning, type: "solid", width: 1.2 } })),
            label: { color: t.ink2, fontSize: 10, formatter: "{b}", position: "insideEndTop" },
          }
        : undefined,
    });
    if (points.some((p) => p.hi && !p.recent)) {
      series.push({ name: highlight ?? "flagged", type: "scatter", symbolSize: 7, data: points.filter((p) => p.hi && !p.recent).map((p) => [p.x, p.y, p.t]), itemStyle: { color: t.warning } });
    }
    if (points.some((p) => p.recent)) {
      series.push({
        name: "latest", type: "scatter", symbolSize: 10, data: points.filter((p) => p.recent).map((p) => [p.x, p.y, p.t, p.hi ? 1 : 0]),
        itemStyle: { color: (p: { value: number[] }) => (p.value[3] ? t.warning : t.blue), borderColor: t.surface, borderWidth: 2 },
      });
    }
    return {
      animation: false,
      grid: { left: 52, right: 16, top: 26, bottom: 40 },
      xAxis: { type: "value", min: x0 - xPad, max: x1 + xPad, name: xLabel, nameLocation: "middle", nameGap: 24, nameTextStyle: { color: t.muted, fontSize: 11 }, ...axisCommon(t), axisLabel: { ...axisCommon(t).axisLabel, showMinLabel: false, showMaxLabel: false, formatter: (v: number) => v.toFixed(spanDecimals(x1 - x0 + 2 * xPad)) } },
      yAxis: { type: "value", min: y0 - yPad, max: y1 + yPad, name: yLabel, nameLocation: "end", nameTextStyle: { color: t.muted, fontSize: 11, align: "left" }, ...axisCommon(t), axisLabel: { ...axisCommon(t).axisLabel, showMinLabel: false, showMaxLabel: false, formatter: (v: number) => v.toFixed(spanDecimals(y1 - y0 + 2 * yPad)) } },
      tooltip: {
        ...tooltipBase(t),
        formatter: (p: { value: [number, number, number?] }) =>
          `${p.value[2] ? `<span style="font-family:${t.mono};font-size:11px;color:${t.muted}">${fmtT(p.value[2] * 1000, false)}</span><br/>` : ""}${xLabel}: <b>${fmtNum(p.value[0], 2)}</b><br/>${yLabel}: <b>${fmtNum(p.value[1], 2)}</b>`,
      },
      series,
    } as EChartsCoreOption;
  }, [points, model, band, xLabel, yLabel, highlight, marks, t]);
  return <EChart option={option} height={height} ariaLabel={`${yLabel} against ${xLabel}`} />;
}

// ---------------------------------------------------------------- daily trend with projection

export function Projection({
  daily,
  projection,
  warn,
  normal,
  unit,
}: {
  daily: { offset: number; mean: number | null }[];
  projection: { m: number; b: number; days_to_warning: number | null } | null;
  warn: number | null;
  normal: [number, number] | null;
  unit: string;
}) {
  const t = useTokens();
  const option = useMemo(() => {
    const pts = daily.filter((d) => d.mean !== null) as { offset: number; mean: number }[];
    const cross = projection?.days_to_warning != null && projection.days_to_warning > 0 ? projection.days_to_warning : null;
    const xmax = Math.min(240, Math.max(30, cross !== null ? Math.ceil(cross + 15) : 30));
    const xe = cross !== null ? Math.min(xmax, cross) : xmax;
    const ys = [...pts.map((d) => d.mean), ...(normal ?? []), ...(warn !== null ? [warn] : [])];
    const lo = ys.length ? Math.min(...ys) : 0;
    const hi = ys.length ? Math.max(...ys) : 1;
    const pad = (hi - lo) * 0.1 || 0.2;
    const last = pts[pts.length - 1];
    return {
      animation: false,
      grid: { left: 48, right: 18, top: 30, bottom: 40 },
      xAxis: {
        type: "value", min: -30, max: xmax, name: "Days from today", nameLocation: "middle", nameGap: 24, nameTextStyle: { color: t.muted, fontSize: 11 },
        ...axisCommon(t), splitLine: { show: false },
        axisLabel: { ...axisCommon(t).axisLabel, formatter: (v: number) => (v === 0 ? "today" : `${v > 0 ? "+" : ""}${v}`) },
      },
      yAxis: { type: "value", min: lo - pad, max: hi + pad, name: unit, nameTextStyle: { color: t.muted, fontSize: 11, align: "left" }, ...axisCommon(t), axisLabel: { ...axisCommon(t).axisLabel, showMinLabel: false, showMaxLabel: false, formatter: (v: number) => v.toFixed(spanDecimals(hi - lo + 2 * pad)) } },
      tooltip: { trigger: "axis", ...tooltipBase(t), valueFormatter: (v: number) => `${fmtNum(v, 2)} ${unit}` },
      series: [
        {
          name: "Daily mean while running", type: "line", data: pts.map((d) => [d.offset, d.mean]), symbol: "circle", symbolSize: 5,
          lineStyle: { color: t.trace, width: 1.75 }, itemStyle: { color: t.trace },
          markArea: normal ? { silent: true, data: [[{ yAxis: normal[0], itemStyle: { color: t.band } }, { yAxis: normal[1] }]] } : undefined,
          markLine: {
            symbol: "none", silent: true, animation: false,
            data: [
              { xAxis: 0, lineStyle: { color: t.axis, type: "solid", width: 1 }, label: { show: false } },
              ...(warn !== null
                ? [{ yAxis: warn, lineStyle: { color: t.warning, type: "dashed", width: 1.2 }, label: { show: true, position: "insideStartTop", formatter: `Warning ${warn}`, color: t.warning, fontSize: 10.5 } }]
                : []),
            ],
          },
          markPoint: last ? { symbol: "circle", symbolSize: 9, silent: true, itemStyle: { color: t.blue, borderColor: t.surface, borderWidth: 2 }, label: { show: false }, data: [{ coord: [last.offset, last.mean] }] } : undefined,
        },
        ...(projection
          ? [
              {
                name: "Linear projection", type: "line", data: [[-30, projection.m * -30 + projection.b], [xe, projection.m * xe + projection.b]], symbol: "none",
                lineStyle: { color: t.muted, type: [3, 4], width: 1.5 }, tooltip: { show: false },
                markPoint:
                  cross !== null && warn !== null && cross <= xmax
                    ? { symbol: "circle", symbolSize: 10, silent: true, itemStyle: { color: t.warning, borderColor: t.surface, borderWidth: 2 }, label: { show: true, position: "top", formatter: `≈ ${Math.round(cross)} days`, color: t.ink2, fontSize: 11 }, data: [{ coord: [cross, warn] }] }
                    : undefined,
              },
            ]
          : []),
      ],
    } as EChartsCoreOption;
  }, [daily, projection, warn, normal, unit, t]);
  return <EChart option={option} height={230} ariaLabel="Daily vibration trend and projection" />;
}

// ---------------------------------------------------------------- simple bar / line series

export function IntervalChart({ points, timeoutS }: { points: [number, number][]; timeoutS: number }) {
  const t = useTokens();
  const option = useMemo(
    () => ({
      animation: false,
      grid: { left: 52, right: 16, top: 14, bottom: 26 },
      xAxis: { type: "time", ...axisCommon(t), axisLabel: { ...axisCommon(t).axisLabel, hideOverlap: true, formatter: (v: number) => fmtT(v, false) } },
      yAxis: { type: "log", min: 10, max: 10 ** Math.ceil(Math.log10(Math.max(timeoutS * 2000, ...points.map(([, ms]) => ms * 1.5)))), ...axisCommon(t), axisLabel: { ...axisCommon(t).axisLabel, formatter: (v: number) => (v >= 1000 ? `${+(v / 1000).toPrecision(3)} s` : `${+v.toPrecision(3)} ms`) } },
      tooltip: { trigger: "axis", ...tooltipBase(t), valueFormatter: (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`) },
      series: [
        {
          name: "Time since previous record", type: "scatter", symbolSize: 4,
          data: points.map(([ts, ms]) => [ts * 1000, Math.max(10, ms)]), itemStyle: { color: t.blue, opacity: 0.7 },
          markLine: { symbol: "none", silent: true, data: [{ yAxis: timeoutS * 1000, name: "comms timeout", lineStyle: { color: t.comms, type: "dashed" } }], label: { color: t.muted, fontSize: 10, formatter: "{b}", position: "insideEndTop" } },
        },
      ],
    }) as EChartsCoreOption,
    [points, timeoutS, t],
  );
  return <EChart option={option} height={200} ariaLabel="Update interval between records" />;
}

export function HourBars({ hours }: { hours: { h: number; count: number }[] }) {
  const t = useTokens();
  const option = useMemo(
    () => ({
      animation: false,
      grid: { left: 36, right: 12, top: 10, bottom: 24 },
      xAxis: { type: "time", ...axisCommon(t), axisLabel: { ...axisCommon(t).axisLabel, hideOverlap: true, formatter: (v: number) => fmtT(v, false) } },
      yAxis: { type: "value", minInterval: 1, ...axisCommon(t) },
      tooltip: { trigger: "axis", ...tooltipBase(t), valueFormatter: (v: number) => `${v} alarms` },
      series: [{ type: "bar", data: hours.map((h) => [h.h, h.count]), barMaxWidth: 18, itemStyle: { color: t.blue, borderRadius: [3, 3, 0, 0] } }],
    }) as EChartsCoreOption,
    [hours, t],
  );
  return <EChart option={option} height={170} ariaLabel="Alarms raised per hour" />;
}
