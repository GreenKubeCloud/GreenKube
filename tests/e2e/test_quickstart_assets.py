"""Static checks of the quickstart assets used by ``make e2e`` (F9, O1).

They run in the normal suite: a typo in the quickstart values or sample
repository would otherwise only surface on a live cluster.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from greenkube.core.config import Config

REPO_ROOT = Path(__file__).resolve().parents[2]
QUICKSTART = REPO_ROOT / "docs" / "examples" / "quickstart"
GITOPS_REPO = QUICKSTART / "gitops-repo"
REPO_URL = "http://gitea-http.gitea.svc.cluster.local:3000/greenkube/greenkube-e2e-quickstart.git"


def _load(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text()) if isinstance(doc, dict)]


def _values() -> dict:
    return yaml.safe_load((QUICKSTART / "values.yaml").read_text())


def _parse_quantity_millicores(value: str) -> int:
    return int(value[:-1]) if value.endswith("m") else int(float(value) * 1000)


def _parse_quantity_mib(value: str) -> int:
    units = {"Mi": 1, "Gi": 1024}
    return int(value[:-2]) * units[value[-2:]]


def test_one_min_samples_knob_sets_every_sample_gate() -> None:
    values = _values()
    # N3: config.recommendations.minSamples also sets the policy and verification gates.
    assert "minSamples" not in values["automation"]["policy"]
    assert "minSamples" not in values["config"]["recommendations"]["verification"]


def test_quickstart_accelerates_evidence() -> None:
    values = _values()
    assert values["config"]["prometheus"]["queryRangeStep"] == "30s"
    assert values["config"]["recommendations"]["minSamples"] <= 6
    # F12: automation is always on; no feature flag in the values.
    assert "enabled" not in values["automation"]
    assert "profile" not in values
    provider = values["automation"]["providers"][0]
    assert provider["type"] == "gitea"
    assert provider["repositoryAllowlist"] == ["greenkube/greenkube-e2e-quickstart"]


@pytest.mark.parametrize(
    ("path", "name", "namespace"),
    [
        (GITOPS_REPO / "apps" / "oversized-web" / "deployment.yaml", "oversized-web", "greenkube-e2e-apps"),
        # The Flux sample takes its namespace from the Kustomization's targetNamespace.
        (GITOPS_REPO / "flux" / "oversized-worker" / "deployment.yaml", "oversized-worker", None),
    ],
)
def test_sample_workloads_are_oversized(path: Path, name: str, namespace: str | None) -> None:
    (deployment,) = [doc for doc in _load(path) if doc["kind"] == "Deployment"]
    assert deployment["metadata"]["name"] == name
    assert deployment["metadata"].get("namespace") == namespace
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    requests = container["resources"]["requests"]
    # Idle containers use a few millicores and MiB: these requests leave room
    # for a rightsizing recommendation well above the 30% thresholds.
    assert _parse_quantity_millicores(requests["cpu"]) >= 250
    assert _parse_quantity_mib(requests["memory"]) >= 256


def test_gitops_objects_point_at_the_sample_repository() -> None:
    (application,) = _load(QUICKSTART / "argocd-application.yaml")
    source = application["spec"]["source"]
    assert source["repoURL"] == REPO_URL
    assert (GITOPS_REPO / source["path"]).is_dir()
    assert application["spec"]["destination"]["namespace"] == "greenkube-e2e-apps"

    flux = {doc["kind"]: doc for doc in _load(QUICKSTART / "flux-kustomization.yaml")}
    assert flux["GitRepository"]["spec"]["url"] == REPO_URL
    kustomization_path = flux["Kustomization"]["spec"]["path"]
    overlay = yaml.safe_load((GITOPS_REPO / kustomization_path / "kustomization.yaml").read_text())
    assert flux["Kustomization"]["spec"]["sourceRef"]["name"] == flux["GitRepository"]["metadata"]["name"]
    # A plain overlay: Flux's targetNamespace alone places the workload (the planner
    # renders the overlay as Flux builds it), which is what the e2e loop exercises.
    assert flux["Kustomization"]["spec"]["targetNamespace"] == "greenkube-e2e-apps"
    assert "namespace" not in overlay


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_quickstart_values_render_a_valid_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    result = subprocess.run(
        [
            "helm",
            "template",
            "greenkube",
            str(REPO_ROOT / "helm-chart"),
            "--namespace",
            "greenkube",
            "-f",
            str(QUICKSTART / "values.yaml"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]
    (configmap,) = [doc for doc in docs if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "greenkube-config"]
    data = configmap["data"]
    assert data["PROMETHEUS_QUERY_RANGE_STEP"] == "30s"
    assert data["RECOMMENDATION_MIN_SAMPLES"] == "6"
    assert data["AUTOMATION_PAUSE_DELIVERY"] == "false"
    assert data["VERIFICATION_MIN_SAMPLES"] == "6"
    assert '"minSamples":6' in data["GITOPS_POLICY_JSON"]

    # The settings the controller and API will read must pass validation.
    for key, value in data.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GREENKUBE_API_KEY", "e2e-test-key")
    config = Config()
    assert config.RECOMMENDATION_MIN_SAMPLES == 6
    assert config.VERIFICATION_WINDOW_HOURS == pytest.approx(0.1)
    assert config.recommendation_scan_interval.total_seconds() == 60
