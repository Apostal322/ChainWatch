import { useEffect, useState } from "react";
import { api, type Info } from "./lib/api";
import ReplayView from "./components/ReplayView";
import PerformanceView from "./components/PerformanceView";
import ModelsView from "./components/ModelsView";
import AboutView from "./components/AboutView";
import LiveView from "./components/LiveView";

const TABS = ["Replay", "Performance", "Models & experiments", "Limitations", "Live monitor"] as const;
type Tab = (typeof TABS)[number];

export default function App() {
  const [info, setInfo] = useState<Info | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("Replay");

  useEffect(() => {
    api.info().then(setInfo).catch((e) => setError(String(e.message ?? e)));
  }, []);

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
      {error && <p className="err">API unavailable: {error}</p>}
      <nav className="tabs" role="tablist">
        {TABS.map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
      </nav>
      {info && tab === "Replay" && <ReplayView info={info} />}
      {info && tab === "Performance" && <PerformanceView info={info} />}
      {info && tab === "Models & experiments" && <ModelsView />}
      {info && tab === "Limitations" && <AboutView info={info} />}
      {info && live && <LiveView info={info} />}
      <footer className="muted" style={{ marginTop: 24, fontSize: 12 }}>
        {info && `API v${info.service_version} · ${info.n_models} model versions · commit ${info.git_commit?.slice(0, 10) ?? "n/a"}`}
      </footer>
    </div>
  );
}
