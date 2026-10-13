"""Versioned model registry for serving.

Layout::

    <registry_dir>/
        index.json                         {version: {"kind", "tau", ...}}
        <version>/
            model.joblib                   sklearn Pipeline (preprocess -> XGBClassifier)
            model_card.json                feature contract, provenance, sha256, library versions

Trust model (read before loading anything):

* joblib/pickle **executes code on load**. A registry must only ever come from
  ``chainwatch export-snapshots`` / ``demo-setup`` run by the operator. Never load a
  registry downloaded from, or writable by, someone else.
* The SHA-256 in each model card is an **integrity** check (corruption, truncation, a
  bundle copied from a different export). It is *not* tamper-proofing: whoever can
  replace a pickle can also rewrite its card. Protection against tampering comes from
  deployment: the demo image builds its registry at image-build time as root and serves
  it read-only, and the real-data compose profile mounts the registry read-only.
* Version names are validated and resolved inside ``registry_dir`` so index entries cannot
  point outside it, and library-version mismatches (pickled sklearn/xgboost objects are
  not portable across versions) are refused unless explicitly overridden.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib

from chainwatch.utils import environment_info, get_logger, read_json, sha256_file, write_json

log = get_logger(__name__)

MODEL_FILE = "model.joblib"
CARD_FILE = "model_card.json"
INDEX_FILE = "index.json"
PINNED_LIBS = ("scikit-learn", "xgboost")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class RegistryError(RuntimeError):
    """The registry is missing, corrupt, or incompatible with this environment."""


def feature_contract_hash(feature_columns: list[str]) -> str:
    """Order-sensitive fingerprint of the model's input columns."""
    return hashlib.sha256("\n".join(feature_columns).encode()).hexdigest()[:16]


@dataclass
class ModelBundle:
    version: str
    model: Any
    card: dict[str, Any]

    @property
    def feature_columns(self) -> list[str]:
        return self.card["feature_columns"]

    @property
    def tau(self) -> int | None:
        return self.card.get("trained_at_end_of_step")


def bundle_dir(registry_dir: Path, version: str) -> Path:
    """Directory of ``version``; refuses names that could escape ``registry_dir``."""
    if not VERSION_RE.match(version) or ".." in version:
        raise RegistryError(f"invalid model version name {version!r}")
    root = registry_dir.resolve()
    d = (root / version).resolve()
    if d.parent != root:
        raise RegistryError(f"model version {version!r} resolves outside the registry")
    return d


def save_bundle(
    registry_dir: Path, version: str, model: Any, card: dict[str, Any], overwrite: bool = False
) -> Path:
    required = ("kind", "feature_columns", "description")
    missing = [k for k in required if k not in card]
    if missing:
        raise ValueError(f"model card missing {missing}")
    if (
        list(getattr(model, "feature_names_in_", card["feature_columns"]))
        != card["feature_columns"]
    ):
        raise ValueError("model.feature_names_in_ does not match the card's feature_columns")
    out = bundle_dir(registry_dir, version)
    if out.exists():
        if not overwrite:
            raise FileExistsError(f"{out} exists")
        shutil.rmtree(out)
    out.mkdir(parents=True)
    joblib.dump(model, out / MODEL_FILE)
    card = {
        **card,
        "version": version,
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "feature_contract": feature_contract_hash(card["feature_columns"]),
        "model_sha256": sha256_file(out / MODEL_FILE),
        "environment": environment_info(),
    }
    write_json(out / CARD_FILE, card)
    _update_index(registry_dir, version, card)
    return out


def _update_index(registry_dir: Path, version: str, card: dict[str, Any]) -> None:
    path = registry_dir / INDEX_FILE
    index = read_json(path) if path.exists() else {}
    index[version] = {
        "kind": card["kind"],
        "description": card["description"],
        "trained_at_end_of_step": card.get("trained_at_end_of_step"),
        "label_latency": card.get("label_latency"),
        "feature_contract": card["feature_contract"],
    }
    write_json(path, dict(sorted(index.items())))


def read_index(registry_dir: Path) -> dict[str, dict[str, Any]]:
    path = registry_dir / INDEX_FILE
    if not path.exists():
        raise RegistryError(
            f"No model registry at {registry_dir}. Build it with `chainwatch export-snapshots` "
            "(real data) or `chainwatch --config configs/demo.yaml demo-setup` (synthetic demo)."
        )
    return read_json(path)


def _check_versions(card: dict[str, Any], allow_mismatch: bool) -> list[str]:
    here = environment_info()
    problems = []
    for lib in PINNED_LIBS:
        trained = card.get("environment", {}).get(lib)
        if trained is None:
            continue
        if trained.split(".")[:2] != here[lib].split(".")[:2]:
            problems.append(f"{lib}: trained with {trained}, running {here[lib]}")
    if problems and not allow_mismatch:
        raise RegistryError(
            f"model {card.get('version')} is incompatible with this environment: {problems}. "
            "Re-export the registry or set serving.allow_version_mismatch=true."
        )
    return problems


def load_bundle(
    registry_dir: Path, version: str, allow_version_mismatch: bool = False
) -> ModelBundle:
    d = bundle_dir(registry_dir, version)
    card_path, model_path = d / CARD_FILE, d / MODEL_FILE
    if not card_path.exists() or not model_path.exists():
        raise RegistryError(f"incomplete bundle {d}")
    card = read_json(card_path)
    digest = sha256_file(model_path)
    if digest != card.get("model_sha256"):
        raise RegistryError(
            f"{version}: model file hash {digest[:12]} != card {card.get('model_sha256', '')[:12]}"
        )
    warnings = _check_versions(card, allow_version_mismatch)
    # Integrity check passed; trust still rests on the registry being operator-built and
    # read-only (see module docstring). No request path ever reaches this function.
    model = joblib.load(model_path)
    if list(model.feature_names_in_) != card["feature_columns"]:
        raise RegistryError(f"{version}: model input columns differ from its card")
    if feature_contract_hash(card["feature_columns"]) != card["feature_contract"]:
        raise RegistryError(f"{version}: feature contract hash mismatch")
    card["load_warnings"] = warnings
    return ModelBundle(version=version, model=model, card=card)


def reset_index(registry_dir: Path) -> None:
    """Start a fresh index (exports rebuild it so stale bundles are never routed)."""
    registry_dir.mkdir(parents=True, exist_ok=True)
    write_json(registry_dir / INDEX_FILE, {})


def load_registry(
    registry_dir: Path,
    allow_version_mismatch: bool = False,
    versions: list[str] | None = None,
    expected_fingerprint: str | None = None,
    expected_latency: int | None = None,
) -> dict[str, ModelBundle]:
    """Load ``versions`` (default: every index entry) and check each card's provenance."""
    index = read_index(registry_dir)
    wanted = list(index) if versions is None else list(versions)
    missing = [v for v in wanted if v not in index]
    if missing:
        raise RegistryError(f"versions not in the registry index: {missing}")
    bundles = {v: load_bundle(registry_dir, v, allow_version_mismatch) for v in wanted}
    for v, b in bundles.items():
        fp = b.card.get("data_fingerprint")
        if expected_fingerprint is not None and fp != expected_fingerprint:
            raise RegistryError(f"{v} was trained on different data ({fp})")
        if (
            expected_latency is not None
            and b.card["kind"] == "snapshot"
            and b.card.get("label_latency") != expected_latency
        ):
            raise RegistryError(
                f"{v} has label latency {b.card.get('label_latency')}, "
                f"registry declares {expected_latency}"
            )
    contracts = {b.card["feature_contract"] for b in bundles.values()}
    log.info("Loaded %d model bundles (%d feature contract(s))", len(bundles), len(contracts))
    return bundles
