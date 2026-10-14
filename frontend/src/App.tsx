import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { api, type Info } from "./lib/api";
import ReplayView from "./components/ReplayView";
import PerformanceView from "./components/PerformanceView";
import ModelsView from "./components/ModelsView";
import AboutView from "./components/AboutView";
import LiveView from "./components/LiveView";

const TABS = ["Replay", "Performance", "Models & experiments", "Limitations", "Live monitor"] as const;
type Tab = (typeof TABS)[number];
const slug = (t: Tab) => t.toLowerCase().replace(/[^a-z]+/g, "-");

export default function App() {
  const [info, setInfo] = useState<Info | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("Replay");
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);

  useEffect(() => {
    api.info().then(setInfo).catch((e) => setError(String(e.message ?? e)));
  }, []);

  useEffect(() => {
    document.title = `${tab} · ChainWatch`;
  }, [tab]);

  // WAI-ARIA tabs: arrow keys / Home / End move between tabs (automatic activation).
  const onTabKey = (e: KeyboardEvent<HTMLButtonElement>, i: number) => {
    const n = TABS.length;
    const j =
      e.key === "ArrowRight" ? (i + 1) % n
      : e.key === "ArrowLeft" ? (i - 1 + n) % n
      : e.key === "Home" ? 0
      : e.key === "End" ? n - 1
      : null;
    if (j === null) return;
    e.preventDefault();
    setTab(TABS[j]);
    tabRefs.current[j]?.focus();
  };

  const live = tab === "Live monitor";
  const synthetic = info?.data_mode === "synthetic" && !live;
  return (
    <div className="app">
      <header className="top">
        <h1>ChainWatch</h1>
        <span className="sub">
          {live
            ? "Live Bitcoin mempool monitor"
            : "Historical replay of illicit-transaction risk ranking on Elliptic++"}
        </span>
        {info && !live && (
          <span className={`badge ${synthetic ? "synthetic" : ""}`}>
            {synthetic ? "SYNTHETIC DEMO DATA" : "Elliptic++ · historical replay"}
          </span>
        )}
        {info && !live && <span className="badge">label latency L = {info.label_latency_steps} step</span>}
      </header>
      {synthetic && (
        <div className="banner" role="status">
          Synthetic demo: random data with the Elliptic++ schema. Every number on this page describes
          that synthetic data, not real Bitcoin activity. Research results under “Models &amp;
          experiments” come from committed reports on the real dataset.
        </div>
      )}
      {!live && (
        <div className="notice">
          Replay of a published, historical dataset: not live blockchain monitoring. Scores rank
          transactions by similarity to past illicit labels. They are not verdicts and not calibrated
          probabilities. “Unknown” transactions are unlabelled, never assumed licit.
        </div>
      )}
      {error && <p className="err" role="alert">API unavailable: {error}</p>}
      <nav className="tabs" role="tablist" aria-label="Dashboard sections">
        {TABS.map((t, i) => (
          <button key={t} ref={(el) => { tabRefs.current[i] = el; }} type="button" role="tab"
                  id={`tab-${slug(t)}`} aria-controls={`panel-${slug(t)}`} aria-selected={tab === t}
                  tabIndex={tab === t ? 0 : -1} onClick={() => setTab(t)} onKeyDown={(e) => onTabKey(e, i)}>
            {t}
          </button>
        ))}
      </nav>
      {TABS.map((t) => (
        <div key={t} role="tabpanel" id={`panel-${slug(t)}`} aria-labelledby={`tab-${slug(t)}`}
             hidden={tab !== t} tabIndex={tab === t ? 0 : -1} className="tabpanel">
          {tab === t && !info && !error && <p className="muted">Loading…</p>}
          {tab === t && info && t === "Replay" && <ReplayView info={info} />}
          {tab === t && info && t === "Performance" && <PerformanceView info={info} />}
          {tab === t && info && t === "Models & experiments" && <ModelsView />}
          {tab === t && info && t === "Limitations" && <AboutView info={info} />}
          {tab === t && info && t === "Live monitor" && <LiveView info={info} />}
        </div>
      ))}
      <footer className="muted" style={{ marginTop: 24, fontSize: 12 }}>
        {info && `API v${info.service_version} · ${info.n_models} model versions · commit ${info.git_commit?.slice(0, 10) ?? "n/a"}`}
      </footer>
    </div>
  );
}
