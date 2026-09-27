import { useEffect, useRef, useState } from "react";
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import { DataZoomComponent, GridComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { DataType } from "../api";

echarts.use([LineChart, GridComponent, TooltipComponent, DataZoomComponent, CanvasRenderer]);

type Point = [number, number | null, number | null, number | null]; // t, avg, min, max

interface Tokens {
  series: string;
  ink: string;
  ink2: string;
  muted: string;
  grid: string;
  axis: string;
  surface: string;
}

function readTokens(): Tokens {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string) => css.getPropertyValue(name).trim();
  return {
    series: v("--series-1"),
    ink: v("--ink"),
    ink2: v("--ink-2"),
    muted: v("--ink-muted"),
    grid: v("--grid"),
    axis: v("--axis"),
    surface: v("--surface"),
  };
}

/** Theme tokens that update on OS theme changes and on the in-app toggle (data-theme). */
function useTokens(): Tokens {
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

/** Insert nulls where samples are missing so the line breaks instead of bridging an outage. */
function withGaps(points: Point[], bucketMs: number, expectedMs: number): Point[] {
  const maxGap = Math.max(bucketMs, expectedMs) * 3;
  const out: Point[] = [];
  for (let i = 0; i < points.length; i++) {
    if (i > 0 && points[i][0] - points[i - 1][0] > maxGap) {
      out.push([points[i - 1][0] + bucketMs, null, null, null]);
    }
    out.push(points[i]);
  }
  return out;
}

export interface TrendChartProps {
  points: Point[];
  from: number;
  to: number;
  bucketS: number;
  expectedIntervalS: number;
  dataType: DataType;
  unit: string;
  decimals: number;
  group: string;
  label: string;
}

export function TrendChart(props: TrendChartProps) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const tokens = useTokens();

  useEffect(() => {
    if (!el.current) return;
    const c = echarts.init(el.current, undefined, { renderer: "canvas" });
    chart.current = c;
    const ro = new ResizeObserver(() => c.resize());
    ro.observe(el.current);
    return () => {
      ro.disconnect();
      c.dispose();
      chart.current = null;
    };
  }, []);

  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    const { points, from, to, bucketS, expectedIntervalS, dataType, unit, decimals, group, label } = props;
    const isBool = dataType === "boolean";
    const data = withGaps(points, bucketS * 1000, expectedIntervalS * 1000);
    const showBand = !isBool && bucketS > 1;
    const fmt = (v: number) =>
      isBool ? (v >= 0.5 ? "ON" : "OFF") : v.toLocaleString(undefined, { maximumFractionDigits: decimals });

    c.group = group;
    echarts.connect(group);
    c.setOption(
      {
        animation: false,
        grid: { left: 8, right: 16, top: 12, bottom: 24, containLabel: true },
        xAxis: {
          type: "time",
          min: from,
          max: to,
          axisLine: { lineStyle: { color: tokens.axis } },
          axisTick: { show: false },
          axisLabel: { color: tokens.muted, hideOverlap: true },
          splitLine: { show: false },
        },
        yAxis: {
          type: "value",
          scale: !isBool,
          min: isBool ? -0.1 : undefined,
          max: isBool ? 1.1 : undefined,
          interval: isBool ? 1 : undefined,
          splitNumber: 4,
          axisLabel: {
            color: tokens.muted,
            formatter: (v: number) => (isBool ? (v === 1 ? "ON" : v === 0 ? "OFF" : "") : fmt(v)),
          },
          splitLine: { lineStyle: { color: tokens.grid, width: 1 } },
        },
        dataZoom: [{ type: "inside", xAxisIndex: 0, filterMode: "none" }],
        tooltip: {
          trigger: "axis",
          backgroundColor: tokens.surface,
          borderColor: tokens.axis,
          textStyle: { color: tokens.ink, fontSize: 12 },
          axisPointer: { type: "line", lineStyle: { color: tokens.axis } },
          formatter: (params: unknown) => {
            const list = params as { seriesId: string; value: Point; axisValue: number }[];
            const p = list.find((x) => x.seriesId === "avg");
            if (!p || p.value[1] === null) return "";
            const [t, avg, min, max] = p.value;
            const when = new Date(t).toLocaleString();
            const u = unit && !isBool ? ` ${unit}` : "";
            let html = `<div style="color:${tokens.ink2}">${when}</div><div><b>${label}</b>: ${fmt(avg as number)}${u}`;
            if (bucketS > 1) html += ` <span style="color:${tokens.ink2}">avg of ${bucketS}s</span>`;
            html += "</div>";
            if (showBand && min !== null && max !== null && min !== max) {
              html += `<div style="color:${tokens.ink2}">range ${fmt(min)} – ${fmt(max)}${u}</div>`;
            }
            return html;
          },
        },
        series: [
          // min/max envelope: invisible base + stacked difference, washed at ~10%.
          {
            id: "band-base",
            type: "line",
            stack: "band",
            symbol: "none",
            silent: true,
            lineStyle: { opacity: 0 },
            data: showBand ? data.map((d) => [d[0], d[2]]) : [],
          },
          {
            id: "band",
            type: "line",
            stack: "band",
            symbol: "none",
            silent: true,
            lineStyle: { opacity: 0 },
            areaStyle: { color: tokens.series, opacity: 0.12 },
            data: showBand ? data.map((d) => [d[0], d[3] !== null && d[2] !== null ? d[3] - d[2] : null]) : [],
          },
          {
            id: "avg",
            type: "line",
            symbol: "none",
            step: isBool ? "end" : false,
            lineStyle: { width: 2, color: tokens.series, cap: "round", join: "round" },
            itemStyle: { color: tokens.series },
            emphasis: { disabled: true },
            data,
            encode: { x: 0, y: 1 },
          },
        ],
      },
      { notMerge: true },
    );
  }, [props, tokens]);

  return <div ref={el} className="chart-box" role="img" aria-label={`${props.label} trend`} />;
}
