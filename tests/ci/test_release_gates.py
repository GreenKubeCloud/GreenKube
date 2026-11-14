"""Release and nightly gates (production readiness): e2e loop, upgrade test, nightly ref."""

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = PROJECT_ROOT / ".github" / "workflows"


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # PyYAML reads the bare "on" key as the boolean True.
    return workflow.get("on", workflow.get(True))


def test_release_requires_the_e2e_loop_and_the_upgrade_test():
    jobs = _workflow("release.yml")["jobs"]

    assert jobs["e2e"]["uses"] == "./.github/workflows/e2e-nightly.yml"
    assert jobs["upgrade"]["uses"] == "./.github/workflows/upgrade-test.yml"
    assert {"e2e", "upgrade"} <= set(jobs["build-and-push"]["needs"])
    # Nothing is published without the image build, which needs every gate.
    assert "build-and-push" in jobs["publish-helm-chart"]["needs"]
    assert "build-and-push" in jobs["github-release"]["needs"]


def test_reusable_workflows_can_be_called():
    assert "workflow_call" in _triggers(_workflow("e2e-nightly.yml"))
    assert "workflow_call" in _triggers(_workflow("upgrade-test.yml"))


def test_scheduled_e2e_checks_out_the_integration_branch():
    workflow = _workflow("e2e-nightly.yml")
    checkout = workflow["jobs"]["loop"]["steps"][0]

    assert "schedule" in _triggers(workflow)
    assert workflow["env"]["E2E_NIGHTLY_REF"] == "dev"
    assert checkout["with"]["ref"] == "${{ github.event_name == 'schedule' && env.E2E_NIGHTLY_REF || '' }}"


def test_e2e_runs_on_pull_requests_touching_the_application_or_the_chart():
    paths = _triggers(_workflow("e2e-nightly.yml"))["pull_request"]["paths"]

    assert "src/greenkube/**" in paths
    assert "helm-chart/**" in paths


def test_upgrade_test_runs_on_migration_and_chart_changes():
    workflow = _workflow("upgrade-test.yml")
    paths = _triggers(workflow)["pull_request"]["paths"]
    job = workflow["jobs"]["upgrade"]

    assert "src/greenkube/core/migrations/**" in paths
    assert "helm-chart/**" in paths
    assert job["steps"][0]["with"]["fetch-depth"] == 0
    assert any(step.get("run") == "hack/upgrade/upgrade-test.sh" for step in job["steps"])
