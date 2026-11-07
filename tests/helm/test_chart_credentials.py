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
    "production-values-file": ["-f", str(CHART_PATH / "values-production.yaml")],
    "production-automation-inline": [
        "--set",
        "profile=production",
        "--set",
        "automation.enabled=true",
        "--set",
        "automation.mode=inline",
    ],
    "sqlite": ["--set", "config.db.type=sqlite"],
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


def test_templates_do_not_call_cluster_lookups_or_random_generators():
    pattern = re.compile(r"\b(" + "|".join(NON_DETERMINISTIC_FUNCTIONS) + r")\b")
    offenders = []
    for template in sorted((CHART_PATH / "templates").rglob("*")):
        if not template.is_file():
            continue
        for action in _template_actions(template.read_text(encoding="utf-8")):
            match = pattern.search(action)
            if match:
                offenders.append(f"{template}: {match.group(1)} in {action.strip()[:80]}")
    assert offenders == []


@pytest.mark.parametrize("args", RENDER_PROFILES.values(), ids=RENDER_PROFILES.keys())
def test_two_helm_template_renders_are_identical(args):
    assert _render(*args) == _render(*args)


def test_default_render_ships_no_generated_credentials():
    docs = _documents(_render())

    values_secret = _get(docs, "Secret", "greenkube-values")
    assert "helm.sh/hook" not in (values_secret["metadata"].get("annotations") or {})
    assert set(values_secret["data"]) == {"BOAVIZTA_API_URL"}
    # The generated-credentials Secret is created by the bootstrap hook, never rendered.
    assert _find(docs, "Secret", "greenkube") is None


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


def _hook_weight(doc: dict) -> int:
    return int(doc["metadata"]["annotations"]["helm.sh/hook-weight"])


def test_credential_check_validates_a_newly_supplied_postgres_password():
    """The values Secret is applied after pre-upgrade hooks (and after Argo CD
    PreSync), so the check must read the new password from a hook Secret that
    exists before it runs, not the previous revision's values Secret."""
    docs = _documents(_render("--set", "postgres.auth.password=new-password"))

    job = _get(docs, "Job", "greenkube-db-credential-check")
    dsn_ref = job["spec"]["template"]["spec"]["containers"][0]["env"][0]
    assert dsn_ref["name"] == "DB_CONNECTION_STRING"
    check_secret = _get(docs, "Secret", dsn_ref["valueFrom"]["secretKeyRef"]["name"])

    annotations = check_secret["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "pre-upgrade"
    assert _hook_weight(check_secret) < _hook_weight(job)
    assert "hook-succeeded" in annotations["helm.sh/hook-delete-policy"]
    assert set(check_secret["data"]) == {"DB_CONNECTION_STRING"}
    dsn = base64.b64decode(check_secret["data"]["DB_CONNECTION_STRING"]).decode()
    assert dsn == "postgresql://greenkube:new-password@greenkube-postgres:5432/greenkube"


def test_credential_check_reads_generated_credentials_after_the_bootstrap_hook():
    docs = _documents(_render())

    job = _get(docs, "Job", "greenkube-db-credential-check")
    dsn_ref = job["spec"]["template"]["spec"]["containers"][0]["env"][0]["valueFrom"]["secretKeyRef"]
    assert dsn_ref["name"] == "greenkube"
    bootstrap = _get(docs, "Job", "greenkube-credentials-bootstrap")
    assert _hook_weight(bootstrap) < _hook_weight(job)
    assert _find(docs, "Secret", "greenkube-db-credential-check") is None


def test_existing_secret_is_the_gitops_path():
    docs = _documents(_render("--set", "secrets.existingSecret=greenkube-credentials"))

    assert not [doc for doc in docs if doc.get("kind") == "Secret"]
    assert _find(docs, "Job", "greenkube-credentials-bootstrap") is None
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
    app_image = _get(docs, "Deployment", "greenkube")["spec"]["template"]["spec"]["containers"][0]["image"]
    assert container["image"] == app_image
    assert container["image"] == f"{values['image']['repository']}:{values['image']['tag']}"
