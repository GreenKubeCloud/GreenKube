# src/greenkube/data/instance_profiles.py

"""
Database of power consumption profiles for cloud instances.

This data is essential for the "Basic Estimation Engine".
It is based on open-source data from the Cloud Carbon Footprint project.
We use provider-level averages for power consumption estimates per vCPU.

Source: https://www.cloudcarbonfootprint.org/docs/methodology/#appendix-ii-machine-type-power-usage
"""

import csv
import logging
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


def load_provider_estimates(data_dir: Path) -> Dict[str, Dict[str, float]]:
    """
    Load provider power estimates from CSV.
    Returns a dict: {provider_name: {'min_watts': float, 'max_watts': float}}
    """
    estimates_file = data_dir / "provider_power_estimates.csv"
    estimates = {}
    try:
        with open(estimates_file, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                provider = row["provider"]
                estimates[provider] = {
                    "min_watts": float(row["min_watts"]),
                    "max_watts": float(row["max_watts"]),
                }
        return estimates
    except Exception as e:
        logger.error(f"Failed to load provider estimates from {estimates_file}: {e}")
        return {}


def load_instance_profiles() -> Dict[str, Any]:
    """
    Load instance profiles from CSV files.

    Combines instance definitions from instance_power_profiles.csv
    with power estimates from provider_power_estimates.csv.

    Returns:
        Dictionary mapping instance type names to profile data (vcores, minWatts, maxWatts)
    """
    data_dir = Path(__file__).parent

    # Load provider estimates
    provider_estimates = load_provider_estimates(data_dir)

    # Map provider names in instance CSV to provider names in estimates CSV
    # Instance CSV uses "Google Cloud", estimates CSV uses "GCP"
    provider_map = {"Google Cloud": "GCP", "AWS": "AWS", "Azure": "Azure"}

    profiles_file = data_dir / "instance_power_profiles.csv"
    profiles = {}

    try:
        with open(profiles_file, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                instance_type = row["instance_type"]
                vcores = int(row["vcores"])
                provider_raw = row["provider"]

                # Map provider name
                provider_key = provider_map.get(provider_raw, provider_raw)

                if provider_key not in provider_estimates:
                    logger.warning(f"No power estimates found for provider {provider_raw} (key: {provider_key})")
                    continue

                est = provider_estimates[provider_key]

                # Calculate total watts based on vcores
                min_watts = est["min_watts"] * vcores
                max_watts = est["max_watts"] * vcores

                profiles[instance_type] = {
                    "vcores": vcores,
                    "minWatts": min_watts,
                    "maxWatts": max_watts,
                    "provider": provider_raw,
                    "family": row["family"],
                }

        logger.info(f"Loaded {len(profiles)} instance profiles")
        return profiles

    except FileNotFoundError:
        logger.error(f"Instance profiles file not found: {profiles_file}")
        return {}
    except Exception as e:
        logger.error(f"Unexpected error loading instance profiles: {e}")
        return {}


# Load profiles at module import time
INSTANCE_PROFILES = load_instance_profiles()
