"""Supply-chain release gate: the workflow files themselves (a regression here would let an
unverified image reach the public registry)."""

from __future__ import annotations

import re

import yaml

from chainwatch.config import REPO_ROOT

WF = REPO_ROOT / ".github" / "workflows"


def _load(name: str) -> dict:
    d = yaml.safe_load((WF / name).read_text())
    d["on"] = d.pop(True, d.get("on"))  # YAML 1.1 reads the key `on` as True
    return d


def test_image_is_published_only_after_successful_ci():
    wf = _load("publish-demo-image.yml")
    assert set(wf["on"]) == {"workflow_run", "workflow_dispatch"}  # never on a bare push
    assert wf["on"]["workflow_run"] == {"workflows": ["CI"], "types": ["completed"],
                                        "branches": ["main"]}  # fmt: skip
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["publish"]
    cond = " ".join(job["if"].split())
    for needed in ("github.event.workflow_run.conclusion == 'success'",
                   "github.event.workflow_run.event == 'push'",
                   "github.event.workflow_run.head_branch == 'main'",
                   "head_repository.full_name == github.repository",
                   "github.ref == 'refs/heads/main'"):  # fmt: skip
        assert needed in cond
    first, checkout = job["steps"][0], job["steps"][1]
    assert "status=success" in first["run"] and "exit 1" in first["run"]  # API re-check
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["ref"] == "${{ env.SHA }}"  # the commit CI tested
    assert "GITHUB_SHA" not in str(job["steps"])  # tags and labels use the tested commit
    assert set(job["permissions"]) == {"contents", "packages", "actions"}


def test_ci_installs_only_hash_locked_dependencies():
    ci = _load("ci.yml")
    assert ci["permissions"] == {"contents": "read"}
    install = next(
        s for s in ci["jobs"]["python"]["steps"] if s.get("name", "").startswith("Install")
    )
    assert "--require-hashes -r requirements/dev.lock.txt" in install["run"]
    assert "--no-deps --no-build-isolation -e ." in install["run"]
    assert {"audit", "deployed-locks"} <= set(ci["jobs"]["docker"]["needs"])
    deployed = " ".join(s.get("run", "") for s in ci["jobs"]["deployed-locks"]["steps"])
    assert (
        "--require-hashes -r requirements/serve.lock.txt -r requirements/test.lock.txt" in deployed
    )
    lock = (REPO_ROOT / "requirements" / "dev.lock.txt").read_text()
    pins = re.findall(r"^([A-Za-z0-9_.-]+)==\S+", lock, re.M)
    assert {"pytest", "ruff", "setuptools", "fastapi"} <= {p.lower() for p in pins}
    hashed = re.findall(r"^[A-Za-z0-9_.-]+==[^\n]*\\\n\s+--hash=sha256:[0-9a-f]{64}", lock, re.M)
    assert len(pins) > 40 and len(hashed) == len(pins)  # every pin carries its hashes


def test_every_action_is_pinned_to_a_commit():
    for path in WF.glob("*.yml"):
        for ref in re.findall(r"uses:\s*(\S+)", path.read_text()):
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref), (path.name, ref)
