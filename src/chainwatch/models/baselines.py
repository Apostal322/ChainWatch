"""Classical baselines: logistic regression and gradient-boosted trees.

Each ``fit_*`` function receives a training split and a validation split, fits the
preprocessor on *training data only*, and returns a single sklearn ``Pipeline``
(preprocessor + classifier) that maps a raw feature DataFrame to probabilities.
Keeping preprocessing inside the artifact means inference cannot silently drift
from training-time transformations.

Class imbalance is handled with loss re-weighting computed from the training labels
(``class_weight='balanced'`` / ``scale_pos_weight = n_neg / n_pos``) rather than
resampling: it uses every training example, adds no synthetic data, and is trivially
reproducible. Re-weighting distorts probability calibration, which is why the
decision threshold is chosen on validation data rather than fixed at 0.5.
"""

from __future__ import annotations

from collections.abc import Iterator
from itertools import product
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from xgboost import XGBClassifier

from chainwatch.features.preprocessing import build_preprocessor

MODEL_NAMES = ("logreg", "xgboost")


def param_grid(grid: dict[str, list[Any]]) -> Iterator[dict[str, Any]]:
    """Cartesian product of a ``{name: [values]}`` grid.

    Keys are sorted so the iteration order (and therefore tie-breaking between equally
    scored candidates) does not depend on how the YAML file happens to be laid out.
    """
    keys = sorted(grid)
    for values in product(*(grid[k] for k in keys)):
        yield dict(zip(keys, values, strict=True))


def fit_logreg(
    X_train: pd.DataFrame,
    y_train: np.ndarray,
    *,
    params: dict[str, Any],
    base: dict[str, Any],
    seed: int,
    log1p_augmented: bool,
    **_: Any,
) -> Pipeline:
    pre = build_preprocessor(list(X_train.columns), log1p_augmented=log1p_augmented, scale=True)
    clf = LogisticRegression(
        C=float(params["C"]),
        class_weight=base.get("class_weight", "balanced"),
        max_iter=int(base.get("max_iter", 5000)),
        solver="lbfgs",
        random_state=seed,
    )
    model = Pipeline([("preprocess", pre), ("clf", clf)])
    model.fit(X_train, y_train)
    return model


def fit_xgboost(
    X_train: pd.DataFrame,
    y_train: np.ndarray,
    *,
    X_val: pd.DataFrame,
    y_val: np.ndarray,
    params: dict[str, Any],
    base: dict[str, Any],
    seed: int,
    log1p_augmented: bool,
) -> Pipeline:
    pre = build_preprocessor(list(X_train.columns), log1p_augmented=log1p_augmented, scale=False)
    # Fit on train only; validation is merely *transformed* for early stopping.
    Xt = pre.fit_transform(X_train)
    Xv = pre.transform(X_val)

    n_pos = int(y_train.sum())
    spw = (len(y_train) - n_pos) / n_pos if base.get("use_scale_pos_weight", True) else 1.0
    clf = XGBClassifier(
        n_estimators=int(base.get("n_estimators", 2000)),
        early_stopping_rounds=int(base.get("early_stopping_rounds", 50)),
        tree_method=base.get("tree_method", "hist"),
        n_jobs=int(base.get("n_jobs", -1)),
        eval_metric="aucpr",
        scale_pos_weight=spw,
        random_state=seed,
        **params,
    )
    clf.fit(Xt, y_train, eval_set=[(Xv, y_val)], verbose=False)
    # Assemble an already-fitted pipeline so the artifact is a single object.
    return Pipeline([("preprocess", pre), ("clf", clf)])


FIT_FUNCTIONS = {"logreg": fit_logreg, "xgboost": fit_xgboost}


def fit_model(name: str, **kwargs: Any) -> Pipeline:
    if name not in FIT_FUNCTIONS:
        raise ValueError(f"Unknown model {name!r}; choose from {MODEL_NAMES}")
    return FIT_FUNCTIONS[name](**kwargs)


def describe_fitted(name: str, model: Pipeline) -> dict[str, Any]:
    """Model-specific facts worth recording (e.g. trees kept after early stopping)."""
    clf = model.named_steps["clf"]
    if name == "xgboost":
        return {
            "best_iteration": int(clf.best_iteration),
            "scale_pos_weight": float(clf.scale_pos_weight),
        }
    if name == "logreg":
        return {"n_iter": int(np.max(clf.n_iter_))}
    return {}
