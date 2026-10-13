import type { LabelStatus, Policy } from "../lib/api";

export const POLICY_LABEL: Record<Policy, string> = {
  expanding: "Retrain every step",
  static: "Frozen at stage origin",
  phase1: "Phase 1 reference",
};
export const POLICY_COLOR: Record<Policy, string> = {
  expanding: "var(--s1)",
  static: "var(--s2)",
  phase1: "var(--s3)",
};

export function LabelChip({ status }: { status: LabelStatus }) {
  const text = status === "unknown" ? "unknown (unlabelled)" : status;
  return <span className={`chip ${status}`}>{text}</span>;
}

export function PolicyPicker(props: {
  value: Policy;
  available: Policy[];
  onChange: (p: Policy) => void;
}) {
  return (
    <div className="seg" role="group" aria-label="Replay policy">
      {(Object.keys(POLICY_LABEL) as Policy[]).map((p) => (
        <button
          key={p}
          aria-pressed={props.value === p}
          disabled={!props.available.includes(p)}
          title={props.available.includes(p) ? "" : "Not available for this step"}
          onClick={() => props.onChange(p)}
        >
          {POLICY_LABEL[p]}
        </button>
      ))}
    </div>
  );
}

export function Tile(props: { k: string; v: string; h?: string }) {
  return (
    <div className="tile">
      <div className="k">{props.k}</div>
      <div className="v">{props.v}</div>
      {props.h && <div className="h">{props.h}</div>}
    </div>
  );
}
