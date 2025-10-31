# src/greenkube/utils/mapping_translator.py

import logging
from ..data.region_mapping import CLOUD_REGION_TO_ELECTRICITY_MAPS_ZONE

logger = logging.getLogger(__name__)


def get_emaps_zone_from_cloud_zone(cloud_zone: str) -> str | None:
    """
    Translate a cloud zone (e.g. 'europe-west9-a') to an Electricity Maps
    zone code (e.g. 'FR') using the region mapping table.

    Returns None when no mapping exists.
    """
    parts = cloud_zone.split('-')
    if len(parts) > 2 and parts[-1].isalpha() and len(parts[-1]) == 1:
        cloud_region = "-".join(parts[:-1])
    else:
        cloud_region = cloud_zone

    emaps_zone = CLOUD_REGION_TO_ELECTRICITY_MAPS_ZONE.get(cloud_region)

    if emaps_zone:
        logger.info("Mapped cloud zone '%s' to Electricity Maps zone '%s'", cloud_zone, emaps_zone)
        return emaps_zone
    else:
        logger.debug("No Electricity Maps mapping for cloud region '%s'", cloud_region)
        return None