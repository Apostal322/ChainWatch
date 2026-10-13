import type { Contribution } from "../lib/api";
import { fmtScore } from "../lib/format";

/** Diverging horizontal bars of TreeSHAP contributions (log-odds); red raises, blue lowers risk. */
export default function ExplanationChart({ items }: { items: Contribution[] }) {
  const max = Math.max(...items.map((c) => Math.abs(c.contribution)), 1e-9);
  const rowH = 22, labelW = 150, valueW = 54, width = 460;
  const half = (width - labelW - valueW) / 2;
  const zero = labelW + half;
  return (
    <svg role="img" aria-label="Feature contributions to the risk score"
         viewBox={`0 0 ${width} ${items.length * rowH + 18}`} width="100%" style={{ maxWidth: width }}>
      <line x1={zero} x2={zero} y1={0} y2={items.length * rowH} stroke="var(--axis)" />
      {items.map((c, i) => {
        const w = (Math.abs(c.contribution) / max) * (half - 4);
        const x = c.contribution >= 0 ? zero : zero - w;
        const y = i * rowH;
        return (
          <g key={c.feature}>
            <title>{`${c.feature}: contribution ${c.contribution.toFixed(3)} (value ${fmtScore(c.value)})`}</title>
            <text x={labelW - 6} y={y + 15} textAnchor="end" fontSize="11.5" fill="var(--ink-2)">{c.feature}</text>
            <rect x={x} y={y + 5} width={Math.max(w, 1)} height={12} rx={3}
                  fill={c.contribution >= 0 ? "var(--pos)" : "var(--neg)"} />
            <text x={width - 4} y={y + 15} textAnchor="end" fontSize="11.5" fill="var(--ink)"
                  style={{ fontVariantNumeric: "tabular-nums" }}>
              {c.contribution >= 0 ? "+" : ""}{c.contribution.toFixed(2)}
            </text>
          </g>
        );
      })}
      <text x={zero} y={items.length * rowH + 14} textAnchor="middle" fontSize="10.5" fill="var(--muted)">
        ← lowers score · log-odds · raises score →
      </text>
    </svg>
  );
}
