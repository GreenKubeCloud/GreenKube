# src/greenkube/data/region_mapping.py

"""
Table de correspondance entre les préfixes des régions des fournisseurs de cloud
et les codes de zone utilisés par Electricity Maps.

Sources :
- GCP: https://cloud.google.com/about/locations
- AWS: https://aws.amazon.com/about-aws/global-infrastructure/
- Azure: https://azure.microsoft.com/en-us/explore/global-infrastructure/regions/
"""

# La clé est le préfixe de la région, la valeur est le code de zone d'Electricity Maps
CLOUD_REGION_TO_ELECTRICITY_MAPS_ZONE = {
    # --- Google Cloud Platform ---
    "europe-west9": "FR",      # Paris, France
    "europe-west1": "BE",      # Belgique
    "europe-north1": "FI",     # Finlande
    "europe-west2": "GB",      # Londres, UK
    "europe-west3": "DE",      # Francfort, Allemagne
    "us-central1": "US-MIDW-MISO", # Iowa, USA
    "us-east1": "US-SE-SOCO",      # Caroline du Sud, USA
    "asia-southeast1": "SG",   # Singapour

    # --- Amazon Web Services ---
    "eu-west-3": "FR",         # Paris, France
    "eu-central-1": "DE",      # Francfort, Allemagne
    "eu-west-2": "GB",         # Londres, UK
    "us-east-1": "US-NE-ISNE", # Virginie du Nord, USA
    "us-west-2": "US-NW-PACW", # Oregon, USA

    # --- Microsoft Azure ---
    "francecentral": "FR",     # Paris, France
    "westeurope": "NL",        # Pays-Bas
    "uksouth": "GB",           # Londres, UK
    "eastus": "US-NE-ISNE",    # Virginie, USA
}