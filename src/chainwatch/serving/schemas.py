"""Pydantic request/response models for the ChainWatch API."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from chainwatch.data import schema as S

Policy = Literal["expanding", "static", "phase1"]
LabelStatus = Literal["illicit", "licit", "unknown"]
# Degrees can always be recomputed by the data owner; the training pipeline repaired them
# from the edge list, so a missing degree here would silently diverge from training.
REQUIRED_NON_NULL = set(S.LOCAL_FEATURES) | set(S.AGGREGATE_FEATURES) | set(S.DEGREE_FEATURES)
NULLABLE = set(S.AUGMENTED_FEATURES) - set(S.DEGREE_FEATURES)
HARD_MAX_ITEMS = 1000  # absolute cap; serving.max_batch_size may be lower


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any = None


class ErrorResponse(BaseModel):
    error: ErrorBody
    request_id: str | None = None


class BatchByIdRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tx_ids: list[int] = Field(
        min_length=1, max_length=HARD_MAX_ITEMS, description="Elliptic++ transaction ids"
    )
    policy: Policy = "expanding"
    model_version: str | None = Field(
        default=None,
        description="Score with this exact registry version instead of the replay policy. "
        "The response reports whether the model was available at the transaction's step.",
    )

    @field_validator("tx_ids")
    @classmethod
    def unique_ids(cls, v: list[int]) -> list[int]:
        if len(set(v)) != len(v):
            raise ValueError("tx_ids must be unique")
        return v


class Prediction(BaseModel):
    tx_id: int
    time_step: int
    risk_score: float = Field(
        description="Model output; a ranking signal, not a calibrated probability and not a verdict"
    )
    model_version: str
    label_status: LabelStatus
    label_available_at_step: int | None
    temporally_valid: bool
    note: str | None = None


class ItemError(BaseModel):
    tx_id: int
    code: str
    message: str


class BatchResponse(BaseModel):
    predictions: list[Prediction]
    errors: list[ItemError]
    disclaimer: str


class FeatureVectorRequest(BaseModel):
    """Research interface: complete Elliptic++-format feature vectors.

    Values must be in the publisher's (anonymised, standardised) feature space. They
    cannot be computed for arbitrary real-world Bitcoin transactions.
    """

    model_config = ConfigDict(extra="forbid")

    model_version: str
    instances: list[dict[str, float | None]] = Field(min_length=1, max_length=HARD_MAX_ITEMS)

    @model_validator(mode="after")
    def check_instances(self) -> FeatureVectorRequest:
        expected = set(S.ALL_FEATURES)
        for i, inst in enumerate(self.instances):
            keys = set(inst)
            if keys != expected:
                missing = sorted(expected - keys)[:5]
                extra = sorted(keys - expected)[:5]
                raise ValueError(f"instance {i}: missing {missing} unexpected {extra}")
            nulls = [k for k in REQUIRED_NON_NULL if inst[k] is None]
            if nulls:
                raise ValueError(f"instance {i}: null not allowed for {sorted(nulls)[:5]}")
            bad = [k for k, v in inst.items() if v is not None and not math.isfinite(v)]
            if bad:
                raise ValueError(f"instance {i}: non-finite values in {bad[:5]}")
        return self


class FeatureVectorResponse(BaseModel):
    model_version: str
    risk_scores: list[float]
    feature_space: str
    disclaimer: str
