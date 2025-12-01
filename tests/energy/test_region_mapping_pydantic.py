import logging
import sys
from pathlib import Path

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
