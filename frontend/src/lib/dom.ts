/** Width below which `.split` stacks its columns (keep in sync with styles.css). */
export const STACKED_QUERY = "(max-width: 980px)";

/**
 * When the layout is stacked (detail panel below a long table), bring the panel into view after a
 * selection; otherwise a tap on a row has no visible effect on a phone.
 */
export function revealIfStacked(el: HTMLElement | null): void {
  if (!el || typeof window === "undefined" || !window.matchMedia?.(STACKED_QUERY).matches) return;
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  el.scrollIntoView({ block: "start", behavior: reduce ? "auto" : "smooth" });
}
