"""The container entrypoint must never train models when a prebuilt registry is required."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from chainwatch.config import REPO_ROOT

ENTRYPOINT = REPO_ROOT / "docker" / "entrypoint.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("sh") is None, reason="POSIX shell entrypoint"
)


def _run(tmp_path, **env):
    # a fake `chainwatch` on PATH records any invocation instead of training or serving
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    fake = bin_dir / "chainwatch"
    fake.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
    fake.chmod(0o755)
    full_env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", **env}
    proc = subprocess.run(["sh", str(ENTRYPOINT)], env=full_env, capture_output=True, text=True)
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def test_prebuilt_only_refuses_to_train_when_registry_missing(tmp_path):
    # /app/artifacts/demo/registry/registry.json does not exist on the test host
    proc, calls = _run(tmp_path, CHAINWATCH_DEMO="1", CHAINWATCH_PREBUILT_ONLY="1")
    assert proc.returncode == 1
    assert "refusing to train" in proc.stderr
    assert calls == []  # neither demo-setup nor serve was started


def test_runtime_target_still_builds_demo_when_allowed(tmp_path):
    proc, calls = _run(tmp_path, CHAINWATCH_DEMO="1", PORT="9999")
    assert proc.returncode == 0
    assert calls[0].endswith("demo-setup")
    assert calls[1].endswith("serve --host 0.0.0.0 --port 9999")


def test_serves_directly_outside_demo_mode(tmp_path):
    proc, calls = _run(tmp_path, CHAINWATCH_CONFIG="/x.yaml")
    assert proc.returncode == 0
    assert calls == ["--config /x.yaml serve --host 0.0.0.0 --port 8000"]
