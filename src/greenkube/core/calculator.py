# src/greenkube/core/calculator.py

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from greenkube.utils.date_utils import ensure_utc, to_iso_z

from ..data.electricity_maps_regions_grid_intensity_default import LEVEL_ZONE, resolve_default_intensity
from ..models.provenance import IntensitySource
from ..storage.base_repository import CarbonIntensityRepository, IntensityPoint, is_default_fallback_point
from .config import Config, get_config

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
    intensity_source: Optional[str] = None
    """Provenance of ``grid_intensity`` (greenkube.models.provenance.IntensitySource)."""


@dataclass(frozen=True)
class _CachedIntensity:
    """A resolved intensity and its provenance, cached per (zone, normalized time)."""

    value: Optional[float]
    source: Optional[str]


class CarbonCalculator:
    """Calculates CO2e emissions based on energy consumption and grid carbon intensity.

    Intensity resolution order for a ``(zone, normalized time)`` (A8, BUG-07):

    1. a stored value no older than ``INTENSITY_MAX_AGE`` →
       ``realtime`` (provisional) or ``consolidated`` (final);
    2. otherwise the bundled annual value of the zone (``zone_default``) or of its
       country (``country_default``, for a sub-zone such as ``US-CAL-CISO``);
    3. otherwise a stale stored value, if any → ``stale``;
    4. otherwise ``DEFAULT_INTENSITY`` → ``global_default``.

    Defaults are computed here, at read time; they are never written to the
    intensity history.
    """

    def __init__(
        self, repository: CarbonIntensityRepository, pue: Optional[float] = None, config: Config | None = None
    ):
        """Initialize with a repository and optional PUE.

        A small in-memory cache is used to avoid repeated repository/API calls
        for the same (zone, timestamp) during a single run.

        Args:
            repository: The carbon intensity data source.
            pue: Explicit PUE override. Falls back to ``config.DEFAULT_PUE``.
            config: Optional Config instance for dependency injection.
        """
        # Store repository
        self.repository = repository
        self._config = config if config is not None else get_config()

        # Resolve PUE at construction time so changes to config (or env-selected
        # profiles) are picked up when the calculator is instantiated. If the
        # caller explicitly provides a pue value, use it; otherwise fall back
        # to the current value of config.DEFAULT_PUE.
        self.pue = pue if pue is not None else self._config.DEFAULT_PUE

        # Simple per-run cache: key = (zone, timestamp) -> _CachedIntensity
        self._intensity_cache = {}
        self._lock = asyncio.Lock()

    async def clear_cache(self):
        """Clears the internal intensity cache."""
        async with self._lock:
            self._intensity_cache.clear()

    def _normalize(self, timestamp) -> datetime:
        dt = _to_datetime(timestamp)
        gran = getattr(self._config, "NORMALIZATION_GRANULARITY", "hour")
        if gran == "hour":
            return dt.replace(minute=0, second=0, microsecond=0)
        if gran == "day":
            return dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return dt

    def _max_age(self) -> timedelta:
        max_age = getattr(self._config, "intensity_max_age", None)
        return max_age if isinstance(max_age, timedelta) else timedelta(hours=6)

    async def prefetch_intensity(
        self, zone: str, timestamp: str, intensity: float, source: Optional[str] = None
    ) -> None:
        """Pre-populate the cache with a known intensity value.

        The timestamp is normalized using the same logic as ``calculate_emissions``
        so that a subsequent calculation for the same (zone, normalized-time) will
        hit the cache instead of querying the repository/API again.

        A value passed with its ``source`` is cached as-is. A bare value carries
        no provenance and was usually looked up at the raw sample time, so the
        point at the normalized time — the one the intensity recompute applies —
        is resolved instead (once per zone and normalized time). The bare value
        is kept, as ``realtime``, only when no fresh point exists there: it came
        from the age-bounded ``get_for_zone_at_time`` lookup.
        """
        normalized_dt = self._normalize(timestamp)
        normalized = _iso_z(normalized_dt)
        cache_key = (zone, normalized)

        async with self._lock:
            if cache_key in self._intensity_cache:
                return
            if source is None and isinstance(self.repository, CarbonIntensityRepository):
                try:
                    resolved = await self._lookup_intensity(zone, normalized, normalized_dt)
                except Exception as exc:
                    logger.debug("Intensity lookup for zone '%s' at %s failed: %s", zone, normalized, exc)
                    resolved = _CachedIntensity(value=None, source=None)
                if resolved.value is not None and resolved.source != IntensitySource.STALE.value:
                    self._intensity_cache[cache_key] = resolved
                    return
            self._intensity_cache[cache_key] = _CachedIntensity(
                value=intensity, source=source or IntensitySource.REALTIME.value
            )

    async def _lookup_intensity(self, zone: str, normalized: str, normalized_dt: datetime) -> _CachedIntensity:
        """Resolve a stored intensity and classify its freshness."""
        repository = self.repository
        if isinstance(repository, CarbonIntensityRepository):
            point = await repository.get_intensity_point(zone, normalized)
            if isinstance(point, IntensityPoint) and is_default_fallback_point(point):
                # A zone default persisted by an older version is not a measurement (BUG-07).
                return _CachedIntensity(value=None, source=None)
            if isinstance(point, IntensityPoint):
                age = point.age(normalized_dt)
                if age is not None and age > self._max_age():
                    return _CachedIntensity(value=point.value, source=IntensitySource.STALE.value)
                source = IntensitySource.CONSOLIDATED if point.is_estimated is False else IntensitySource.REALTIME
                return _CachedIntensity(value=point.value, source=source.value)
            if point is None:
                return _CachedIntensity(value=None, source=None)
        value = await repository.get_for_zone_at_time(zone, normalized)
        if value is None:
            return _CachedIntensity(value=None, source=None)
        return _CachedIntensity(value=value, source=IntensitySource.REALTIME.value)

    def _apply_defaults(self, zone: str, entry: _CachedIntensity, normalized_dt: datetime) -> _CachedIntensity:
        """Replace missing or stale values by the zone or global default (computed, never stored)."""
        if entry.value is not None and entry.source != IntensitySource.STALE.value:
            return entry
        # Annual bundled value of the zone, else of its country (NRG-04).
        bundled = resolve_default_intensity(zone)
        if bundled is not None:
            logger.info(
                "Carbon intensity %s for zone '%s' at %s; using the %s annual default of '%s': %s gCO2e/kWh",
                "stale" if entry.value is not None else "missing",
                zone,
                normalized_dt.isoformat(),
                bundled.year,
                bundled.zone,
                bundled.value,
            )
            source = IntensitySource.ZONE_DEFAULT if bundled.level == LEVEL_ZONE else IntensitySource.COUNTRY_DEFAULT
            return _CachedIntensity(value=float(bundled.value), source=source.value)
        if entry.value is not None:
            logger.warning(
                "Only a stale carbon intensity exists for zone '%s' at %s and no zone default is known; using it",
                zone,
                normalized_dt.isoformat(),
            )
            return entry
        logger.warning(
            "Carbon intensity missing for zone '%s' at %s and no zone default available; "
            "using global fallback %s gCO2e/kWh",
            zone,
            normalized_dt.isoformat(),
            self._config.DEFAULT_INTENSITY,
        )
        return _CachedIntensity(value=self._config.DEFAULT_INTENSITY, source=IntensitySource.GLOBAL_DEFAULT.value)

    async def calculate_emissions(
        self, joules: float, zone: str, timestamp: str | datetime, pue: Optional[float] = None
    ) -> Optional[CarbonCalculationResult]:
        """Calculate CO2e grams and return it with the grid intensity used and its provenance.

        If intensity is missing or stale in the repository, a default is used
        (see the class docstring for the resolution order).

        Args:
            joules: Energy consumed in Joules.
            zone: Electricity Maps zone identifier.
            timestamp: Timestamp for the carbon intensity lookup.
            pue: Power Usage Effectiveness to apply. If None, uses the instance default.
        """
        # Normalize timestamp to hour to increase cache hit rate across similar timestamps
        normalized_dt = self._normalize(timestamp)
        normalized = _iso_z(normalized_dt)
        cache_key = (zone, normalized)

        # Use a single async lock acquisition to check cache and fetch if needed.
        # This prevents concurrent coroutines from all fetching the same key.
        async with self._lock:
            cached = self._intensity_cache.get(cache_key)
            if isinstance(cached, _CachedIntensity):
                entry = cached
            elif cached is not None:
                entry = _CachedIntensity(value=cached, source=IntensitySource.REALTIME.value)
            else:
                entry = self._apply_defaults(
                    zone, await self._lookup_intensity(zone, normalized, normalized_dt), normalized_dt
                )
                self._intensity_cache[cache_key] = entry

        grid_intensity_value = float(entry.value) if entry.value is not None else self._config.DEFAULT_INTENSITY

        if joules == 0.0:
            return CarbonCalculationResult(
                co2e_grams=0.0,
                grid_intensity=grid_intensity_value,
                grid_intensity_timestamp=normalized_dt,
                intensity_source=entry.source,
            )

        kwh = joules / self._config.JOULES_PER_KWH
        effective_pue = pue if pue is not None else self.pue
        kwh_adjusted_for_pue = kwh * effective_pue
        co2e_grams = kwh_adjusted_for_pue * grid_intensity_value

        return CarbonCalculationResult(
            co2e_grams=co2e_grams,
            grid_intensity=grid_intensity_value,
            grid_intensity_timestamp=normalized_dt,
            intensity_source=entry.source,
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
