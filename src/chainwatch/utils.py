"""Small cross-cutting helpers: logging, seeding, hashing, JSON IO, environment capture."""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import random
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def setup_logging(level: int | str = logging.INFO) -> None:
    logging.basicConfig(level=level, format=LOG_FORMAT, datefmt="%H:%M:%S", force=True)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def set_global_seed(seed: int) -> None:
    """Seed Python and NumPy RNGs. Model-level seeds are passed explicitly as well."""
    random.seed(seed)
    np.random.seed(seed)  # noqa: NPY002 - seeds libraries that use the legacy global RNG


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def _to_jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return None if np.isnan(obj) else float(obj)
    if isinstance(obj, float) and np.isnan(obj):
        return None
    if isinstance(obj, np.ndarray):
        return _to_jsonable(obj.tolist())
    if isinstance(obj, Path):
        return str(obj)
    return obj


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(_to_jsonable(obj), fh, indent=2, sort_keys=False)
        fh.write("\n")


def read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def git_commit(repo_root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True
        ).stdout.strip()
        return out.stdout.strip() + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return None


def environment_info() -> dict[str, str]:
    import pandas
    import sklearn
    import xgboost

    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "numpy": np.__version__,
        "pandas": pandas.__version__,
        "scikit-learn": sklearn.__version__,
        "xgboost": xgboost.__version__,
    }
