"""Regression tests for the standalone and production Helm profiles."""

import subprocess

import yaml

CHART_PATH = "helm-chart"


def helm_template(*set_values: str) -> list[dict]:
    cmd = ["helm", "template", "greenkube", CHART_PATH, "--namespace", "greenkube"]
    for value in set_values:
        cmd.extend(["--set", value])
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [
        parsed
        for raw in result.stdout.split("---")
        if raw.strip()
        for parsed in [yaml.safe_load(raw)]
        if isinstance(parsed, dict)
    ]


def manifests(docs: list[dict], kind: str) -> list[dict]:
    return [doc for doc in docs if doc.get("kind") == kind]


def test_standalone_profile_keeps_one_combined_deployment():
    docs = helm_template()
    deployments = manifests(docs, "Deployment")
    assert len(deployments) == 1
    assert deployments[0]["metadata"]["name"] == "greenkube"
    assert len(deployments[0]["spec"]["template"]["spec"]["containers"]) == 2


def test_production_profile_separates_components_and_wires_lease():
    docs = helm_template("profile=production")
    deployments = manifests(docs, "Deployment")
    assert {doc["metadata"]["name"] for doc in deployments} == {
        "greenkube-api",
        "greenkube-controller",
        "greenkube-automation",
    }
    assert len(manifests(docs, "Job")) >= 1
    assert len(manifests(docs, "Lease")) == 1
    assert manifests(docs, "ServiceAccount")
    assert manifests(docs, "ClusterRoleBinding")[0]["subjects"][0]["name"] == "greenkube-controller"


def test_production_api_service_selects_only_api_component():
    docs = helm_template("profile=production")
    service = next(doc for doc in manifests(docs, "Service") if doc["metadata"]["name"] == "greenkube-api")
    assert service["spec"]["selector"]["app.kubernetes.io/component"] == "api"
