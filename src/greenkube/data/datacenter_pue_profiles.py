# src/greenkube/data/datacenter_pue_profiles.py

"""
Source of Power Usage Effectiveness (PUE) values for various data centers.
GCP : https://datacenters.google/efficiency/
AWS : https://sustainability.aboutamazon.com/products-services/aws-cloud#carbon-free-energy
Azure : https://cdn-dynmedia-1.microsoft.com/is/content/microsoftcorp/microsoft/msc/documents/presentations/CSR/2025-Microsoft-Environmental-Sustainability-Report.pdf#page=01
OVHcloud : https://corporate.ovhcloud.com/en/sustainability/environment/
Scaleway : https://www.scaleway.com/fr/leadership-environnemental/
"""

# We currently use default PUE values per cloud provider.
# In the future, we may expand this to specific regions or data centers.
DATACENTER_PUE_PROFILES = {
    # --- AWS ---
    "default_aws": 1.15,
    # --- GCP ---
    "default_gcp": 1.09,
    # --- Azure ---
    "default_azure": 1.18,
    # --- OVHcloud ---
    "default_ovh": 1.26,
    # --- Scaleway ---
    "default_scaleway": 1.37,
}

# Private datacenters publish no fleet-wide PUE: ``cloudProvider`` set to one of
# these names maps explicitly to the operator's ``config.defaults.pue``
# (``DEFAULT_PUE``, 1.3 unless set), which should be the measured PUE of the site.
ON_PREMISES_PROVIDERS = frozenset({"on-prem", "on-premise", "on-premises", "onprem", "on_prem"})
