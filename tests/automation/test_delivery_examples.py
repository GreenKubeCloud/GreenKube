"""The shipped delivery examples stay consistent with the API (roadmap F3, F4)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

EXAMPLES = Path(__file__).resolve().parents[2] / "docs" / "examples"


def test_github_app_manifest_requests_only_the_permissions_greenkube_uses():
    manifest = json.loads((EXAMPLES / "github-app-manifest.json").read_text())

    assert manifest["public"] is False
    assert manifest["default_permissions"] == {
        "metadata": "read",
        "contents": "write",
        "pull_requests": "write",
        "statuses": "write",
    }
    assert manifest["hook_attributes"]["url"].endswith("/api/v1/webhooks/inferred-github.com")


def test_ci_delivery_workflow_uses_the_delivery_endpoints_with_a_read_only_key():
    text = (EXAMPLES / "github-actions-greenkube-delivery.yaml").read_text()
    workflow = yaml.safe_load(text)

    assert workflow["permissions"] == {"contents": "write", "pull-requests": "write"}
    script = workflow["jobs"]["deliver"]["steps"][1]["run"]
    assert "/automation/plans?status=approved&delivery=ci" in script
    assert "/automation/plans/${id}/patch" in script
    assert "/automation/plans/${id}/delivery" in script
    assert "git am --3way" in script
    assert '"read" and "plans:deliver"' in text
