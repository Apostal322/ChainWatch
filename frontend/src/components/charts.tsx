import type { ReactNode } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

export interface Series { key: string; label: string; color: string; dashed?: boolean }

interface TipProps { active?: boolean; label?: number | string;
  payload?: { dataKey?: unknown; value?: unknown; color?: string }[]; series: Series[];
  fmt: (v: number) => string }

function Tip({ active, label, payload, series, fmt }: TipProps) {
  if (!active || !payload?.length) return null;
  return (
    <div className="chart-tooltip">
      <div className="muted">Step {label}</div>
      {series.map((s) => {
        const p = payload.find((x) => x.dataKey === s.key);
        const v = p?.value;
        return (
          <div className="row" key={s.key}>
            <span className="key" style={{ background: s.color }} />
            <b>{typeof v === "number" ? fmt(v) : "—"}</b>
            <span className="muted">{s.label}</span>
          </div>
        );
      })}
    </div>
  );
}

/** One-axis line chart with crosshair tooltip listing every series (dataviz rules). */
export function StepLineChart(props: {
  data: Record<string, number | null>[];
  series: Series[];
  yLabel: string;
  yDomain?: [number, number];
  fmt?: (v: number) => string;
  height?: number;
  footer?: ReactNode;
}) {
  const fmt = props.fmt ?? ((v: number) => v.toFixed(3));
  return (
    <div>
      <ResponsiveContainer width="100%" height={props.height ?? 280}>
        <LineChart data={props.data} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
          <CartesianGrid stroke="var(--grid)" vertical={false} />
          <XAxis dataKey="step" stroke="var(--axis)" tick={{ fill: "var(--muted)", fontSize: 11 }}
                 tickLine={false} />
          <YAxis domain={props.yDomain ?? ["auto", "auto"]} stroke="var(--axis)" tickLine={false}
                 tick={{ fill: "var(--muted)", fontSize: 11 }} width={44}
                 label={{ value: props.yLabel, angle: -90, position: "insideLeft", fill: "var(--ink-2)", fontSize: 11 }} />
          <Tooltip cursor={{ stroke: "var(--axis)" }}
                   content={(p) => <Tip {...(p as unknown as TipProps)} series={props.series} fmt={fmt} />} />
          {/* legend text in the body ink: the series colours alone fail contrast (1.5-2.7:1) */}
          <Legend wrapperStyle={{ fontSize: 12 }} iconType="plainline"
                  formatter={(value) => <span style={{ color: "var(--ink-2)" }}>{value}</span>} />
          {props.series.map((s) => (
            <Line key={s.key} type="linear" dataKey={s.key} name={s.label} stroke={s.color}
                  strokeWidth={2} strokeDasharray={s.dashed ? "5 4" : undefined}
                  dot={{ r: 3 }} activeDot={{ r: 5 }} connectNulls={false} isAnimationActive={false} />
          ))}
        </LineChart>
      </ResponsiveContainer>
      {props.footer}
    </div>
  );
}
