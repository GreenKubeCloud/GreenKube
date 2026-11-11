"""One install topology (roadmap F11, F6, N2) and the upgrade path from 0.3.x (F17).

The upgrade checks render the released 0.3.1 chart (from the ``v0.3.1`` git tag)
and the current chart with the same values and compare what must survive the
upgrade: the bundled PostgreSQL StatefulSet, its volume claim, Service and
credentials Secret, the controller Lease, and the former SQLite claim. They need
no cluster (``helm template`` only) and are skipped when the tag is unavailable.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

CHART_PATH = Path("helm-chart")
RELEASE = "greenkube"
NAMESPACE = "greenkube"
LEGACY_TAG = "v0.3.1"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


def _run(chart: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = ["helm", "template", RELEASE, str(chart), "--namespace", NAMESPACE, *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=check)


def _docs(chart: Path, *args: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(_run(chart, *args).stdout) if isinstance(doc, dict)]


def _names(docs: list[dict], kind: str) -> set[str]:
    return {doc["metadata"]["name"] for doc in docs if doc.get("kind") == kind}


def _get(docs: list[dict], kind: str, name: str) -> dict:
    found = next((d for d in docs if d.get("kind") == kind and d["metadata"]["name"] == name), None)
    assert found is not None, f"{kind}/{name} is not rendered"
    return found


def _set(*pairs: str) -> list[str]:
    args: list[str] = []
    for pair in pairs:
        args.extend(["--set", pair])
    return args


def _cpu_millicores(value: str) -> float:
    text = str(value)
    return float(text[:-1]) if text.endswith("m") else float(text) * 1000


def _memory_mib(value: str) -> float:
    text = str(value)
    for suffix, factor in (("Gi", 1024.0), ("Mi", 1.0), ("Ki", 1 / 1024)):
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * factor
    return float(text) / (1024 * 1024)


# --------------------------------------------------------------------------- F11


def test_empty_values_render_exactly_the_one_install_topology():
    docs = _docs(CHART_PATH)

    assert _names(docs, "Deployment") == {"greenkube-api", "greenkube-controller", "greenkube-renderer"}
    assert _names(docs, "StatefulSet") == {"greenkube-postgres"}
    assert _names(docs, "Lease") == {"greenkube-leader"}
    assert {"greenkube-api", "greenkube-postgres", "greenkube-renderer"} <= _names(docs, "Service")
    assert "greenkube-renderer-netpol" in _names(docs, "NetworkPolicy")
    allowed = {
        "ServiceAccount",
        "ConfigMap",
        "ClusterRole",
        "ClusterRoleBinding",
        "Role",
        "RoleBinding",
        "Service",
        "Deployment",
        "StatefulSet",
        "Lease",
        "NetworkPolicy",
        "Job",  # the credentials bootstrap hook (N1) only
        "Pod",  # the `helm test` connection check
    }
    assert {doc["kind"] for doc in docs} <= allowed
    assert _names(docs, "Job") == {"greenkube-credentials-bootstrap"}


def test_default_requests_fit_an_evaluation_cluster():
    docs = _docs(CHART_PATH)
    cpu = memory = 0.0
    for doc in docs:
        if doc.get("kind") not in {"Deployment", "StatefulSet"}:
            continue
        replicas = doc["spec"].get("replicas", 1)
        for container in doc["spec"]["template"]["spec"]["containers"]:
            requests = container["resources"]["requests"]
            cpu += replicas * _cpu_millicores(requests["cpu"])
            memory += replicas * _memory_mib(requests["memory"])
    # Roadmap F11: about 325m CPU / 600 MiB including PostgreSQL.
    assert cpu <= 330
    assert memory <= 640


def test_high_availability_is_two_numbers():
    docs = _docs(CHART_PATH, *_set("api.replicas=2", "controller.replicas=2"))

    assert _get(docs, "Deployment", "greenkube-api")["spec"]["replicas"] == 2
    controller = _get(docs, "Deployment", "greenkube-controller")
    assert controller["spec"]["replicas"] == 2
    env = {e["name"]: e.get("value") for e in controller["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["LEADER_ELECTION_ENABLED"] == "true"
    assert env["DB_RUN_MIGRATIONS"] == "true"


def test_renderer_is_always_deployed_small_and_credential_free():
    docs = _docs(CHART_PATH, *_set("automation.enabled=false"))

    renderer = _get(docs, "Deployment", "greenkube-renderer")
    spec = renderer["spec"]["template"]["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert spec["containers"][0]["resources"]["requests"] == {"cpu": "25m", "memory": "96Mi"}
    assert all("secret" not in volume and "projected" not in volume for volume in spec["volumes"])


# ---------------------------------------------------------------------------- N2


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (["automation.prPollIntervalSeconds=5"], "prPollIntervalSeconds"),
        (["automation.syncTimeoutSeconds=30"], "syncTimeoutSeconds"),
        (["automation.policy.missingEvidence=ignore"], "missingEvidence"),
        (["auth.session.secureCookie=sometimes"], "secureCookie"),
        (["controller.metricsPort=70000"], "metricsPort"),
        (["image.digest=sha256:nope"], "digest"),
        (["config.apiAuthMode=open"], "apiAuthMode"),
        (["automation.providers[0].id=gh", "automation.providers[0].type=bitbucket"], "type"),
        (["api.replicas=0"], "replicas"),
    ],
)
def test_values_schema_rejects_invalid_values(values: list[str], message: str):
    result = _run(CHART_PATH, *_set(*values), check=False)

    assert result.returncode != 0
    assert message in result.stderr


# --------------------------------------------------------------------------- F17


@pytest.fixture(scope="module")
def legacy_chart(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The released 0.3.1 chart, extracted from its git tag."""
    archive = subprocess.run(
        ["git", "archive", "--format=tar", LEGACY_TAG, "helm-chart"],
        capture_output=True,
        check=False,
    )
    if archive.returncode != 0:
        pytest.skip(f"git tag {LEGACY_TAG} is not available")
    target = tmp_path_factory.mktemp("legacy")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(target, filter="data")
    return target / "helm-chart"


def _postgres_identity(docs: list[dict]) -> dict:
    statefulset = _get(docs, "StatefulSet", "greenkube-postgres")
    container = statefulset["spec"]["template"]["spec"]["containers"][0]
    password = next(e for e in container["env"] if e["name"] == "POSTGRES_PASSWORD")
    return {
        "statefulset": statefulset["metadata"]["name"],
        "selector": statefulset["spec"]["selector"]["matchLabels"],
        "claims": [claim["metadata"]["name"] for claim in statefulset["spec"]["volumeClaimTemplates"]],
        "service": _get(docs, "Service", "greenkube-postgres")["spec"]["selector"],
        "password_secret": password["valueFrom"]["secretKeyRef"]["name"],
    }


def test_standalone_upgrade_keeps_postgres_and_replaces_the_combined_pod(legacy_chart: Path):
    old = _docs(legacy_chart)
    new = _docs(CHART_PATH)

    assert _postgres_identity(new) == _postgres_identity(old)
    assert "greenkube" in _names(old, "Deployment")
    assert "greenkube" not in _names(new, "Deployment")
    assert {"greenkube-api", "greenkube-controller"} <= _names(new, "Deployment")


def test_production_upgrade_with_automation_keeps_its_data_lease_and_workers(legacy_chart: Path):
    values = _set(
        "profile=production",
        "automation.enabled=true",
        "production.components.api.replicas=2",
        "automation.providers[0].id=github",
        "automation.providers[0].type=github",
        "automation.providers[0].secretName=git-token",
        "automation.providers[0].secretKey=token",
        "automation.providers[0].repositoryAllowlist[0]=acme/*",
    )
    old = _docs(legacy_chart, *values)
    new = _docs(CHART_PATH, *values)

    assert _postgres_identity(new) == _postgres_identity(old)
    assert _names(new, "Lease") == _names(old, "Lease")
    for name in ("greenkube-api", "greenkube-controller"):
        old_selector = _get(old, "Deployment", name)["spec"]["selector"]
        assert _get(new, "Deployment", name)["spec"]["selector"] == old_selector
    assert _get(new, "Deployment", "greenkube-api")["spec"]["replicas"] == 2
    # 0.3.x production defaulted to worker mode: one dedicated worker keeps running.
    assert "greenkube-worker" in _names(new, "Deployment")
    config = _get(new, "ConfigMap", "greenkube-config")["data"]
    assert config["AUTOMATION_PAUSE_DELIVERY"] == "false"
    assert '"id":"github"' in config["GITOPS_PROVIDERS_JSON"]


def test_disabled_automation_upgrade_pauses_delivery_only(legacy_chart: Path):
    new = _docs(CHART_PATH, *_set("automation.enabled=false"))

    assert _get(new, "ConfigMap", "greenkube-config")["data"]["AUTOMATION_PAUSE_DELIVERY"] == "true"
    assert {"greenkube-api", "greenkube-controller", "greenkube-renderer"} <= _names(new, "Deployment")


def test_sqlite_upgrade_fails_with_the_exact_migration_commands(legacy_chart: Path):
    values = _set("config.db.type=sqlite", "config.persistence.enabled=true")
    old = _docs(legacy_chart, *values)
    assert "greenkube" in _names(old, "PersistentVolumeClaim")

    result = _run(CHART_PATH, *values, check=False)

    assert result.returncode != 0
    assert "config.db.type=sqlite is no longer supported" in result.stderr
    assert 'greenkube db copy --from sqlite:///data/greenkube_data.db --to "$DB_CONNECTION_STRING"' in result.stderr
    assert "--reset-then-reuse-values --set config.db.type=postgres --set controller.replicas=0" in result.stderr


def test_sqlite_claim_survives_the_switch_to_postgres(legacy_chart: Path):
    old_claim = _get(
        _docs(legacy_chart, *_set("config.db.type=sqlite", "config.persistence.enabled=true")),
        "PersistentVolumeClaim",
        "greenkube",
    )
    new = _docs(
        CHART_PATH,
        *_set("config.db.type=postgres", "config.persistence.enabled=true", "controller.replicas=0"),
    )

    claim = _get(new, "PersistentVolumeClaim", "greenkube")
    assert claim["spec"] == old_claim["spec"]
    assert claim["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    assert _get(new, "Deployment", "greenkube-controller")["spec"]["replicas"] == 0
    assert "greenkube-postgres" in _names(new, "StatefulSet")
