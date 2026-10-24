"""Typed experiment configuration loaded from YAML.

Dataclasses (rather than raw dicts) give us a single place where config keys are
validated, so a typo in a YAML file fails loudly at start-up instead of silently
falling back to a default deep inside training.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "default.yaml"
RAW_DIR_ENV_VAR = "CHAINWATCH_RAW_DIR"

FEATURE_SETS = ("local", "local_aggregate", "all", "augmented", "live_computable")
THRESHOLD_STRATEGIES = ("max_f1", "fixed")


def _resolve(path: str | Path) -> Path:
    """Resolve a config path relative to the repository root (not the CWD)."""
    p = Path(path).expanduser()
    return p if p.is_absolute() else (REPO_ROOT / p).resolve()


@dataclass
class PathsConfig:
    raw_dir: Path
    processed_dir: Path
    artifacts_dir: Path
    reports_dir: Path
    backtests_dir: Path

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PathsConfig:
        raw = os.environ.get(RAW_DIR_ENV_VAR) or d["raw_dir"]
        return cls(
            raw_dir=_resolve(raw),
            processed_dir=_resolve(d["processed_dir"]),
            artifacts_dir=_resolve(d["artifacts_dir"]),
            reports_dir=_resolve(d["reports_dir"]),
            backtests_dir=_resolve(d.get("backtests_dir", "artifacts/backtests")),
        )


@dataclass
class SplitConfig:
    train: tuple[int, int]
    val: tuple[int, int]
    test: tuple[int, int]

    def __post_init__(self) -> None:
        self.train, self.val, self.test = (tuple(x) for x in (self.train, self.val, self.test))
        for name, (lo, hi) in self.ranges().items():
            if lo > hi:
                raise ValueError(f"split.{name}: start {lo} > end {hi}")
        # Strict chronological ordering is the core anti-leakage guarantee.
        if not (self.train[1] < self.val[0] and self.val[1] < self.test[0]):
            raise ValueError(
                "Splits must be chronological and non-overlapping: "
                f"train={self.train}, val={self.val}, test={self.test}"
            )

    def ranges(self) -> dict[str, tuple[int, int]]:
        return {"train": self.train, "val": self.val, "test": self.test}


@dataclass
class PreprocessingConfig:
    log1p_augmented: bool = True
    recompute_degrees: bool = True


@dataclass
class EvaluationConfig:
    threshold_strategy: str = "max_f1"
    fixed_threshold: float = 0.5
    k_values: list[int] = field(default_factory=lambda: [50, 100, 200, 500])
    per_step_k: int = 20
    latency_repeats: int = 200

    def __post_init__(self) -> None:
        if self.threshold_strategy not in THRESHOLD_STRATEGIES:
            raise ValueError(f"threshold_strategy must be one of {THRESHOLD_STRATEGIES}")


@dataclass
class TrackingConfig:
    mlflow: bool = False
    experiment_name: str = "chainwatch"
    tracking_uri: str = "sqlite:///mlflow.db"


@dataclass
class BacktestStage:
    origin: int  # last step whose end is the first deployment time
    end: int  # last scored step (inclusive)
    seeds: list[int]

    def __post_init__(self) -> None:
        if self.end <= self.origin:
            raise ValueError(
                f"backtest stage end ({self.end}) must be after origin ({self.origin})"
            )
        if not self.seeds:
            raise ValueError("backtest stage needs at least one seed")

    @property
    def scored_steps(self) -> list[int]:
        return list(range(self.origin + 1, self.end + 1))


@dataclass
class BootstrapConfig:
    n_resamples: int = 2000
    block_length: int = 3
    seed: int = 0


@dataclass
class BacktestConfig:
    """Rolling-origin backtest settings (Phase 2). See docs/PHASE2_PROTOCOL.md."""

    label_latency: int
    latency_sensitivity: list[int]
    feature_set: str
    xgboost: dict[str, Any]
    strategies: dict[str, dict[str, Any]]
    stages: dict[str, BacktestStage]
    budgets: list[int]
    primary_budget: int
    selection_tolerance: float
    reference_prevalence: float
    bootstrap: BootstrapConfig
    confirm_split_after: int = 42  # confirm sub-periods: (.., split] and (split, ..]
    time_proxy_threshold: float = 0.9
    time_proxy_max_step: int = 34

    def __post_init__(self) -> None:
        if self.label_latency < 0:
            raise ValueError("label_latency must be >= 0")
        if self.feature_set not in FEATURE_SETS:
            raise ValueError(f"backtest.feature_set must be one of {FEATURE_SETS}")
        if self.primary_budget not in self.budgets:
            raise ValueError("primary_budget must be one of budgets")
        if "dev" in self.stages and "confirm" in self.stages:
            dev, conf = self.stages["dev"], self.stages["confirm"]
            if dev.end > conf.origin:
                raise ValueError("dev backtest must end before the confirm stage starts scoring")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BacktestConfig:
        return cls(
            label_latency=int(d["label_latency"]),
            latency_sensitivity=[int(x) for x in d.get("latency_sensitivity", [])],
            feature_set=d.get("feature_set", "all"),
            xgboost=dict(d["xgboost"]),
            strategies=dict(d["strategies"]),
            stages={k: BacktestStage(**v) for k, v in d["stages"].items()},
            budgets=[int(k) for k in d["budgets"]],
            primary_budget=int(d["primary_budget"]),
            selection_tolerance=float(d.get("selection_tolerance", 0.0)),
            reference_prevalence=float(d.get("reference_prevalence", 0.1)),
            bootstrap=BootstrapConfig(**d.get("bootstrap", {})),
            confirm_split_after=int(d.get("confirm_split_after", 42)),
            time_proxy_threshold=float(d.get("time_proxy_threshold", 0.9)),
            time_proxy_max_step=int(d.get("time_proxy_max_step", 34)),
        )


@dataclass
class ServingConfig:
    """Phase 3 inference service. See docs/INFERENCE_CONTRACT.md."""

    registry_dir: Path
    snapshot_seed: int = 0
    snapshot_first_tau: int = 19
    snapshot_last_tau: int = 48
    max_batch_size: int = 1000
    allow_version_mismatch: bool = False
    frontend_dist: Path | None = None
    cors_origins: list[str] = field(default_factory=list)
    explain_top_k: int = 10
    # public-exposure controls (docs/DEPLOYMENT.md)
    max_body_bytes: int = 6_000_000
    rate_limit_per_minute: int = 0  # 0 = off; per client address, /v1 routes only
    trusted_proxy_hops: int = 0  # proxies appending to X-Forwarded-For; 0 = ignore it
    expose_docs: bool = True  # /docs and /redoc load third-party JS (Swagger UI / ReDoc)
    limit_concurrency: int = 64

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ServingConfig:
        d = dict(d)
        d["registry_dir"] = _resolve(d.get("registry_dir", "artifacts/registry"))
        if d.get("frontend_dist"):
            d["frontend_dist"] = _resolve(d["frontend_dist"])
        return cls(**d)


@dataclass
class LiveConfig:
    """Phase 5 live Bitcoin monitor (docs/LIVE_MONITOR.md). Disabled unless opted in.

    ``enabled`` only exposes the read-only /v1/live API over ``db_path``; the collector that
    talks to mempool.space is a separate process (``chainwatch live-collect``).
    """

    enabled: bool = False
    db_path: Path = field(default_factory=lambda: _resolve("data/live/live.sqlite"))
    ws_url: str = "wss://mempool.space/api/v1/ws"
    rest_base: str = "https://mempool.space/api"
    explorer_tx_url: str = "https://mempool.space/tx/"
    sample_rate: float = 0.05  # fraction of announced txids whose details are fetched
    fetch_per_minute: int = 30  # REST detail requests per minute (hard cap)
    fetch_burst: int = 5
    fetch_concurrency: int = 2
    request_timeout_s: float = 10.0
    queue_max: int = 200  # pending detail fetches; newer txids are dropped when full
    max_queue_age_s: float = 300.0  # skip queued txids older than this (no stale backfill)
    # transient detail-fetch failures (timeout, network, 429, 5xx, a first 404) are retried
    # with exponential backoff and jitter; every attempt draws from the same fetch budget
    fetch_max_attempts: int = 3  # 1 = never retry
    fetch_retry_base_s: float = 5.0  # first retry after 2.5-5 s, then 5-10 s, ...
    dedupe_max: int = 200_000  # txids remembered for deduplication (bounded LRU)
    retention_rows: int = 20_000  # stored transactions kept in SQLite
    retention_days: float = 7.0  # and none older than this
    reconnect_base_s: float = 1.0
    reconnect_max_s: float = 60.0
    stale_after_s: float = 90.0  # reconnect if no message arrives for this long
    heartbeat_s: float = 5.0
    sse_max_clients: int = 20
    sse_max_seconds: float = 300.0  # clients reconnect automatically after this
    sse_max_per_client: int = 2  # per client address (a tab or two); the rest get 429
    # Risk intelligence (docs/RISK_ENGINE.md). Screening needs indicator sources loaded with
    # `chainwatch-live indicators update`; a source not refreshed for this long is "stale".
    risk_enabled: bool = True
    indicator_max_age_days: float = 7.0
    # Remote collector (docs/LIVE_CLOUD.md). The edge API on the collector VM requires a
    # bearer token read from the environment variable named here (never from the config);
    # a dashboard with ``remote_url`` set proxies /v1/live/* to that edge API with it.
    api_token_env: str = "CHAINWATCH_LIVE_TOKEN"
    remote_url: str | None = None
    remote_timeout_s: float = 5.0
    remote_cache_s: float = 2.0
    edge_rate_limit_per_minute: int = 240  # per client on the edge API (dashboards poll it)
    edge_trusted_proxy_hops: int = 1  # Caddy terminates TLS in front of the edge API

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LiveConfig:
        d = dict(d)
        if "db_path" in d:
            d["db_path"] = _resolve(d["db_path"])
        url = d.get("remote_url")
        if isinstance(url, str) and url.startswith("env:"):  # deployment-specific, not in Git
            d["remote_url"] = os.environ.get(url[4:]) or None
            if d["remote_url"] is None and d.get("enabled"):
                raise ValueError(f"live.remote_url: environment variable {url[4:]} is not set")
        return cls(**d)

    def __post_init__(self) -> None:
        """Reject syntactically valid but unsafe values (e.g. ``retention_rows: 0`` made every
        prune delete all stored rows; ``heartbeat_s: 0`` made the heartbeat loop spin)."""
        if not 0.0 < self.sample_rate <= 1.0:
            raise ValueError("live.sample_rate must be in (0, 1]")
        if self.fetch_max_attempts > 10:
            raise ValueError("live.fetch_max_attempts must be at most 10")
        at_least_one = ("fetch_per_minute", "fetch_burst", "fetch_concurrency", "queue_max",
                        "fetch_max_attempts", "dedupe_max", "retention_rows", "sse_max_per_client",
                        "edge_rate_limit_per_minute")  # fmt: skip
        positive = ("request_timeout_s", "max_queue_age_s", "retention_days", "reconnect_base_s",
                    "reconnect_max_s", "stale_after_s", "heartbeat_s", "sse_max_seconds",
                    "indicator_max_age_days", "remote_timeout_s",
                    "fetch_retry_base_s")  # fmt: skip
        for name in at_least_one:
            if getattr(self, name) < 1:
                raise ValueError(f"live.{name} must be at least 1")
        for name in positive:
            if not getattr(self, name) > 0:
                raise ValueError(f"live.{name} must be positive")
        # sse_max_clients: 0 turns the event stream off (503); the dashboard keeps polling
        if self.remote_cache_s < 0 or self.edge_trusted_proxy_hops < 0 or self.sse_max_clients < 0:
            raise ValueError(
                "live.remote_cache_s, edge_trusted_proxy_hops and sse_max_clients must be >= 0"
            )
        if self.reconnect_max_s < self.reconnect_base_s:
            raise ValueError("live.reconnect_max_s must be >= live.reconnect_base_s")
        if not self.ws_url.startswith(("ws://", "wss://")):
            raise ValueError("live.ws_url must be a ws:// or wss:// URL")
        for name in ("rest_base", "explorer_tx_url"):
            if not getattr(self, name).startswith(("http://", "https://")):
                raise ValueError(f"live.{name} must be an http:// or https:// URL")


@dataclass
class Config:
    seed: int
    paths: PathsConfig
    feature_set: str
    split: SplitConfig
    preprocessing: PreprocessingConfig
    evaluation: EvaluationConfig
    models: dict[str, dict[str, Any]]
    tracking: TrackingConfig
    backtest: BacktestConfig | None = None
    serving: ServingConfig | None = None
    live: LiveConfig = field(default_factory=LiveConfig)
    source_path: Path | None = None

    def __post_init__(self) -> None:
        if self.feature_set not in FEATURE_SETS:
            raise ValueError(f"data.feature_set must be one of {FEATURE_SETS}")

    def model_params(self, name: str) -> dict[str, Any]:
        if name not in self.models:
            raise KeyError(f"No config for model '{name}'. Available: {sorted(self.models)}")
        return self.models[name]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["paths"] = {k: str(v) for k, v in d["paths"].items()}
        d["source_path"] = str(self.source_path) if self.source_path else None
        for section in ("serving", "live"):
            if d.get(section):
                d[section] = {
                    k: str(v) if isinstance(v, Path) else v for k, v in d[section].items()
                }
        return d


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        out[k] = (
            _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
        )
    return out


def _read_yaml(path: Path, _depth: int = 0) -> dict[str, Any]:
    """Read YAML; ``extends: other.yaml`` (relative to this file) is deep-merged first."""
    if _depth > 5:
        raise ValueError("config `extends` chain too deep")
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    parent = raw.pop("extends", None)
    if parent:
        raw = _deep_merge(_read_yaml((path.parent / parent).resolve(), _depth + 1), raw)
    return raw


def load_config(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> Config:
    """Load a YAML config. ``overrides`` supports a few common top-level tweaks."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    raw = _read_yaml(path)
    overrides = overrides or {}
    if overrides.get("feature_set"):
        raw["data"]["feature_set"] = overrides["feature_set"]
    if "seed" in overrides and overrides["seed"] is not None:
        raw["seed"] = int(overrides["seed"])
    if overrides.get("mlflow"):
        raw.setdefault("tracking", {})["mlflow"] = True

    return Config(
        seed=int(raw["seed"]),
        paths=PathsConfig.from_dict(raw["paths"]),
        feature_set=raw["data"]["feature_set"],
        split=SplitConfig(**raw["split"]),
        preprocessing=PreprocessingConfig(**raw.get("preprocessing", {})),
        evaluation=EvaluationConfig(**raw.get("evaluation", {})),
        models=raw.get("models", {}),
        tracking=TrackingConfig(**raw.get("tracking", {})),
        backtest=BacktestConfig.from_dict(raw["backtest"]) if raw.get("backtest") else None,
        serving=ServingConfig.from_dict(raw["serving"]) if raw.get("serving") else None,
        live=LiveConfig.from_dict(raw.get("live") or {}),
        source_path=path.resolve(),
    )
