# src/greenkube/core/calculator.py

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from greenkube.utils.date_utils import ensure_utc, to_iso_z

from ..storage.base_repository import CarbonIntensityRepository
from .config import config

logger = logging.getLogger(__name__)


def _to_datetime(ts) -> datetime:
    """Convert a timestamp (str or datetime) to a timezone-aware datetime in UTC."""
    return ensure_utc(ts)


def _iso_z(dt: datetime) -> str:
    """Return an ISO-format UTC string ending with 'Z' for compatibility with tests."""
    return to_iso_z(dt)


@dataclass
class CarbonCalculationResult:
    co2e_grams: float
    grid_intensity: float
    grid_intensity_timestamp: Optional[datetime] = None


class CarbonCalculator:
    """Calculates CO2e emissions based on energy consumption and grid carbon intensity."""

    def __init__(self, repository: CarbonIntensityRepository, pue: Optional[float] = None):
        """Initialize with a repository and optional PUE.

        A small in-memory cache is used to avoid repeated repository/API calls
        for the same (zone, timestamp) during a single run.
        """
        # Store repository
        self.repository = repository

        # Resolve PUE at construction time so changes to config (or env-selected
        # profiles) are picked up when the calculator is instantiated. If the
        # caller explicitly provides a pue value, use it; otherwise fall back
        # to the current value of config.DEFAULT_PUE.
        self.pue = pue if pue is not None else config.DEFAULT_PUE

        # Simple per-run cache: key = (zone, timestamp) -> intensity value
        # (float or None)
        self._intensity_cache = {}
        self._lock = asyncio.Lock()

    async def clear_cache(self):
        """Clears the internal intensity cache."""
        async with self._lock:
            self._intensity_cache.clear()

    async def calculate_emissions(self, joules: float, zone: str, timestamp: str) -> Optional[CarbonCalculationResult]:
        """Calculate CO2e grams and return it with the grid intensity used.

        If intensity is missing in the repository, the configured default is used.
        """
        # Normalize timestamp to hour to increase cache hit rate across similar timestamps
        dt = _to_datetime(timestamp)
        gran = getattr(config, "NORMALIZATION_GRANULARITY", "hour")
        if gran == "hour":
            normalized_dt = dt.replace(minute=0, second=0, microsecond=0)
        elif gran == "day":
            normalized_dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        else:  # 'none'
            normalized_dt = dt
        normalized = _iso_z(normalized_dt)
        cache_key = (zone, normalized)

        # Use a single async lock acquisition to check cache and fetch if needed.
        # This prevents concurrent coroutines from all fetching the same key.
        async with self._lock:
            if cache_key in self._intensity_cache:
                grid_intensity_data = self._intensity_cache[cache_key]
            else:
                grid_intensity_data = await self.repository.get_for_zone_at_time(zone, normalized)
                self._intensity_cache[cache_key] = grid_intensity_data

        grid_intensity_value = grid_intensity_data

        # Use default if intensity data is missing
        if grid_intensity_value is None:
            async with self._lock:
                if cache_key not in self._intensity_cache or self._intensity_cache[cache_key] is None:
                    self._intensity_cache[cache_key] = None
                    logger.warning(
                        "Carbon intensity missing for zone '%s' at %s; using default %s gCO2e/kWh",
                        zone,
                        normalized_dt.isoformat(),
                        config.DEFAULT_INTENSITY,
                    )
            grid_intensity_value = config.DEFAULT_INTENSITY

        if joules == 0.0:
            return CarbonCalculationResult(
                co2e_grams=0.0,
                grid_intensity=grid_intensity_value,
                grid_intensity_timestamp=normalized_dt,
            )

        kwh = joules / config.JOULES_PER_KWH
        kwh_adjusted_for_pue = kwh * self.pue
        co2e_grams = kwh_adjusted_for_pue * grid_intensity_value

        return CarbonCalculationResult(
            co2e_grams=co2e_grams,
            grid_intensity=grid_intensity_value,
            grid_intensity_timestamp=normalized_dt,
        )

    def calculate_embodied_emissions(
        self, gwp_manufacture_kg: float, lifespan_hours: float, duration_seconds: float, share: float = 1.0
    ) -> float:
        """
        Calculates the allocated embodied emissions (Scope 3) in grams.

        Formula: (gwp_kg * 1000 / lifespan_hours) * (duration_seconds / 3600) * share
        """
        if lifespan_hours <= 0:
            return 0.0

        gwp_grams = gwp_manufacture_kg * 1000.0
        rate_grams_per_hour = gwp_grams / lifespan_hours
        duration_hours = duration_seconds / 3600.0

        return rate_grams_per_hour * duration_hours * share
