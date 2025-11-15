# src/greenkube/data/region_mapping.py

"""
Mapping table between cloud provider region prefixes and the zone codes
used by Electricity Maps.

Sources:
- GCP: https://cloud.google.com/about/locations
- AWS: https://aws.amazon.com/about-aws/global-infrastructure/
- Azure: https://azure.microsoft.com/en-us/explore/global-infrastructure/regions/
"""

# The key is the region prefix, the value is the Electricity Maps zone code
CLOUD_REGION_TO_ELECTRICITY_MAPS_ZONE = {
    # --- Google Cloud Platform ---
    "europe-west9": "FR",  # Paris, France
    "europe-west1": "BE",  # Belgium
    "europe-north1": "FI",  # Finland
    "europe-west2": "GB",  # London, UK
    "europe-west3": "DE",  # Frankfurt, Germany
    "us-central1": "US-MIDW-MISO",  # Iowa, USA
    "us-east1": "US-SE-SOCO",  # South Carolina, USA
    "asia-southeast1": "SG",  # Singapore
    # --- Amazon Web Services ---
    "eu-west-3": "FR",  # Paris, France
    "eu-central-1": "DE",  # Frankfurt, Germany
    "eu-west-2": "GB",  # London, UK
    "us-east-1": "US-NE-ISNE",  # North Virginia, USA
    "us-west-2": "US-NW-PACW",  # Oregon, USA
    # --- Microsoft Azure ---
    "francecentral": "FR",  # Paris, France
    "westeurope": "NL",  # Netherlands
    "uksouth": "GB",  # London, UK
    "eastus": "US-NE-ISNE",  # Virginia, USA
}
