# src/greenkube/data/instance_profiles.py

"""
Database of power consumption profiles for cloud instances.

This data is essential for the "Basic Estimation Engine".
It is based on open-source data from the Cloud Carbon Footprint project,
which provides estimates for minimum (idle) and maximum (100% CPU) power
draw for various instance types.

Instance profiles are now loaded from a YAML configuration file to allow
users to add custom hardware without modifying source code.

Source: https://github.com/cloud-carbon-footprint/cloud-carbon-footprint/blob/trunk/packages/gcp/src/lib/GCPFootprintEstimationConstants.ts
Source: https://github.com/cloud-carbon-footprint/cloud-carbon-footprint/blob/trunk/packages/aws/src/lib/AWSFootprintEstimationConstants.ts
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict

import yaml

logger = logging.getLogger(__name__)


def load_instance_profiles() -> Dict[str, Any]:
    """
    Load instance profiles from YAML configuration file.

    Checks for custom profiles file via INSTANCE_PROFILES_PATH environment
    variable, or uses the default shipped with GreenKube. This allows users
    to provide custom ConfigMaps in Kubernetes deployments.

    Returns:
        Dictionary mapping instance type names to profile data (vcores, minWatts, maxWatts)
    """
    # Check for custom profiles file (e.g., from Kubernetes ConfigMap)
    custom_path = os.environ.get("INSTANCE_PROFILES_PATH")
    if custom_path and Path(custom_path).exists():
        profiles_file = Path(custom_path)
        logger.info(f"Loading instance profiles from custom path: {profiles_file}")
    else:
        # Use default file shipped with GreenKube
        profiles_file = Path(__file__).parent / "instance_profiles.yaml"
        logger.debug(f"Loading instance profiles from default path: {profiles_file}")

    try:
        with open(profiles_file, "r") as f:
            data = yaml.safe_load(f)
            profiles = data.get("profiles", {})
            logger.info(f"Loaded {len(profiles)} instance profiles")
            return profiles
    except FileNotFoundError:
        logger.error(f"Instance profiles file not found: {profiles_file}")
        return {}
    except yaml.YAMLError as e:
        logger.error(f"Failed to parse instance profiles YAML: {e}")
        return {}
    except Exception as e:
        logger.error(f"Unexpected error loading instance profiles: {e}")
        return {}


# Load profiles at module import time
INSTANCE_PROFILES = load_instance_profiles()
