"""Optional MLflow logging.

Local JSON artifacts are the source of truth; MLflow is a convenience layer for
browsing/comparing runs. If MLflow is disabled or not installed, everything still
works and nothing is lost.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from chainwatch.config import REPO_ROOT, TrackingConfig
from chainwatch.utils import get_logger

log = get_logger(__name__)


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def log_run(
    tracking: TrackingConfig,
    run_name: str,
    params: dict[str, Any],
    metrics: dict[str, Any],
    run_dir: Path,
    tags: dict[str, str] | None = None,
    log_artifacts: bool = True,
) -> None:
    if not tracking.mlflow:
        return
    try:
        import mlflow
    except ImportError:
        log.warning(
            "tracking.mlflow=true but mlflow is not installed; `pip install -e .[tracking]`"
        )
        return

    uri = tracking.tracking_uri
    if uri.startswith("sqlite:///") and not Path(uri.removeprefix("sqlite:///")).is_absolute():
        uri = f"sqlite:///{(REPO_ROOT / uri.removeprefix('sqlite:///')).as_posix()}"
    mlflow.set_tracking_uri(uri)
    mlflow.set_experiment(tracking.experiment_name)
    numeric = {
        k: float(v)
        for k, v in _flatten(metrics).items()
        if isinstance(v, int | float) and not isinstance(v, bool) and v == v  # drop NaN
    }
    with mlflow.start_run(run_name=run_name):
        mlflow.log_params({k: str(v)[:500] for k, v in _flatten(params).items()})
        mlflow.log_metrics({k.replace("@", "_at_"): v for k, v in numeric.items()})
        mlflow.set_tags(tags or {})
        if log_artifacts:
            mlflow.log_artifacts(str(run_dir), artifact_path="run")
        else:
            mlflow.set_tag("local_run_dir", str(run_dir))
    log.info("Logged run %s to MLflow (%s)", run_name, uri)
