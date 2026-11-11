"""Container port names must be valid IANA service names (at most 15 characters).

The API server rejects longer names, so an over-long name blocks every install
even though `helm template` and `helm lint` accept it.
"""

import re
import subprocess

import pytest
import yaml

CHART_PATH = "helm-chart"
_IANA_SVC_NAME = re.compile(r"^(?=.{1,15}$)(?=.*[a-z])[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def _render(*set_values: str) -> list[dict]:
    cmd = ["helm", "template", "greenkube", CHART_PATH, "--namespace", "greenkube"]
    for value in set_values:
        cmd.extend(["--set", value])
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def _pod_specs(doc: dict) -> list[dict]:
    if doc.get("kind") in {"Deployment", "StatefulSet", "Job"}:
        return [doc["spec"]["template"]["spec"]]
    if doc.get("kind") == "Pod":
        return [doc["spec"]]
    return []


@pytest.mark.parametrize(
    "set_values",
    [
        (),
        ("worker.replicas=1",),
        ("worker.replicas=1", "monitoring.serviceMonitor.enabled=true", "renderer.egressFirewall.enabled=true"),
    ],
)
def test_named_ports_are_valid_iana_service_names(set_values: tuple[str, ...]) -> None:
    docs = _render(*set_values)
    names: set[str] = set()
    for doc in docs:
        for spec in _pod_specs(doc):
            for container in spec.get("initContainers", []) + spec.get("containers", []):
                names.update(port["name"] for port in container.get("ports", []) if "name" in port)
        if doc.get("kind") == "Service":
            for port in doc["spec"]["ports"]:
                target = port.get("targetPort")
                if isinstance(target, str):
                    names.add(target)
    invalid = sorted(name for name in names if not _IANA_SVC_NAME.match(name))
    assert not invalid, f"port names longer than 15 characters or not IANA service names: {invalid}"
