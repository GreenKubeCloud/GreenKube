"""CodeQL scans the Python backend and the JavaScript frontend (security hardening)."""

from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/security.yml")


def test_codeql_analyses_python_and_javascript_with_pinned_actions():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    job = workflow["jobs"]["codeql"]

    assert job["permissions"]["security-events"] == "write"
    assert set(job["strategy"]["matrix"]["language"]) == {"python", "javascript-typescript"}
    uses = [step["uses"] for step in job["steps"] if "uses" in step]
    assert any(item.startswith("github/codeql-action/init@") for item in uses)
    assert any(item.startswith("github/codeql-action/analyze@") for item in uses)
    # Every third-party action is pinned to a full commit SHA.
    assert all(len(item.split("@", 1)[1]) == 40 for item in uses)
    init = next(step for step in job["steps"] if step.get("uses", "").startswith("github/codeql-action/init@"))
    assert init["with"]["queries"] == "security-extended"
