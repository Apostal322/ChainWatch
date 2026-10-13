import { useMemo } from "react";
import { forceCollide, forceLink, forceManyBody, forceRadial, forceSimulation,
  type SimulationLinkDatum, type SimulationNodeDatum } from "d3-force";
import type { Neighbourhood, NbNode } from "../lib/api";
import { fmtScore } from "../lib/format";

type N = NbNode & SimulationNodeDatum;
type L = SimulationLinkDatum<N>;

const W = 460, H = 320;

/** Static force layout (computed synchronously, deterministic start) of a 1-2 hop neighbourhood. */
export default function NeighbourhoodGraph(props: { data: Neighbourhood; onSelect: (id: number) => void }) {
  const { nodes, links } = useMemo(() => {
    const nodes: N[] = props.data.nodes.map((n, i) => ({
      ...n,
      x: W / 2 + 60 * Math.cos(i) * n.hop,
      y: H / 2 + 60 * Math.sin(i) * n.hop,
    }));
    const byId = new Map(nodes.map((n) => [n.tx_id, n]));
    const links: L[] = props.data.edges
      .filter((e) => byId.has(e.source) && byId.has(e.target))
      .map((e) => ({ source: byId.get(e.source)!, target: byId.get(e.target)! }));
    const center = byId.get(props.data.center);
    if (center) { center.fx = W / 2; center.fy = H / 2; }
    forceSimulation(nodes)
      .force("link", forceLink<N, L>(links).distance(70).strength(0.2))
      .force("charge", forceManyBody().strength(-90))
      .force("collide", forceCollide(15))
      // concentric rings: hop 1 around the centre, hop 2 further out
      .force("radial", forceRadial<N>((d) => d.hop * 68, W / 2, H / 2).strength(0.9))
      .stop()
      .tick(300);
    for (const n of nodes) {
      n.x = Math.max(14, Math.min(W - 14, n.x ?? 0));
      n.y = Math.max(14, Math.min(H - 14, n.y ?? 0));
    }
    return { nodes, links };
  }, [props.data]);

  const ring = (n: NbNode) =>
    n.label_status === "illicit" ? "var(--s2)" : n.label_status === "licit" ? "var(--ink-2)" : "var(--muted)";
  return (
    <div>
      <svg role="img" aria-label="Transaction graph neighbourhood" viewBox={`0 0 ${W} ${H}`} width="100%"
           style={{ maxWidth: W, background: "var(--surface-2)", borderRadius: 8 }}>
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="17" refY="5" markerWidth="6" markerHeight="6" orient="auto">
            <path d="M0,0 L10,5 L0,10 z" fill="var(--axis)" />
          </marker>
        </defs>
        {links.map((l, i) => {
          const s = l.source as N, t = l.target as N;
          return <line key={i} x1={s.x} y1={s.y} x2={t.x} y2={t.y} stroke="var(--axis)" strokeWidth={1.2}
                       markerEnd="url(#arrow)" />;
        })}
        {nodes.map((n) => {
          const r = n.tx_id === props.data.center ? 11 : 8;
          return (
            <g key={n.tx_id} transform={`translate(${n.x},${n.y})`} style={{ cursor: "pointer" }}
               onClick={() => props.onSelect(n.tx_id)} tabIndex={0}
               onKeyDown={(e) => e.key === "Enter" && props.onSelect(n.tx_id)}>
              <title>{`tx ${n.tx_id} · hop ${n.hop} · score ${fmtScore(n.risk_score)} · rank ${n.rank} · ${n.label_status}`}</title>
              <circle r={14} fill="transparent" />
              <circle r={r} fill="var(--s1)" fillOpacity={0.12 + 0.88 * n.risk_score}
                      stroke={ring(n)} strokeWidth={n.label_status === "illicit" ? 3 : 1.5}
                      strokeDasharray={n.label_status === "unknown" ? "3 2" : undefined} />
            </g>
          );
        })}
      </svg>
      <div className="legend">
        <span><i style={{ background: "var(--s1)", opacity: 0.25 }} />low risk</span>
        <span><i style={{ background: "var(--s1)" }} />high risk (fill = score)</span>
        <span><i style={{ border: "3px solid var(--s2)" }} />labelled illicit</span>
        <span><i style={{ border: "1.5px solid var(--ink-2)" }} />labelled licit</span>
        <span><i style={{ border: "1.5px dashed var(--muted)" }} />unknown</span>
      </div>
      <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>
        {props.data.note}{props.data.truncated ? " Neighbourhood truncated to 80 nodes." : ""} Click a node to inspect it.
      </p>
    </div>
  );
}
