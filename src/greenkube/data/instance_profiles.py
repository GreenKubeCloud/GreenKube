# src/greenkube/data/instance_profiles.py

"""
Database of power consumption profiles for cloud instances.

This data is essential for the "Basic Estimation Engine".
It is based on open-source data from the Cloud Carbon Footprint project,
which provides estimates for minimum (idle) and maximum (100% CPU) power
draw for various instance types.

Source: https://github.com/cloud-carbon-footprint/cloud-carbon-footprint/blob/trunk/packages/gcp/src/lib/GCPFootprintEstimationConstants.ts
Source: https://github.com/cloud-carbon-footprint/cloud-carbon-footprint/blob/trunk/packages/aws/src/lib/AWSFootprintEstimationConstants.ts
"""

# Each key is the 'instance_type' (e.g., 'm5.large')
# - vcores: Number of vCPUs for this instance type.
# - minWatts: Power consumption at idle.
# - maxWatts: Power consumption at 100% CPU utilization.
INSTANCE_PROFILES = {
    # --- AWS ---
    "m5.large": {
        "vcores": 2,
        "minWatts": 3.23,
        "maxWatts": 36.30
    },
    "m5.xlarge": {
        "vcores": 4,
        "minWatts": 5.82,
        "maxWatts": 66.27
    },
    "t3.medium": {
        "vcores": 2,
        "minWatts": 2.03,
        "maxWatts": 23.41
    },
    "t3.large": {
        "vcores": 2,
        "minWatts": 2.03,
        "maxWatts": 23.41 # Often shared for burstable types
    },
    "t3.xlarge": {
        "vcores": 4,
        "minWatts": 3.42,
        "maxWatts": 40.48
    },

    # --- GCP ---
    "n1-standard-1": {
        "vcores": 1,
        "minWatts": 1.42,
        "maxWatts": 13.56
    },
    "n1-standard-2": {
        "vcores": 2,
        "minWatts": 2.22,
        "maxWatts": 22.31
    },
    "e2-standard-2": {
        "vcores": 2,
        "minWatts": 1.34,
        "maxWatts": 11.23
    },
    "e2-standard-4": {
        "vcores": 4,
        "minWatts": 2.36,
        "maxWatts": 19.94
    },

    # --- Azure (Examples) ---
    "Standard_D2s_v3": {
        "vcores": 2,
        "minWatts": 2.22,
        "maxWatts": 22.31 # Based on similar n1-standard-2 GCP profile
    },
    "Standard_D4s_v3": {
        "vcores": 4,
        "minWatts": 3.82,
        "maxWatts": 39.81 # Based on similar n1-standard-4 GCP profile
    },
}

