"""Live end-to-end loop on a real cluster (O1), opt-in with ``GREENKUBE_E2E=1``.

Runs ``hack/e2e/run.sh`` (build, install, Argo CD and Flux loops) and checks
the evidence it records. Needs Minikube with Argo CD, Flux, Gitea and
Prometheus; see docs/get-started/first-pr.md. Takes 20-40 minutes.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(os.getenv("GREENKUBE_E2E") != "1", reason="set GREENKUBE_E2E=1 to run the live loop")

ARGO_STEPS = (
    "argocd.automation_ready",
    "argocd.binding",
    "argocd.recommendation",
    "argocd.plan_preview",
    "argocd.approval",
    "argocd.pull_request",
    "argocd.merge",
    "argocd.controller_sync",
    "argocd.greenkube_merge_detected",
    "argocd.greenkube_sync_confirmed",
    "argocd.apply_detected",
    "argocd.measurement_started",
)


def test_live_loop(tmp_path: Path) -> None:
    env = {**os.environ, "E2E_STATE_DIR": os.getenv("E2E_STATE_DIR", str(tmp_path))}
    result = subprocess.run([str(REPO_ROOT / "hack" / "e2e" / "run.sh")], env=env, cwd=REPO_ROOT, check=False)

    steps_file = Path(env["E2E_STATE_DIR"]) / "evidence" / "steps.jsonl"
    steps = [json.loads(line) for line in steps_file.read_text().splitlines() if line.strip()]
    failed = [step for step in steps if step["status"] == "failed"]
    assert not failed, failed
    reached = {step["step"] for step in steps if step["status"] == "ok"}
    expected = set(ARGO_STEPS)
    if env.get("E2E_FLUX", "1") == "1":
        expected |= {step.replace("argocd.", "flux.", 1) for step in ARGO_STEPS}
    assert expected <= reached, sorted(expected - reached)
    assert result.returncode == 0
