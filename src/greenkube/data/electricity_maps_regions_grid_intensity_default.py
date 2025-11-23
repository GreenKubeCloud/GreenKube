# src/greenkube/data/electricity_maps_regions_grid_intensity_default.py
import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

"""
This file contains the default grid intensity data for electricity maps.
The default values correspond to the values registered the 23/11/2025.
For more accurate values, create a EM token and run the greenkube collect command.
"""


def _load_defaults():
    defaults = {}
    try:
        # Resolve path relative to this file
        current_dir = Path(__file__).parent
        csv_path = current_dir / "electricity_maps_default.csv"

        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                zone_code = row.get("Zone Code")
                intensity = row.get("Default Grid Intensity")
                if zone_code and intensity:
                    try:
                        defaults[zone_code] = int(intensity)
                    except ValueError:
                        pass
    except Exception as e:
        logger.error(f"Failed to load default electricity maps data from {csv_path}: {e}")

    return defaults


DEFAULT_GRID_INTENSITY_BY_ZONE = _load_defaults()
