"""Layer C: supervised ML risk predictions, behind an evidence gate.

No model is validated for live Bitcoin scoring (docs/RISK_DATA_FEASIBILITY.md): the
labelled datasets that exist are anonymised (Elliptic, Elliptic++, Elliptic2 features cannot
be computed for new transactions), address/entity-level and small (OFAC), or not available
under terms that allow public training and evaluation. Until a model passes the promotion
gate below, this layer returns ``NO_VALIDATED_MODEL`` for every transaction. It never falls
back to the historical Elliptic++ models.

Promotion gate (all required, checked from the model card, not from the model itself):
the card's ``feature_schema`` equals the live schema; ``validated_for_live`` is true;
``evaluation`` names an independent, time-separated holdout with pre-registered primary
metrics; and the artifact's sha256 matches. Shadow mode (scored, logged, never shown as a
finding) is the only allowed state before promotion.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from chainwatch.live.features import FEATURE_SCHEMA_VERSION

NO_VALIDATED_MODEL = "NO_VALIDATED_MODEL"
REQUIRED_CARD_KEYS = (
    "name",
    "version",
    "feature_schema",
    "validated_for_live",
    "evaluation",
    "sha256",
)


class ModelGateError(ValueError):
    """A model card failed the promotion gate; the reason says which requirement."""


def check_model_card(card: dict[str, Any], artifact: Path) -> None:
    missing = [k for k in REQUIRED_CARD_KEYS if k not in card]
    if missing:
        raise ModelGateError(f"model card lacks {missing}")
    if card["feature_schema"] != FEATURE_SCHEMA_VERSION:
        raise ModelGateError(
            f"model expects features {card['feature_schema']!r}, live schema is "
            f"{FEATURE_SCHEMA_VERSION!r}"
        )
    if card["validated_for_live"] is not True:
        raise ModelGateError("model is not validated for live scoring")
    ev = card["evaluation"]
    if not isinstance(ev, dict):
        raise ModelGateError("evaluation must be an object")
    for key in ("holdout", "primary_metrics", "label_source", "time_separated"):
        if key not in ev:
            raise ModelGateError(f"evaluation lacks {key!r}")
    if ev["time_separated"] is not True:
        raise ModelGateError("evaluation holdout is not time-separated")
    # present is not enough: an empty holdout, metric list or label source proves nothing
    if (
        not ev["holdout"]
        or not isinstance(ev["primary_metrics"], list | dict)
        or not (ev["primary_metrics"])
    ):
        raise ModelGateError("evaluation needs a non-empty holdout and primary metrics")
    if not isinstance(ev["label_source"], str) or not ev["label_source"].strip():
        raise ModelGateError("evaluation needs a label source")
    if not isinstance(card["sha256"], str):
        raise ModelGateError("sha256 must be a string")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if digest != card["sha256"]:
        raise ModelGateError("artifact sha256 does not match its model card")


class SupervisedLayer:
    """Holds a promoted model if (and only if) one passed the gate; abstains otherwise."""

    def __init__(self, model_dir: Path | None = None) -> None:
        self.model = None
        self.card: dict[str, Any] | None = None
        self.gate_error: str | None = None
        if model_dir is None:
            self.gate_error = "no model configured"
            return
        card_path, artifact = Path(model_dir) / "model_card.json", Path(model_dir) / "model.bin"
        try:
            card = json.loads(card_path.read_text())
            check_model_card(card, artifact)
        except (OSError, ValueError, TypeError, KeyError) as e:  # gate, JSON and shape errors
            self.gate_error = str(e)[:300]
            return
        # A real loader would deserialize a pickle-free format here. None exists because no
        # model has passed the gate; refuse rather than guess.
        self.gate_error = "no pickle-free loader for promoted models is implemented yet"
        self.card = card

    def status(self) -> dict[str, Any]:
        return {"state": "abstaining", "model": None, "reason": self.gate_error}

    def predict(self, features: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": NO_VALIDATED_MODEL,
            "detail": "No supervised model is validated for live Bitcoin transactions; this "
            "layer abstains (see docs/RISK_DATA_FEASIBILITY.md).",
            "gate": self.gate_error,
        }
