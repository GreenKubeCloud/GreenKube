"""CI hygiene guards (roadmap O6, BUG-62)."""

import tomllib
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = PROJECT_ROOT / ".github" / "workflows"


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _run_scripts(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_ci_does_not_write_to_the_repository():
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    workflow = _workflow("ci.yml")

    assert "contents: write" not in text
    assert "git-auto-commit-action" not in text
    assert "README badges" not in text
    assert workflow["permissions"] == {"contents": "read"}


def test_ci_python_job_has_time_for_the_full_suite():
    job = _workflow("ci.yml")["jobs"]["lint-and-test"]

    assert job["timeout-minutes"] >= 20
    assert "uv run pytest --cov=greenkube" in _run_scripts(job)


def test_coverage_gate_is_80_percent():
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["tool"]["coverage"]["report"]["fail_under"] >= 80


def test_ci_checks_that_helm_renders_are_stable():
    assert "diff -u /tmp/render-1.yaml /tmp/render-2.yaml" in _run_scripts(_workflow("ci.yml")["jobs"]["helm-lint"])


def test_release_is_gated_on_frontend_tests():
    jobs = _workflow("release.yml")["jobs"]

    assert set(jobs["build-and-push"]["needs"]) == {"test", "frontend-test"}
    scripts = _run_scripts(jobs["frontend-test"])
    assert "npm ci" in scripts
    assert "npm test" in scripts
    assert jobs["test"]["timeout-minutes"] >= 20


def test_release_pushes_latest_for_the_newest_release():
    jobs = _workflow("release.yml")["jobs"]
    build = next(step for step in jobs["build-and-push"]["steps"] if step.get("name") == "Build and push")

    assert "greenkube/greenkube:latest" in build["with"]["tags"]
    assert "steps.version.outputs.latest == 'true'" in build["with"]["tags"]


def test_release_generates_artifacthub_changes_before_packaging():
    steps = _workflow("release.yml")["jobs"]["publish-helm-chart"]["steps"]
    names = [step.get("name", "") for step in steps]

    generate = names.index("Generate the Artifact Hub changes list from the CHANGELOG")
    assert generate < names.index("Package Helm Chart")


def test_dependabot_covers_every_ecosystem():
    config = yaml.safe_load((PROJECT_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    ecosystems = {(update["package-ecosystem"], update["directory"]) for update in config["updates"]}

    assert ecosystems == {("uv", "/"), ("npm", "/frontend"), ("github-actions", "/"), ("docker", "/")}
    assert all(update["target-branch"] == "dev" for update in config["updates"])


def test_generated_coverage_reports_are_ignored():
    ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "coverage.json" in ignored
    assert "coverage.xml" in ignored
