# src/greenkube/core/calculator.py

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from greenkube.utils.date_utils import ensure_utc, to_iso_z

from ..storage.base_repository import CarbonIntensityRepository
from .config import config


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
        self._lock = threading.Lock()

    def clear_cache(self):
        """Clears the internal intensity cache."""
        with self._lock:
            self._intensity_cache.clear()

    def calculate_emissions(self, joules: float, zone: str, timestamp: str) -> Optional[CarbonCalculationResult]:
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
        grid_intensity_data = None

        with self._lock:
            if cache_key in self._intensity_cache:
                grid_intensity_data = self._intensity_cache[cache_key]

        # If not in cache, fetch it (outside lock to avoid holding it during I/O)
        if grid_intensity_data is None and (
            cache_key not in self._intensity_cache if hasattr(self, "_intensity_cache") else True
        ):
            # Double-check locking pattern or just fetch.
            # Fetching twice is better than blocking.
            # But wait, if we fetch, we want to store.
            # Let's fetch then lock to store.

            # Optimization: check again under lock if another thread fetched it?
            # For simplicity, just fetch.

            # Actually, the original code did:
            # if cache_key in self._intensity_cache: ... else: fetch; store

            # To match that safely:
            fetched = False
            with self._lock:
                if cache_key in self._intensity_cache:
                    grid_intensity_data = self._intensity_cache[cache_key]
                    fetched = True

            if not fetched:
                grid_intensity_data = self.repository.get_for_zone_at_time(zone, normalized)
                with self._lock:
                    self._intensity_cache[cache_key] = grid_intensity_data

        grid_intensity_value = grid_intensity_data

        # A value of -1 in the cache indicates we've already warned for this key.
        # We need to check this under lock too
        has_warned = False
        with self._lock:
            has_warned = self._intensity_cache.get(cache_key) == -1

        if grid_intensity_value is None:
            if not has_warned:
                logger = logging.getLogger(__name__)
                logger.warning(
                    "Carbon intensity missing for zone '%s' at %s; using default %s gCO2e/kWh",
                    zone,
                    normalized_dt.isoformat(),
                    config.DEFAULT_INTENSITY,
                )
                # Mark this cache key as warned to prevent re-logging.
                with self._lock:
                    self._intensity_cache[cache_key] = -1
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
