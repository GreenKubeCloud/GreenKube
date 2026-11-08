"""CI hygiene guards (roadmap O6, BUG-62)."""

import os
import subprocess
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
    scripts = _run_scripts(_workflow("ci.yml")["jobs"]["helm-lint"])

    assert "diff -u /tmp/render-1.yaml /tmp/render-2.yaml" in scripts
    # Values-supplied credentials also render the db-credential-check hook Secret.
    assert "check --set secrets.apiKey=ci-api-key --set postgres.auth.password=ci-password" in scripts


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


def _latest_output(repo: Path, tags: list[str], ref_name: str) -> str:
    """Run the release 'Extract version from tag' step against *tags*."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    git = ["git", "-C", str(repo), "-c", "user.name=ci", "-c", "user.email=ci@example.com"]
    if not (repo / ".git").exists():
        subprocess.run([*git, "init", "-q"], check=True, env=env)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "--no-verify", "-m", "init"], check=True, env=env)
    for tag in tags:
        subprocess.run([*git, "tag", "-f", tag], check=True, env=env, capture_output=True)

    step = next(
        step
        for step in _workflow("release.yml")["jobs"]["build-and-push"]["steps"]
        if step.get("name") == "Extract version from tag"
    )
    output = repo / "github-output"
    output.write_text("", encoding="utf-8")
    env.update(GITHUB_OUTPUT=str(output), GITHUB_REF_NAME=ref_name)
    # GitHub runs bash steps with -e and -o pipefail.
    subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]], cwd=repo, check=True, env=env
    )
    outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    return outputs["latest"]


def test_release_moves_latest_only_for_the_highest_stable_tag(tmp_path):
    tags = ["v0.4.0", "v0.5.0", "v0.5.0-rc1", "v0.4.1"]

    assert _latest_output(tmp_path, tags, "v0.5.0") == "true"
    assert _latest_output(tmp_path, tags, "v0.4.1") == "false"
    assert _latest_output(tmp_path, ["v0.6.0-rc1"], "v0.5.0") == "true"


def test_release_latest_step_tolerates_a_repository_without_release_tags(tmp_path):
    assert _latest_output(tmp_path, ["v1.0.0-rc1"], "v1.0.0-rc1") == "false"


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
