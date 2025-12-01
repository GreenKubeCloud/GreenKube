# src/greenkube/data/region_mapping.py

"""
Mapping table between cloud provider region prefixes and the zone codes
used by Electricity Maps.

Sources:
- GCP: https://cloud.google.com/about/locations
- AWS: https://aws.amazon.com/about-aws/global-infrastructure/
- Azure: https://azure.microsoft.com/en-us/explore/global-infrastructure/regions/
- OVHcloud: https://www.ovhcloud.com/en/about-us/global-infrastructure/regions/
- Scaleway: https://www.scaleway.com/en/docs/account/reference-content/products-availability/
"""

import csv
import logging
from pathlib import Path
from typing import Dict, Tuple

from greenkube.models.region_mapping import RegionMapping

logger = logging.getLogger(__name__)


def load_region_mappings() -> Tuple[Dict[Tuple[str, str], str], Dict[str, str]]:
    """
    Load region mappings from CSV using Pydantic for validation.

    Returns:
        Tuple containing:
        - provider_mapping: Dict[(provider, region_id), em_zone]
        - fallback_mapping: Dict[region_id, em_zone]
    """
    data_dir = Path(__file__).parent
    mapping_file = data_dir / "cloud_region_electricity_maps_mapping.csv"

    provider_mapping = {}
    fallback_mapping = {}

    try:
        with open(mapping_file, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    # Validate row using Pydantic model
                    mapping = RegionMapping(**row)
                except Exception as e:
                    logger.warning(f"Skipping invalid row in region mapping CSV: {row} - Error: {e}")
                    continue

                provider = mapping.cloud_provider
                region_id = mapping.region_id
                em_zone = mapping.electricity_maps_zone

                # Populate provider-specific mapping
                # Map provider names to match what NodeCollector detects
                # CSV: "Google Cloud Platform", "Amazon Web Services", "Microsoft Azure", "OVH Cloud", "Scaleway"
                # NodeCollector: "gcp", "aws", "azure", "ovh", "unknown" (Scaleway might be "unknown" or need update)

                # Normalize provider name to lowercase for easier matching
                norm_provider = provider.lower()
                if "google" in norm_provider:
                    key_provider = "gcp"
                elif "amazon" in norm_provider or "aws" in norm_provider:
                    key_provider = "aws"
                elif "azure" in norm_provider:
                    key_provider = "azure"
                elif "ovh" in norm_provider:
                    key_provider = "ovh"
                elif "scaleway" in norm_provider:
                    key_provider = "scaleway"
                else:
                    key_provider = norm_provider

                provider_mapping[(key_provider, region_id)] = em_zone

                # Populate fallback mapping (last one wins if duplicates, which is acceptable for fallback)
                fallback_mapping[region_id] = em_zone

        logger.info(f"Loaded {len(provider_mapping)} region mappings from CSV")
        return provider_mapping, fallback_mapping

    except FileNotFoundError:
        logger.error(f"Region mapping file not found: {mapping_file}")
        return {}, {}
    except Exception as e:
        logger.error(f"Unexpected error loading region mappings: {e}")
        return {}, {}


# Load mappings at module import time
PROVIDER_REGION_TO_EM_ZONE, CLOUD_REGION_TO_ELECTRICITY_MAPS_ZONE = load_region_mappings()
