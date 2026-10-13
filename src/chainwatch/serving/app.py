"""ChainWatch inference service (FastAPI).

Serves risk scores for **historical Elliptic++ transactions** from frozen, versioned model
snapshots (see docs/INFERENCE_CONTRACT.md). There is deliberately no endpoint for
transaction hashes, wallet addresses or live blockchain data.

Run: ``chainwatch serve`` (or ``uvicorn chainwatch.serving.app:create_app --factory``).
The config is read from ``CHAINWATCH_CONFIG`` (default: configs/default.yaml).
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from chainwatch import __version__
from chainwatch.config import REPO_ROOT, Config, load_config
from chainwatch.data import schema as S
from chainwatch.serving.export import REGISTRY_META
from chainwatch.serving.registry import RegistryError, load_registry
from chainwatch.serving.replay import POLICIES, ReplayEngine, ReplayError
from chainwatch.serving.schemas import (
    BatchByIdRequest,
    BatchResponse,
    ErrorResponse,
    FeatureVectorRequest,
    FeatureVectorResponse,
    ItemError,
    Prediction,
)
from chainwatch.serving.security import (
    DASHBOARD_CSP,
    SECURITY_HEADERS,
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    request_id,
)
from chainwatch.serving.store import FeatureStore, UnknownTransactionError, data_fingerprint
from chainwatch.utils import git_commit, read_json

log = logging.getLogger("chainwatch.api")

DISCLAIMER = (
    "Historical replay on the Elliptic++ research dataset. Scores rank transactions by "
    "similarity to transactions labelled illicit in the past; they are not calibrated "
    "probabilities, not evidence of criminal activity, and not valid for arbitrary current "
    "Bitcoin transactions. Unknown-label transactions are never treated as licit."
)
FEATURE_SPACE = (
    "Elliptic++ transaction features: 165 anonymised features standardised by the dataset "
    "publisher plus 17 augmented features. Not computable for new real-world transactions."
)


# --------------------------------------------------------------------------- logging
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(getattr(record, "extra_fields", {}))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_json_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


# --------------------------------------------------------------------------- state
class ServiceState:
    def __init__(self, cfg: Config) -> None:
        sv = cfg.serving
        if sv is None:
            raise RegistryError("config has no `serving` section")
        t0 = time.perf_counter()
        self.cfg = cfg
        self.registry_meta = (
            read_json(sv.registry_dir / REGISTRY_META)
            if (sv.registry_dir / REGISTRY_META).exists()
            else None
        )
        if self.registry_meta is None:
            raise RegistryError(f"no {REGISTRY_META} in {sv.registry_dir}; run export-snapshots")
        fp = data_fingerprint(cfg.paths.processed_dir)
        if fp != self.registry_meta["data_fingerprint"]:
            raise RegistryError(
                f"processed data fingerprint {fp} differs from the data the registry was built on "
                f"({self.registry_meta['data_fingerprint']}); refusing to serve mismatched models"
            )
        self.store = FeatureStore.from_processed(cfg.paths.processed_dir)
        meta = self.registry_meta
        routed = list(meta["snapshots"]) + (
            [meta["phase1_reference"]] if meta.get("phase1_reference") else []
        )
        self.bundles = load_registry(
            sv.registry_dir,
            sv.allow_version_mismatch,
            versions=routed,
            expected_fingerprint=meta["data_fingerprint"],
            expected_latency=int(meta["label_latency"]),
        )
        self.engine = ReplayEngine(self.store, self.bundles, self.registry_meta)
        self.research = research_results(REPO_ROOT / "reports")  # read once, served read-only
        self.started = time.time()
        self.load_seconds = time.perf_counter() - t0
        self.git_commit = git_commit(REPO_ROOT)


def _state(request: Request) -> ServiceState:
    return request.app.state.svc


# --------------------------------------------------------------------------- errors
def _error(status: int, code: str, message: str, request: Request, details: Any = None):
    body = ErrorResponse.model_validate(
        {
            "error": {"code": code, "message": message, "details": details},
            "request_id": getattr(request.state, "request_id", None),
        }
    )
    return JSONResponse(status_code=status, content=body.model_dump())


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        details = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return _error(422, "validation_error", "request validation failed", request, details)

    @app.exception_handler(StarletteHTTPException)  # also covers fastapi.HTTPException
    async def _http(request: Request, exc: StarletteHTTPException):
        code = {
            404: "not_found",
            400: "bad_request",
            405: "method_not_allowed",
            413: "payload_too_large",
        }.get(exc.status_code, "http_error")
        return _error(exc.status_code, code, str(exc.detail), request)

    @app.exception_handler(ReplayError)
    async def _replay(request: Request, exc: ReplayError):
        return _error(400, "replay_unavailable", str(exc), request)

    @app.exception_handler(UnknownTransactionError)
    async def _unknown(request: Request, exc: UnknownTransactionError):
        return _error(
            404, "unknown_transaction", f"transaction(s) not in dataset: {exc.args[0]}", request
        )

    @app.exception_handler(Exception)
    async def _internal(request: Request, exc: Exception):
        log.exception(
            "unhandled error",
            extra={"extra_fields": {"request_id": getattr(request.state, "request_id", None)}},
        )
        return _error(500, "internal_error", "internal server error", request)


# --------------------------------------------------------------------------- app
def create_app(cfg: Config | None = None, state: ServiceState | None = None) -> FastAPI:
    cfg = cfg or load_config(os.environ.get("CHAINWATCH_CONFIG"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.svc = state or ServiceState(cfg)  # fail fast on any artifact problem
        log.info(
            "service ready",
            extra={
                "extra_fields": {
                    "models": len(app.state.svc.bundles),
                    "data_mode": app.state.svc.registry_meta["data_mode"],
                    "load_seconds": round(app.state.svc.load_seconds, 2),
                }
            },
        )
        yield

    sv = cfg.serving
    docs = sv is None or sv.expose_docs
    app = FastAPI(
        title="ChainWatch",
        version=__version__,
        description=DISCLAIMER,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        responses={422: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )
    if cfg.serving and cfg.serving.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.serving.cors_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )
    _install_error_handlers(app)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request_id(request.headers.get("x-request-id"))
        request.state.request_id = rid
        t0 = time.perf_counter()
        response = await call_next(request)
        ms = (time.perf_counter() - t0) * 1e3
        response.headers["x-request-id"] = rid
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        path = request.url.path
        if not path.startswith(("/docs", "/redoc", "/v1", "/health", "/openapi")):
            response.headers.setdefault("content-security-policy", DASHBOARD_CSP)
        if request.url.path.startswith(("/v1", "/health")):
            log.info(
                "request",
                extra={
                    "extra_fields": {
                        "request_id": rid,
                        "method": request.method,
                        "path": request.url.path,
                        "status": response.status_code,
                        "duration_ms": round(ms, 2),
                    }
                },
            )
        return response

    _routes(app)
    _mount_frontend(app, cfg)
    if sv and sv.rate_limit_per_minute > 0:
        app.add_middleware(
            RateLimitMiddleware,
            per_minute=sv.rate_limit_per_minute,
            trusted_proxy_hops=sv.trusted_proxy_hops,
        )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=sv.max_body_bytes if sv else 6_000_000)
    return app


def _prediction_rows(
    svc: ServiceState,
    version: str,
    ids: list[int],
    steps: np.ndarray,
    scores: np.ndarray,
    temporal: dict[int, dict],
) -> list[Prediction]:
    infos = svc.store.label_info_many(ids, svc.engine.latency)
    out = []
    for tx, step, score, li in zip(ids, steps, scores, infos, strict=True):
        tv = temporal[int(step)]
        out.append(
            Prediction(
                tx_id=tx,
                time_step=int(step),
                risk_score=float(score),
                model_version=version,
                label_status=li["label_status"],
                label_available_at_step=li["label_available_at_step"],
                temporally_valid=tv["temporally_valid"],
                note=tv["note"],
            )
        )
    return out


def _routes(app: FastAPI) -> None:
    # ------------------------------------------------------------------ health
    @app.get("/health/live", tags=["health"])
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    def ready(request: Request):
        svc = getattr(request.app.state, "svc", None)
        if svc is None:
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        warnings = {
            v: b.card.get("load_warnings")
            for v, b in svc.bundles.items()
            if b.card.get("load_warnings")
        }
        return {
            "status": "ready",
            "models_loaded": len(svc.bundles),
            "transactions_loaded": len(svc.store.model_frame),
            "data_fingerprint_verified": True,
            "version_warnings": warnings,
            "load_seconds": round(svc.load_seconds, 2),
        }

    # ------------------------------------------------------------------ meta
    @app.get("/v1/info", tags=["meta"])
    def info(request: Request) -> dict[str, Any]:
        svc = _state(request)
        m = svc.registry_meta
        return {
            "service_version": __version__,
            "git_commit": svc.git_commit,
            "data_mode": m["data_mode"],
            "label_latency_steps": m["label_latency"],
            "replay_steps": m["replay_steps"],
            "policies": {p: d for p, d in m["policies"].items()}
            | (
                {
                    "phase1": "Phase 1 reference model (trained on steps 1-29); "
                    "out-of-sample from step 35"
                }
                if m.get("phase1_reference")
                else {}
            ),
            "scoring_assumption": "end-of-step batch scoring: features of step s use the "
            "complete transaction graph of step s",
            "disclaimer": DISCLAIMER,
            "n_models": len(svc.bundles),
        }

    @app.get("/v1/models", tags=["models"])
    def models(request: Request) -> list[dict[str, Any]]:
        keys = (
            "version",
            "kind",
            "description",
            "trained_at_end_of_step",
            "label_latency",
            "train_steps",
            "n_train",
            "n_train_illicit",
            "feature_contract",
            "model_sha256",
            "created_utc",
            "data_mode",
        )
        return [{k: b.card.get(k) for k in keys} for b in _state(request).bundles.values()]

    @app.get("/v1/models/{version}", tags=["models"])
    def model(version: str, request: Request) -> dict[str, Any]:
        b = _state(request).bundles.get(version)
        if b is None:
            raise HTTPException(404, f"unknown model version {version!r}")
        return {k: v for k, v in b.card.items() if k != "feature_columns"} | {
            "n_features": len(b.feature_columns)
        }

    # ------------------------------------------------------------------ replay
    @app.get("/v1/replay/steps", tags=["replay"])
    def steps(request: Request) -> list[dict[str, Any]]:
        svc = _state(request)
        out = []
        for s in svc.engine.replay_steps():
            f = svc.store.step_frame(s)
            avail = []
            for p in POLICIES:
                try:
                    svc.engine.model_for(s, p)
                    avail.append(p)
                except ReplayError:
                    pass
            out.append(
                {
                    "step": s,
                    "n_transactions": len(f),
                    "n_unknown": int((f[S.RAW_LABEL] == S.CLASS_UNKNOWN).sum()),
                    "policies": avail,
                }
            )
        return out

    @app.get("/v1/replay/steps/{step}/ranking", tags=["replay"])
    def ranking(
        step: int,
        request: Request,
        policy: str = "expanding",
        limit: int = Query(50, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        labels: str = Query("all", pattern="^(all|labelled|unknown)$"),
    ) -> dict[str, Any]:
        svc = _state(request)
        r = svc.engine.ranking(step, policy)
        version = r.attrs["model_version"]
        if labels == "labelled":
            r = r[r["label_status"] != "unknown"]
        elif labels == "unknown":
            r = r[r["label_status"] == "unknown"]
        page = r.iloc[offset : offset + limit]
        return {
            "step": step,
            "policy": policy,
            "model_version": version,
            "model_trained_at_end_of_step": svc.bundles[version].tau,
            "total": len(r),
            "offset": offset,
            "labels_revealed_at_step": step + svc.engine.latency,
            "rows": [
                {
                    "tx_id": int(x.tx_id),
                    "risk_score": float(x.score),
                    "rank": int(x.rank),
                    "percentile": float(x.percentile),
                    "label_status": x.label_status,
                }
                for x in page.itertuples()
            ],
        }

    @app.get("/v1/replay/steps/{step}/metrics", tags=["replay"])
    def step_metrics(step: int, request: Request, policy: str = "expanding") -> dict[str, Any]:
        return _state(request).engine.step_metrics(step, policy)

    @app.get("/v1/replay/timeline", tags=["replay"])
    def timeline(request: Request, policy: str = "expanding") -> list[dict[str, Any]]:
        if policy not in POLICIES:
            raise HTTPException(400, f"unknown policy {policy!r}")
        return _state(request).engine.timeline(policy)

    # ------------------------------------------------------------------ transactions
    @app.get("/v1/transactions/{tx_id}", tags=["transactions"])
    def transaction(tx_id: int, request: Request, policy: str = "expanding") -> dict[str, Any]:
        svc = _state(request)
        row = svc.store.row(tx_id)
        step = int(row[S.TIME_STEP])
        version = svc.engine.model_for(step, policy)
        r = svc.engine.ranking(step, policy)
        me = r[r[S.TX_ID] == tx_id].iloc[0]
        top_k = svc.cfg.serving.explain_top_k if svc.cfg.serving else 10
        return {
            "tx_id": tx_id,
            "time_step": step,
            "policy": policy,
            "model_version": version,
            "model_trained_at_end_of_step": svc.bundles[version].tau,
            "risk_score": float(me.score),
            "rank_in_step": int(me["rank"]),
            "n_in_step": len(r),
            "percentile_in_step": float(me.percentile),
            **svc.store.label_info(tx_id, svc.engine.latency),
            "augmented_features": {c: _clean(row[c]) for c in S.AUGMENTED_FEATURES},
            "explanation": svc.engine.explain(version, tx_id, top_k),
            "disclaimer": DISCLAIMER,
        }

    @app.get("/v1/transactions/{tx_id}/neighbourhood", tags=["transactions"])
    def neighbourhood(
        tx_id: int,
        request: Request,
        policy: str = "expanding",
        hops: int = Query(1, ge=1, le=2),
        max_nodes: int = Query(60, ge=2, le=300),
    ) -> dict[str, Any]:
        svc = _state(request)
        nb = svc.store.neighbourhood(tx_id, hops, max_nodes)
        ranking = svc.engine.ranking(nb["step"], policy)
        r = ranking.set_index(S.TX_ID)
        nodes = [
            {
                "tx_id": n,
                "hop": d,
                "risk_score": float(r.loc[n, "score"]),
                "rank": int(r.loc[n, "rank"]),
                "label_status": r.loc[n, "label_status"],
            }
            for n, d in nb["nodes"].items()
            if n in r.index  # robust to edges whose endpoint is missing from the step
        ]
        return {
            "center": tx_id,
            "step": nb["step"],
            "policy": policy,
            "model_version": ranking.attrs.get("model_version"),
            "nodes": nodes,
            "edges": [{"source": a, "target": b} for a, b in nb["edges"]],
            "truncated": nb["truncated"],
            "note": "Edges are directed money flows within the same time step; the "
            "dataset has no edges across time steps.",
        }

    # ------------------------------------------------------------------ predictions
    @app.post("/v1/predict/batch", response_model=BatchResponse, tags=["predict"])
    def predict_batch(req: BatchByIdRequest, request: Request) -> BatchResponse:
        svc = _state(request)
        max_n = svc.cfg.serving.max_batch_size if svc.cfg.serving else 1000
        if len(req.tx_ids) > max_n:
            raise HTTPException(413, f"batch of {len(req.tx_ids)} exceeds max_batch_size={max_n}")
        if req.model_version and req.model_version not in svc.bundles:
            raise HTTPException(404, f"unknown model version {req.model_version!r}")
        present = set(
            svc.store.model_frame[S.TX_ID]
            .to_numpy()[np.isin(svc.store.model_frame[S.TX_ID].to_numpy(), req.tx_ids)]
            .tolist()
        )
        known = [t for t in req.tx_ids if t in present]
        errors = [
            ItemError(
                tx_id=t,
                code="unknown_transaction",
                message="transaction id not in the Elliptic++ dataset",
            )
            for t in req.tx_ids
            if t not in present
        ]
        preds: list[Prediction] = []
        if known:
            steps_arr = svc.store.step_of(known)
            groups: dict[str, list[int]] = {}
            temporal: dict[str, dict[int, dict]] = {}
            per_step: dict[int, str | ReplayError] = {}
            for step in {int(x) for x in steps_arr}:
                try:
                    per_step[step] = req.model_version or svc.engine.model_for(step, req.policy)
                except ReplayError as exc:
                    per_step[step] = exc
            for tx, step in zip(known, steps_arr, strict=True):
                version = per_step[int(step)]
                if isinstance(version, ReplayError):
                    errors.append(
                        ItemError(tx_id=tx, code="replay_unavailable", message=str(version))
                    )
                    continue
                groups.setdefault(version, []).append(tx)
                if int(step) not in temporal.setdefault(version, {}):
                    temporal[version][int(step)] = svc.engine.temporal_validity(version, int(step))
            for version, ids in groups.items():
                scores = svc.engine.score(version, ids)
                preds += _prediction_rows(
                    svc, version, ids, svc.store.step_of(ids), scores, temporal[version]
                )
        order = {t: i for i, t in enumerate(req.tx_ids)}
        preds.sort(key=lambda p: order[p.tx_id])
        return BatchResponse(predictions=preds, errors=errors, disclaimer=DISCLAIMER)

    @app.post("/v1/predict/features", response_model=FeatureVectorResponse, tags=["predict"])
    def predict_features(req: FeatureVectorRequest, request: Request) -> FeatureVectorResponse:
        svc = _state(request)
        b = svc.bundles.get(req.model_version)
        if b is None:
            raise HTTPException(404, f"unknown model version {req.model_version!r}")
        max_n = svc.cfg.serving.max_batch_size if svc.cfg.serving else 1000
        if len(req.instances) > max_n:
            raise HTTPException(413, f"{len(req.instances)} instances exceed {max_n}")
        X = pd.DataFrame(req.instances, columns=b.feature_columns).astype("float64")
        scores = b.model.predict_proba(X)[:, 1]
        return FeatureVectorResponse(
            model_version=b.version,
            risk_scores=scores.tolist(),
            feature_space=FEATURE_SPACE,
            disclaimer=DISCLAIMER,
        )

    # ------------------------------------------------------------------ research results
    @app.get("/v1/experiments", tags=["research"])
    def experiments(request: Request) -> dict[str, Any]:
        return _state(request).research


def research_results(reports: Path) -> dict[str, Any]:
    """Committed Phase 1/2 results on the real Elliptic++ data (read-only, never recomputed)."""
    out: dict[str, Any] = {
        "source": "committed reports/ files generated in Phases 1-2 on Elliptic++; independent "
        "of the data currently loaded by the service",
    }
    p1 = reports / "baseline_results.json"
    if p1.exists():
        out["phase1_baselines"] = read_json(p1)
    bt = reports / "phase2" / "backtest"
    if (bt / "per_step_confirm.csv").exists():
        out["phase2_per_step"] = {}
        for stage in ("dev", "confirm"):
            ps = pd.read_csv(bt / f"per_step_{stage}.csv")
            agg = ps.groupby(["strategy", S.TIME_STEP])[["ap", "roc_auc", "hits@50"]].mean()
            out["phase2_per_step"][stage] = [
                {"strategy": st, "step": int(t), **{k: _clean(v) for k, v in row.items()}}
                for (st, t), row in agg.iterrows()
            ]
    for name in (
        "bootstrap_dev",
        "bootstrap_confirm",
        "bootstrap_confirm_early",
        "bootstrap_confirm_late",
        "costs",
    ):
        p = bt / f"{name}.csv"
        if p.exists():
            df = pd.read_csv(p)
            out[f"phase2_{name}"] = [
                {k: _clean(v) for k, v in r.items()} for r in df.to_dict(orient="records")
            ]
    if (bt / "verdicts.json").exists():
        out["phase2_verdicts"] = read_json(bt / "verdicts.json")
    return out


def _clean(v: Any) -> Any:
    if isinstance(v, float | np.floating):
        return None if not np.isfinite(v) else float(v)
    if isinstance(v, np.integer):
        return int(v)
    return v


def _mount_frontend(app: FastAPI, cfg: Config) -> None:
    dist = cfg.serving.frontend_dist if cfg.serving else None
    if not dist or not (dist / "index.html").exists():
        return
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        first = path.split("/", 1)[0]
        if first in {"v1", "health", "docs", "redoc", "openapi.json", "assets"}:
            raise HTTPException(404, f"no route /{path}")
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        if "." in path.rsplit("/", 1)[-1]:  # missing file-like path: real 404, not the SPA
            raise HTTPException(404, f"no file /{path}")
        return FileResponse(dist / "index.html")
