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

# Steps before the pull request: a resumed run (E2E_FRESH=0) skips them.
PRE_PR_STEPS = ("recommendation", "plan_preview", "approval")
LOOP_STEPS = (
    "automation_ready",
    "binding",
    *PRE_PR_STEPS,
    "pull_request",
    "merge",
    "controller_sync",
    "greenkube_merge_detected",
    "greenkube_sync_confirmed",
    "apply_detected",
    "measurement_started",
)


def expected_steps(controller: str, reached: set[str], env: dict[str, str]) -> set[str]:
    """Steps that a successful loop of ``controller`` records as ``ok``."""
    resumed = f"{controller}.resume" in reached
    steps = {f"{controller}.{step}" for step in LOOP_STEPS if not (resumed and step in PRE_PR_STEPS)}
    if env.get("E2E_WAIT_VERDICT", "1") == "1" and env.get("E2E_ALLOW_REGRESSION", "0") != "1":
        steps.add(f"{controller}.verdict")
    return steps


def test_live_loop(tmp_path: Path) -> None:
    env = {**os.environ, "E2E_STATE_DIR": os.getenv("E2E_STATE_DIR", str(tmp_path))}
    result = subprocess.run([str(REPO_ROOT / "hack" / "e2e" / "run.sh")], env=env, cwd=REPO_ROOT, check=False)

    steps_file = Path(env["E2E_STATE_DIR"]) / "evidence" / "steps.jsonl"
    steps = [json.loads(line) for line in steps_file.read_text().splitlines() if line.strip()]
    failed = [step for step in steps if step["status"] == "failed"]
    assert not failed, failed
    reached = {step["step"] for step in steps if step["status"] == "ok"}
    expected = expected_steps("argocd", reached, env)
    if env.get("E2E_FLUX", "1") == "1":
        expected |= expected_steps("flux", reached, env)
    assert expected <= reached, sorted(expected - reached)
    assert result.returncode == 0
