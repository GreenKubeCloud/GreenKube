"""Cloud provider detection from providerID and provider-specific labels, never region prefixes (COL-15)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from greenkube.collectors import node_collector as node_collector_module
from greenkube.collectors.node_collector import NodeCollector

INSTANCE = "node.kubernetes.io/instance-type"
REGION = "topology.kubernetes.io/region"


def _node(provider_id: str = ""):
    return SimpleNamespace(spec=SimpleNamespace(provider_id=provider_id))


# The nine label sets of the audit's sim_provider.py.
CASES = [
    (
        "GKE us-central1",
        {INSTANCE: "e2-standard-4", REGION: "us-central1", "cloud.google.com/gke-nodepool": "p"},
        "gcp",
    ),
    ("GKE us-east4", {INSTANCE: "n2-standard-8", REGION: "us-east4", "cloud.google.com/machine-family": "n2"}, "gcp"),
    ("GKE europe-west1", {INSTANCE: "e2-medium", REGION: "europe-west1", "cloud.google.com/gke-os": "cos"}, "gcp"),
    ("Linode LKE us-east", {INSTANCE: "g6-standard-2", REGION: "us-east", "lke.linode.com/pool-id": "1"}, "unknown"),
    ("OCI OKE eu-frankfurt-1", {INSTANCE: "VM.Standard.E4.Flex", REGION: "eu-frankfurt-1"}, "unknown"),
    ("IBM IKS eu-de", {INSTANCE: "bx2.4x16", REGION: "eu-de", "ibm-cloud.kubernetes.io/worker-id": "w"}, "unknown"),
    ("Alibaba ACK ap-southeast-1", {INSTANCE: "ecs.g7.large", REGION: "ap-southeast-1"}, "unknown"),
    ("EKS us-east-1", {INSTANCE: "m5.large", REGION: "us-east-1", "eks.amazonaws.com/nodegroup": "ng"}, "aws"),
    (
        "EKS me-south-1 karpenter",
        {INSTANCE: "m5.large", REGION: "me-south-1", "karpenter.k8s.aws/instance-family": "m5"},
        "aws",
    ),
]


@pytest.fixture(autouse=True)
def _no_configured_provider(monkeypatch):
    monkeypatch.setattr(node_collector_module.global_config, "CLOUD_PROVIDER", "unknown", raising=False)


@pytest.mark.parametrize(("name", "labels", "expected"), CASES, ids=[case[0] for case in CASES])
def test_provider_is_detected_from_provider_labels_only(name, labels, expected):
    assert NodeCollector()._detect_cloud_provider(labels, _node()) == expected


@pytest.mark.parametrize(
    ("provider_id", "expected"),
    [
        ("gce://project/us-central1-a/gke-node-1", "gcp"),
        ("aws:///me-south-1a/i-0123456789abcdef0", "aws"),
        ("azure:///subscriptions/x/resourceGroups/y", "azure"),
        ("openstack:///4a2b0c1d", "unknown"),
        ("scaleway://instance/fr-par-1/abc", "scaleway"),
        ("linode://12345", "unknown"),
    ],
)
def test_provider_id_decides_before_labels(provider_id, expected):
    labels = {INSTANCE: "x", REGION: "us-central1"}
    assert NodeCollector()._detect_cloud_provider(labels, _node(provider_id)) == expected


def test_ovh_openstack_provider_id_with_ovh_labels_is_ovh():
    labels = {"node.k8s.ovh/type": "standard"}
    assert NodeCollector()._detect_cloud_provider(labels, _node("openstack:///abc")) == "ovh"


def test_configured_cloud_provider_only_fills_undetected_nodes(monkeypatch):
    """Review 5: providerID and provider labels win (mixed and hybrid clusters)."""
    monkeypatch.setattr(node_collector_module.global_config, "CLOUD_PROVIDER", "gcp", raising=False)
    eks = {INSTANCE: "m5.large", REGION: "us-east-1", "eks.amazonaws.com/nodegroup": "ng"}
    assert NodeCollector()._detect_cloud_provider(eks, _node("aws:///us-east-1a/i-1")) == "aws"
    assert NodeCollector()._detect_cloud_provider({INSTANCE: "x"}, _node()) == "gcp"


@pytest.mark.parametrize("alias", ["on-prem", "on-premise", "on-premises", "onprem", "on_prem"])
def test_on_prem_aliases_are_not_cloud_providers(monkeypatch, alias):
    """Review 5: an on-prem node is never looked up in a cloud catalogue or Boavizta's cloud API."""
    from greenkube.collectors.boavizta_collector import NON_CLOUD_PROVIDERS

    monkeypatch.setattr(node_collector_module.global_config, "CLOUD_PROVIDER", alias, raising=False)
    assert NodeCollector()._detect_cloud_provider({}, _node()) == alias
    assert alias in NON_CLOUD_PROVIDERS


@pytest.mark.parametrize("provider_id", ["k3s://node-1", "kind://docker/kind/kind-control-plane", "openstack:///abc"])
def test_an_unrecognised_provider_id_is_not_filled_from_the_configuration(monkeypatch, provider_id):
    """Re-review 3: CLOUD_PROVIDER only fills nodes without any providerID."""
    monkeypatch.setattr(node_collector_module.global_config, "CLOUD_PROVIDER", "gcp", raising=False)
    assert NodeCollector()._detect_cloud_provider({}, _node(provider_id)) == "unknown"
