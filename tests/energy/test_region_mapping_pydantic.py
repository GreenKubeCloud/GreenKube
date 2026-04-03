import logging
import sys
from pathlib import Path

import pytest

# Add src to path
sys.path.append(str(Path(__file__).parent.parent / "src"))

from greenkube.data.region_mapping import CLOUD_REGION_TO_ELECTRICITY_MAPS_ZONE, PROVIDER_REGION_TO_EM_ZONE
from greenkube.utils.mapping_translator import get_emaps_zone_from_cloud_zone

logging.basicConfig(level=logging.INFO)


def test_mappings():
    print(f"Loaded {len(PROVIDER_REGION_TO_EM_ZONE)} provider mappings")
    print(f"Loaded {len(CLOUD_REGION_TO_ELECTRICITY_MAPS_ZONE)} fallback mappings")

    scaleway_keys = [k for k in PROVIDER_REGION_TO_EM_ZONE.keys() if k[0] == "scaleway"]
    print(f"Scaleway keys: {scaleway_keys}")

    # Test cases
    cases = [
        # (zone, provider, expected_zone)
        ("europe-west9-a", "gcp", "FR"),
        ("us-east-1a", "aws", "US-MIDA-PJM"),
        ("eastus", "azure", "US-MIDA-PJM"),
        ("rbx", "ovh", "FR"),
        ("PAR1", "scaleway", "FR"),
        # Fallback cases (no provider or unknown provider)
        ("europe-west9-a", None, "FR"),
        ("us-east-1a", None, "US-MIDA-PJM"),  # Should map to AWS fallback
        ("eastus", None, "US-MIDA-PJM"),  # Should map to Azure fallback
    ]

    for zone, provider, expected in cases:
        result = get_emaps_zone_from_cloud_zone(zone, provider=provider)
        print(f"Zone: {zone}, Provider: {provider} -> Result: {result}, Expected: {expected}")
        if result != expected:
            print(f"❌ FAILED: {zone} (provider={provider}) mapped to {result}, expected {expected}")
        else:
            print("✅ PASSED")


# --- OVH OpenStack "nova" zone handling ---
# OVH Managed Kubernetes exposes:
#   topology.kubernetes.io/zone   = "nova"          (OpenStack AZ name — useless for mapping)
#   topology.kubernetes.io/region = "GRA11"         (data-centre code with numeric suffix)
# The correct flow is: skip "nova", fall through to region, strip the numeric
# suffix from "GRA11" → "GRA", look up ("ovh", "GRA") → "FR".


@pytest.mark.parametrize(
    "cloud_zone, provider, expected",
    [
        # Region trigrams (exact CSV keys)
        ("GRA", "ovh", "FR"),
        ("RBX", "ovh", "FR"),
        ("SBG", "ovh", "FR"),
        ("WAW", "ovh", "PL"),
        ("BHS", "ovh", "CA-QC"),
        # Numbered data-centre codes → strip suffix → trigram
        ("GRA11", "ovh", "FR"),
        ("GRA7", "ovh", "FR"),
        ("RBX8", "ovh", "FR"),
        ("SBG5", "ovh", "FR"),
        ("WAW1", "ovh", "PL"),
        ("BHS5", "ovh", "CA-QC"),
        # New long-form region IDs (OVHcloud modern API)
        ("eu-west-par", "ovh", "FR"),
        ("eu-west-gra", "ovh", "FR"),
        ("eu-west-rbx", "ovh", "FR"),
        ("eu-west-sbg", "ovh", "FR"),
        ("eu-central-waw", "ovh", "PL"),
        ("ca-east-bhs", "ovh", "CA-QC"),
        ("us-east-vin", "ovh", "US-MIDA-PJM"),
        ("ap-southeast-sgp", "ovh", "SG"),
        ("ap-southeast-syd", "ovh", "AU-NSW"),
        # AZ-level long-form (strip -a/-b suffix)
        ("eu-west-par-a", "ovh", "FR"),
        ("eu-west-gra-a", "ovh", "FR"),
        ("eu-central-waw-a", "ovh", "PL"),
        # "nova" itself must NOT map — caller should use region instead
        ("nova", "ovh", None),
    ],
)
def test_ovh_zone_mapping(cloud_zone, provider, expected):
    """OVH zone/region codes must resolve to the correct Electricity Maps zone."""
    result = get_emaps_zone_from_cloud_zone(cloud_zone, provider=provider)
    assert result == expected, f"Expected {expected!r} for ({cloud_zone!r}, {provider!r}), got {result!r}"


# --- OVH provider detection via node labels ---


def test_ovh_provider_detection_via_node_k8s_ovh_label():
    """node.k8s.ovh/type label (used by OVHcloud MKS) must be detected as 'ovh'."""
    from greenkube.collectors.node_collector import NodeCollector

    collector = NodeCollector()

    # Labels as seen on real OVHcloud MKS nodes (no k8s.ovh.net/ prefix)
    ovh_labels = {
        "beta.kubernetes.io/arch": "amd64",
        "beta.kubernetes.io/instance-type": "b3-16",
        "beta.kubernetes.io/os": "linux",
        "failure-domain.beta.kubernetes.io/region": "GRA11",
        "failure-domain.beta.kubernetes.io/zone": "nova",
        "kubernetes.io/arch": "amd64",
        "kubernetes.io/hostname": "nodepool-prep-node-3a7e1d",
        "kubernetes.io/os": "linux",
        "node.k8s.ovh/type": "standard",
        "node.kubernetes.io/instance-type": "b3-16",
        "nodepool": "nodepool",
        "topology.cinder.csi.openstack.org/zone": "nova",
        "topology.kubernetes.io/region": "GRA11",
        "topology.kubernetes.io/zone": "nova",
    }

    assert collector._detect_cloud_provider(ovh_labels) == "ovh"
