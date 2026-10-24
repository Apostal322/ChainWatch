import { useSyncExternalStore } from "react";

// One shared 1 s clock for every "x seconds ago" cell, so only those cells re-render each second
// (not the whole view, its tables and charts).
const listeners = new Set<() => void>();
let now = Date.now() / 1000;
let timer: ReturnType<typeof setInterval> | null = null;

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  if (timer === null) {
    now = Date.now() / 1000;
    timer = setInterval(() => {
      now = Date.now() / 1000;
      listeners.forEach((l) => l());
    }, 1000);
  }
  return () => {
    listeners.delete(cb);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

const snapshot = () => now;

/** Current time in seconds, updated once per second while any component uses it. */
export function useNow(): number {
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}
