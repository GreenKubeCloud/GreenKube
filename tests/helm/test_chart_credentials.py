"""Regression tests for BUG-58 (roadmap N1): Argo CD-safe chart credentials.

Argo CD renders the chart with ``helm template``: ``lookup`` returns nothing
and every ``randAlphaNum`` call yields a new value, so a chart that generates
credentials at render time rotates the API key and the PostgreSQL password on
every sync while the database volume keeps the old password. Rendering must be
a pure function of the values; generated credentials are created once, at run
time, by the credentials bootstrap hook.
"""

import base64
import re
import subprocess
from pathlib import Path

import pytest
import yaml

CHART_PATH = Path("helm-chart")

# Helm/Sprig functions whose result depends on the cluster or on randomness.
NON_DETERMINISTIC_FUNCTIONS = (
    "lookup",
    "randAlphaNum",
    "randAlpha",
    "randNumeric",
    "randAscii",
    "randBytes",
    "randInt",
    "uuidv4",
    "now",
    "genPrivateKey",
    "genCA",
    "genCAWithKey",
    "genSelfSignedCert",
    "genSelfSignedCertWithKey",
    "genSignedCert",
    "genSignedCertWithKey",
)

RENDER_PROFILES = {
    "standalone": [],
    "standalone-automation": ["--set", "automation.enabled=true"],
    "standalone-automation-worker": ["--set", "automation.enabled=true", "--set", "automation.mode=worker"],
    "production": ["--set", "profile=production"],
    "ha": ["--set", "api.replicas=2", "--set", "controller.replicas=2"],
    "dedicated-worker": ["--set", "worker.replicas=1"],
    "production-automation-inline": [
        "--set",
        "profile=production",
        "--set",
        "automation.enabled=true",
        "--set",
        "automation.mode=inline",
    ],
    "former-sqlite-claim": ["--set", "config.db.type=postgres", "--set", "config.persistence.enabled=true"],
    "values-credentials": ["--set", "secrets.apiKey=k3y", "--set", "postgres.auth.password=pw"],
    "values-postgres-password-production": ["--set", "profile=production", "--set", "postgres.auth.password=pw"],
    "external-postgres": [
        "--set",
        "postgres.enabled=false",
        "--set",
        "secrets.dbConnectionString=postgresql://u:p@db:5432/greenkube",
    ],
}


def _render(*args: str) -> str:
    cmd = ["helm", "template", "greenkube", str(CHART_PATH), "--namespace", "greenkube", *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=True).stdout


def _documents(rendered: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict)]


def _find(docs: list[dict], kind: str, name: str) -> dict | None:
    return next(
        (doc for doc in docs if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name),
        None,
    )


def _get(docs: list[dict], kind: str, name: str) -> dict:
    found = _find(docs, kind, name)
    assert found is not None, f"{kind}/{name} is not rendered"
    return found


def _template_actions(text: str) -> list[str]:
    """Return template actions with comments and string literals removed."""
    text = re.sub(r"{{-?\s*/\*.*?\*/\s*-?}}", "", text, flags=re.DOTALL)
    actions = re.findall(r"{{.*?}}", text, flags=re.DOTALL)
    return [re.sub(r'"(?:\\.|[^"\\])*"|`[^`]*`', '""', action) for action in actions]


# NOTES.txt is printed by helm install/upgrade and never applied to the cluster, so it
# cannot make a sync unstable. It reads the initial-admin Secret (read-only) to stop
# pointing to a consumed password (N11); under `helm template` the lookup is empty.
NOTES_LOOKUP_ALLOWED = {"NOTES.txt": {"lookup"}}


def test_templates_do_not_call_cluster_lookups_or_random_generators():
    pattern = re.compile(r"\b(" + "|".join(NON_DETERMINISTIC_FUNCTIONS) + r")\b")
    offenders = []
    for template in sorted((CHART_PATH / "templates").rglob("*")):
        if not template.is_file():
            continue
        allowed = NOTES_LOOKUP_ALLOWED.get(template.name, set())
        for action in _template_actions(template.read_text(encoding="utf-8")):
            match = pattern.search(action)
            if match and match.group(1) not in allowed:
                offenders.append(f"{template}: {match.group(1)} in {action.strip()[:80]}")
    assert offenders == []


@pytest.mark.parametrize("args", RENDER_PROFILES.values(), ids=RENDER_PROFILES.keys())
def test_two_helm_template_renders_are_identical(args):
    assert _render(*args) == _render(*args)


def test_default_render_ships_no_generated_credentials():
    docs = _documents(_render())

    # Nothing is supplied in values, so no Secret is rendered at all. The
    # generated-credentials Secret is created by the bootstrap hook, never rendered.
    assert [doc["metadata"]["name"] for doc in docs if doc.get("kind") == "Secret"] == []


def test_values_secret_is_a_regular_release_resource():
    docs = _documents(_render("--set", "secrets.electricityMapsToken=tok"))

    values_secret = _get(docs, "Secret", "greenkube-values")
    assert "helm.sh/hook" not in (values_secret["metadata"].get("annotations") or {})
    assert set(values_secret["data"]) == {"ELECTRICITY_MAPS_TOKEN"}


def _projected_secret_for(docs: list[dict], deployment: str, key: str) -> str:
    volumes = _get(docs, "Deployment", deployment)["spec"]["template"]["spec"]["volumes"]
    names = [
        source["secret"]["name"]
        for volume in volumes
        for source in volume.get("projected", {}).get("sources", [])
        if "secret" in source and any(item["key"] == key for item in source["secret"].get("items", []))
    ]
    assert len(set(names)) == 1, f"{key} is projected from {names}"
    return names[0]


@pytest.mark.parametrize("key", ["BOAVIZTA_API_URL", "PROMETHEUS_URL", "OPENCOST_API_URL"])
def test_settings_page_service_url_override_survives_a_pod_restart(key):
    """With chart defaults, each service URL is read from Secret <release>, where
    the Settings page stores its overrides. A non-empty chart default would put
    the key in the values Secret, which the Pod reads instead, so an override
    would be lost on the next restart."""
    docs = _documents(_render())

    assert _projected_secret_for(docs, "greenkube-controller", key) == "greenkube"
    assert key not in _get(docs, "ConfigMap", "greenkube-config")["data"]


def test_service_url_set_in_values_wins_over_settings_page_overrides():
    docs = _documents(_render("--set", "config.boavizta.url=https://boavizta.example.com"))

    assert _projected_secret_for(docs, "greenkube-controller", "BOAVIZTA_API_URL") == "greenkube-values"
    values_secret = _get(docs, "Secret", "greenkube-values")
    assert base64.b64decode(values_secret["data"]["BOAVIZTA_API_URL"]).decode() == "https://boavizta.example.com"


def test_bootstrap_hook_creates_generated_credentials_once():
    docs = _documents(_render())

    job = _get(docs, "Job", "greenkube-credentials-bootstrap")
    assert job["metadata"]["annotations"]["helm.sh/hook"] == "pre-install,pre-upgrade"
    env = {item["name"]: item.get("value") for item in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["GREENKUBE_SECRET_NAME"] == "greenkube"
    assert env["GREENKUBE_GENERATE_API_KEY"] == "true"
    assert env["GREENKUBE_GENERATE_POSTGRES_PASSWORD"] == "true"
    assert env["GREENKUBE_POSTGRES_PVC"] == "data-greenkube-postgres-0"

    role = _get(docs, "Role", "greenkube-credentials-bootstrap")
    named_rules = [rule for rule in role["rules"] if rule.get("resourceNames")]
    assert {"secrets", "persistentvolumeclaims"} == {res for rule in named_rules for res in rule["resources"]}
    assert all(rule["verbs"] == ["create"] for rule in role["rules"] if not rule.get("resourceNames"))


def test_supplied_credentials_are_not_generated():
    docs = _documents(_render("--set", "secrets.apiKey=k3y", "--set", "postgres.auth.password=pw"))

    values_secret = _get(docs, "Secret", "greenkube-values")
    assert {"GREENKUBE_API_KEY", "POSTGRES_PASSWORD", "DB_CONNECTION_STRING"} <= set(values_secret["data"])
    job = _get(docs, "Job", "greenkube-credentials-bootstrap")
    env = {item["name"]: item.get("value") for item in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["GREENKUBE_GENERATE_API_KEY"] == "false"
    assert env["GREENKUBE_GENERATE_POSTGRES_PASSWORD"] == "false"

    statefulset = _get(docs, "StatefulSet", "greenkube-postgres")
    password = next(
        e for e in statefulset["spec"]["template"]["spec"]["containers"][0]["env"] if e["name"] == "POSTGRES_PASSWORD"
    )
    assert password["valueFrom"]["secretKeyRef"]["name"] == "greenkube-values"


def test_only_the_credentials_bootstrap_and_test_pod_are_hooks():
    """No migration or credential-check hook remains (F11): the controller migrates at
    startup. The credentials bootstrap stays a pre-install/pre-upgrade hook because it
    must create the generated credentials once, outside the release, before the pods
    that read them (N1, BUG-58)."""
    hooked = {
        (doc["kind"], doc["metadata"]["name"])
        for doc in _documents(_render("--set", "monitoring.serviceMonitor.enabled=true"))
        if "helm.sh/hook" in (doc["metadata"].get("annotations") or {})
    }
    assert hooked == {
        ("ServiceAccount", "greenkube-credentials-bootstrap"),
        ("Role", "greenkube-credentials-bootstrap"),
        ("RoleBinding", "greenkube-credentials-bootstrap"),
        ("Job", "greenkube-credentials-bootstrap"),
        ("Pod", "greenkube-test-connection"),
    }


def test_existing_secret_is_the_gitops_path():
    docs = _documents(_render("--set", "secrets.existingSecret=greenkube-credentials"))

    assert not [doc for doc in docs if doc.get("kind") == "Secret"]
    # The hook only creates the initial-admin Secret (F14): it never reads or
    # writes the user's Secret.
    job = _get(docs, "Job", "greenkube-credentials-bootstrap")
    env = {item["name"]: item.get("value") for item in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["GREENKUBE_SECRET_NAME"] == ""
    assert env["GREENKUBE_GENERATE_API_KEY"] == "false"
    assert env["GREENKUBE_GENERATE_POSTGRES_PASSWORD"] == "false"
    assert env["GREENKUBE_INITIAL_ADMIN_SECRET"] == "greenkube-initial-admin"
    role = _get(docs, "Role", "greenkube-credentials-bootstrap")
    assert all("greenkube-credentials" not in rule.get("resourceNames", []) for rule in role["rules"])
    declarative = _documents(
        _render("--set", "secrets.existingSecret=greenkube-credentials", "--set", "auth.admin.existingSecret=admin")
    )
    assert _find(declarative, "Job", "greenkube-credentials-bootstrap") is None
    statefulset = _get(docs, "StatefulSet", "greenkube-postgres")
    password = next(
        e for e in statefulset["spec"]["template"]["spec"]["containers"][0]["env"] if e["name"] == "POSTGRES_PASSWORD"
    )
    assert password["valueFrom"]["secretKeyRef"]["name"] == "greenkube-credentials"


def test_generation_without_rbac_fails_with_guidance():
    cmd = ["helm", "template", "greenkube", str(CHART_PATH), "--set", "rbac.create=false"]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode != 0
    assert "secrets.existingSecret" in result.stderr


def test_bootstrap_job_runs_the_tested_script_verbatim():
    docs = _documents(_render())

    container = _get(docs, "Job", "greenkube-credentials-bootstrap")["spec"]["template"]["spec"]["containers"][0]
    script = (CHART_PATH / "files" / "credentials_bootstrap.py").read_text(encoding="utf-8")
    assert container["command"] == ["python", "-I", "-B", "-c"]
    assert container["args"] == [script]
    # The hook runs in the application image; the tag follows values.image.tag
    # (rewritten by scripts/sync_version.py at release), so never hardcode it.
    values = yaml.safe_load((CHART_PATH / "values.yaml").read_text(encoding="utf-8"))
    app_image = _get(docs, "Deployment", "greenkube-controller")["spec"]["template"]["spec"]["containers"][0]["image"]
    assert container["image"] == app_image
    assert container["image"] == f"{values['image']['repository']}:{values['image']['tag']}"
