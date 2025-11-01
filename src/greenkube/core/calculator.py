# src/greenkube/core/calculator.py

from dataclasses import dataclass
from ..storage.base_repository import CarbonIntensityRepository
from .config import config
from datetime import datetime, timezone


def _to_datetime(ts) -> datetime:
    """Convert a timestamp (str or datetime) to a timezone-aware datetime in UTC."""
    if isinstance(ts, datetime):
        dt = ts
    elif isinstance(ts, str):
        s = ts
        # Accept ISO strings ending with Z
        if s.endswith('Z'):
            s = s.replace('Z', '+00:00')
        try:
            dt = datetime.fromisoformat(s)
        except Exception:
            # Fallback: use now to avoid crashing; caller will likely use default intensity.
            dt = datetime.now(timezone.utc)
    else:
        dt = datetime.now(timezone.utc)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_z(dt: datetime) -> str:
    """Return an ISO-format UTC string ending with 'Z' for compatibility with tests."""
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


@dataclass
class CarbonCalculationResult:
    co2e_grams: float
    grid_intensity: float


class CarbonCalculator:
    """Calculates CO2e emissions based on energy consumption and grid carbon intensity."""

    def __init__(self, repository: CarbonIntensityRepository, pue: float = config.DEFAULT_PUE):
        """Initialize with a repository and optional PUE.

        A small in-memory cache is used to avoid repeated repository/API calls
        for the same (zone, timestamp) during a single run.
        """
        self.repository = repository
        self.pue = pue

        # Simple per-run cache: key = (zone, timestamp) -> intensity value (float or None)
        self._intensity_cache = {}

    def calculate_emissions(self, joules: float, zone: str, timestamp: str) -> CarbonCalculationResult:
        """Calculate CO2e grams and return it with the grid intensity used.

        If intensity is missing in the repository, the configured default is used.
        """
        # Normalize timestamp to hour to increase cache hit rate across similar timestamps
        dt = _to_datetime(timestamp)
        gran = getattr(config, 'NORMALIZATION_GRANULARITY', 'hour')
        if gran == 'hour':
            normalized_dt = dt.replace(minute=0, second=0, microsecond=0)
        elif gran == 'day':
            normalized_dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        else:  # 'none'
            normalized_dt = dt
        normalized = _iso_z(normalized_dt)
        cache_key = (zone, normalized)
        if cache_key in self._intensity_cache:
            grid_intensity_value = self._intensity_cache[cache_key]
        else:
            grid_intensity_value = self.repository.get_for_zone_at_time(zone, normalized)
            # Store even None values to avoid repeated DB/API lookups for missing data
            self._intensity_cache[cache_key] = grid_intensity_value

        if grid_intensity_value is None:
            print(f"WARN: Carbon intensity data not found for zone '{zone}' at {timestamp}. Using default value: {config.DEFAULT_INTENSITY} gCO2e/kWh.")
            grid_intensity_value = config.DEFAULT_INTENSITY

        if joules == 0.0:
            return CarbonCalculationResult(co2e_grams=0.0, grid_intensity=grid_intensity_value)

        kwh = joules / config.JOULES_PER_KWH
        kwh_adjusted_for_pue = kwh * self.pue
        co2e_grams = kwh_adjusted_for_pue * grid_intensity_value

        return CarbonCalculationResult(co2e_grams=co2e_grams, grid_intensity=grid_intensity_value)


