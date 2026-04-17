# src/greenkube/utils/mapping_translator.py

import logging
import re

from ..data.region_mapping import CLOUD_REGION_TO_ELECTRICITY_MAPS_ZONE, PROVIDER_REGION_TO_EM_ZONE

logger = logging.getLogger(__name__)

# Pre-compute the set of valid Electricity Maps zone codes from the mapping
# values so we can recognise when the input is already an EM zone (e.g. on
# bare-metal, minikube, or on-prem clusters where operators label nodes with
# Electricity Maps zone codes directly like "FR", "DE", "US-CAL-CISO").
_KNOWN_EM_ZONES: set[str] = set(CLOUD_REGION_TO_ELECTRICITY_MAPS_ZONE.values()) | {
    v for v in PROVIDER_REGION_TO_EM_ZONE.values()
}


def get_emaps_zone_from_cloud_zone(cloud_zone: str, provider: str = None) -> str | None:
    """
    Translate a cloud zone (e.g. 'europe-west9-a') to an Electricity Maps
    zone code (e.g. 'FR') using the region mapping table.

    If the input is already a valid Electricity Maps zone code (e.g. 'FR'),
    it is returned as-is.  This supports bare-metal / on-prem clusters
    where the topology label is set directly to an EM zone.

    Args:
        cloud_zone: The zone string from the cloud provider (e.g. 'us-east-1a')
                    or an Electricity Maps zone code (e.g. 'FR').
        provider: Optional cloud provider name (e.g. 'aws', 'gcp', 'ovh')

    Returns:
        Electricity Maps zone code or None if no mapping exists.
    """
    # "nova" is the OpenStack default availability-zone name used by OVH MKS
    # when the cluster does not expose real AZ information.  It carries no
    # geographic meaning and must never be treated as a valid region.
    if cloud_zone == "nova":
        return None

    # If the input is already a valid Electricity Maps zone code, return it
    # directly.  This covers bare-metal / minikube / on-prem nodes labelled
    # with EM zones (e.g. topology.kubernetes.io/zone=FR).
    if cloud_zone in _KNOWN_EM_ZONES:
        logger.debug(
            "Cloud zone '%s' is already a valid Electricity Maps zone code.",
            cloud_zone,
        )
        return cloud_zone

    # Try to find an exact match first (e.g. if the input is already a region)
    if provider and (provider, cloud_zone) in PROVIDER_REGION_TO_EM_ZONE:
        return PROVIDER_REGION_TO_EM_ZONE[(provider, cloud_zone)]
    if cloud_zone in CLOUD_REGION_TO_ELECTRICITY_MAPS_ZONE:
        return CLOUD_REGION_TO_ELECTRICITY_MAPS_ZONE[cloud_zone]

    # Heuristics to extract region from zone
    candidates = []

    # 1. GCP style: europe-west9-a -> europe-west9
    parts = cloud_zone.split("-")
    if len(parts) > 2 and parts[-1].isalpha() and len(parts[-1]) == 1:
        candidates.append("-".join(parts[:-1]))

    # 2. AWS style: us-east-1a -> us-east-1
    # Check if the last character is a letter and the second to last is a digit
    if len(cloud_zone) > 2 and cloud_zone[-1].isalpha() and cloud_zone[-2].isdigit():
        candidates.append(cloud_zone[:-1])

    # 3. Scaleway/Generic style: fr-par-1 -> fr-par
    if len(parts) > 1 and parts[-1].isdigit():
        candidates.append("-".join(parts[:-1]))

    # 4. OVH numbered data-centre codes: GRA11 -> GRA, RBX8 -> RBX, BHS5 -> BHS …
    # OVH trigrams are 2-4 uppercase letters followed by one or more digits.
    ovh_dc_match = re.fullmatch(r"([A-Z]{2,4})(\d+)", cloud_zone)
    if ovh_dc_match:
        candidates.append(ovh_dc_match.group(1))

    emaps_zone = None

    # Try candidates against provider mapping
    if provider:
        for region_candidate in candidates:
            emaps_zone = PROVIDER_REGION_TO_EM_ZONE.get((provider, region_candidate))
            if emaps_zone:
                logger.debug(
                    "Mapped cloud zone '%s' (provider: %s) to Electricity Maps zone '%s' via candidate '%s'",
                    cloud_zone,
                    provider,
                    emaps_zone,
                    region_candidate,
                )
                return emaps_zone

    # Fallback to generic mapping with candidates
    for region_candidate in candidates:
        emaps_zone = CLOUD_REGION_TO_ELECTRICITY_MAPS_ZONE.get(region_candidate)
        if emaps_zone:
            logger.info(
                "Mapped cloud zone '%s' to Electricity Maps zone '%s' (fallback via candidate '%s')",
                cloud_zone,
                emaps_zone,
                region_candidate,
            )
            return emaps_zone

    logger.debug("No Electricity Maps mapping for cloud zone '%s' (provider: %s)", cloud_zone, provider)
    return None
