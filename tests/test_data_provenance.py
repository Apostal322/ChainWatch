"""Data provenance: manifests and run metadata must fingerprint the files actually used.

Regression test for a bug where ``manifest.json`` (and therefore every run's
``data_manifest``) recorded the *published* Elliptic++ SHA-256 constants whatever was on
disk, so a model trained on synthetic data carried the real dataset's hashes.
"""

from __future__ import annotations

import logging

import pytest

from chainwatch.data import schema as S
from chainwatch.data.download import REMOTE_FILES
from chainwatch.data.loader import MANIFEST_JSON, load_manifest, prepare
from chainwatch.data.synthetic import write_synthetic_raw
from chainwatch.serving.store import data_fingerprint
from chainwatch.training.pipeline import _safe_manifest, compare_manifests, evaluate, train
from chainwatch.utils import read_json, sha256_file, write_json

PUBLISHED = {rf.name: rf.sha256 for rf in REMOTE_FILES}


@pytest.fixture
def synthetic_cfg(cfg):
    """The test config, with its raw dir replaced by the demo-data writer's output."""
    write_synthetic_raw(cfg.paths.raw_dir, n_steps=12, per_step=60, seed=0)
    prepare(cfg.paths.raw_dir, cfg.paths.processed_dir)
    return cfg


def test_manifest_records_actual_file_hashes(synthetic_cfg):
    cfg = synthetic_cfg
    manifest = load_manifest(cfg.paths.processed_dir)
    assert manifest["manifest_version"] >= 2
    assert manifest["synthetic"] is True
    for name in (S.FEATURES_FILE, S.CLASSES_FILE, S.EDGES_FILE):
        entry = manifest["raw_files"][name]
        assert entry["sha256"] == sha256_file(cfg.paths.raw_dir / name)
        assert entry["sha256"] != PUBLISHED[name]
        assert entry["published_sha256"] == PUBLISHED[name]
        assert entry["size_bytes"] == (cfg.paths.raw_dir / name).stat().st_size
        assert entry["matches_published"] is False


def test_run_metadata_carries_fingerprints_not_published_constants(synthetic_cfg):
    cfg = synthetic_cfg
    meta = read_json(train(cfg, "logreg") / "metadata.json")
    dm = meta["data_manifest"]
    assert dm["synthetic"] is True
    assert dm["processed_fingerprint"] == data_fingerprint(cfg.paths.processed_dir)
    for name, entry in dm["raw_files"].items():
        assert entry["sha256"] == sha256_file(cfg.paths.raw_dir / name)
        assert entry["sha256"] != PUBLISHED[name]
        assert entry["matches_published"] is False


def test_evaluate_warns_only_when_data_changed(synthetic_cfg, caplog):
    cfg = synthetic_cfg
    run_dir = train(cfg, "logreg")

    with caplog.at_level(logging.WARNING, logger="chainwatch.training.pipeline"):
        evaluate(cfg, run_dir)
    assert "Data provenance" not in caplog.text

    # different raw data under the same file names: hashes and fingerprint must differ
    write_synthetic_raw(cfg.paths.raw_dir, n_steps=12, per_step=60, seed=1)
    prepare(cfg.paths.raw_dir, cfg.paths.processed_dir)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="chainwatch.training.pipeline"):
        evaluate(cfg, run_dir, force=True)
    assert "raw file" in caplog.text
    assert "processed data fingerprint differs" in caplog.text


def test_legacy_manifest_and_metadata_remain_readable(synthetic_cfg):
    cfg = synthetic_cfg
    path = cfg.paths.processed_dir / MANIFEST_JSON
    legacy = read_json(path)
    legacy.pop("manifest_version")
    legacy.pop("synthetic")
    legacy["raw_files"] = {
        n: {"sha256": PUBLISHED[n], "matches_published": False} for n in PUBLISHED
    }
    write_json(path, legacy)

    current = _safe_manifest(cfg.paths.processed_dir)
    assert current["manifest_version"] == 1
    # the legacy "sha256" was the published constant, so it must not be reported as actual
    assert all(v["sha256"] is None for v in current["raw_files"].values())
    assert all(v["matches_published"] is False for v in current["raw_files"].values())

    # old run metadata stored a flat {file: published sha256} map
    notices = compare_manifests(dict(PUBLISHED), current)
    assert len(notices) == 1 and "cannot verify" in notices[0]
    assert compare_manifests(None, current) == []
    assert compare_manifests(current, None) == []
    assert compare_manifests(current, current) == []
