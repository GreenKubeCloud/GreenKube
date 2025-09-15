# src/greenkube/utils/mapping_translator.py

from ..data.region_mapping import CLOUD_REGION_TO_ELECTRICITY_MAPS_ZONE

def get_emaps_zone_from_cloud_zone(cloud_zone: str) -> str:
    """
    Traduit une zone de cloud (ex: 'europe-west9-a') en code de zone
    Electricity Maps (ex: 'FR') en utilisant la table de correspondance.
    """
    # Gère les formats comme 'europe-west9-a' -> 'europe-west9'
    # et 'francecentral' -> 'francecentral'
    parts = cloud_zone.split('-')
    if len(parts) > 2 and parts[-1].isalpha() and len(parts[-1]) == 1:
        cloud_region = "-".join(parts[:-1])
    else:
        cloud_region = cloud_zone

    emaps_zone = CLOUD_REGION_TO_ELECTRICITY_MAPS_ZONE.get(cloud_region)

    if emaps_zone:
        print(f"  -> Mapped cloud zone '{cloud_zone}' to Electricity Maps zone '{emaps_zone}'")
        return emaps_zone
    else:
        print(f"  -> Warning: No mapping found for cloud region '{cloud_region}'. Defaulting to 'unknown'.")
        return "unknown"