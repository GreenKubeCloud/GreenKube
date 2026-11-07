"""Carbon-intensity staleness and provenance in the calculator (roadmap A8, BUG-07)."""

from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.core.calculator import CarbonCalculator
from greenkube.data.electricity_maps_regions_grid_intensity_default import DEFAULT_GRID_INTENSITY_BY_ZONE
from greenkube.storage.base_repository import CarbonIntensityRepository, IntensityPoint

AT = datetime(2026, 10, 1, 12, 30, tzinfo=timezone.utc)
HOUR = AT.replace(minute=0)
UNKNOWN_ZONE = "XX-NOWHERE"


class PointRepository(CarbonIntensityRepository):
    def __init__(self, point: Optional[IntensityPoint]):
        self.point = point
        self.calls = 0

    async def get_for_zone_at_time(self, zone: str, timestamp: str) -> float | None:  # pragma: no cover - unused
        raise AssertionError("the calculator must use the provenance-aware lookup")

    async def get_intensity_point(self, zone: str, timestamp: str) -> Optional[IntensityPoint]:
        self.calls += 1
        return self.point

    async def save_history(self, history_data: list, zone: str) -> int:  # pragma: no cover - unused
        return 0


async def _calculate(point: Optional[IntensityPoint], zone: str = "FR"):
    calculator = CarbonCalculator(PointRepository(point), pue=1.0)
    return await calculator.calculate_emissions(joules=3.6e6, zone=zone, timestamp=AT)


@pytest.mark.parametrize(
    ("is_estimated", "expected"),
    [(False, "consolidated"), (True, "realtime"), (None, "realtime")],
)
async def test_fresh_points_are_realtime_or_consolidated(is_estimated, expected):
    result = await _calculate(
        IntensityPoint(value=42.0, observed_at=HOUR - timedelta(hours=1), is_estimated=is_estimated)
    )

    assert result is not None
    assert result.grid_intensity == 42.0
    assert result.co2e_grams == pytest.approx(42.0)
    assert result.intensity_source == expected


async def test_stale_point_falls_back_to_the_zone_default():
    result = await _calculate(IntensityPoint(value=42.0, observed_at=HOUR - timedelta(hours=7), is_estimated=False))

    assert result is not None
    assert result.grid_intensity == float(DEFAULT_GRID_INTENSITY_BY_ZONE["FR"])
    assert result.intensity_source == "zone_default"


async def test_stale_point_is_kept_and_flagged_when_no_zone_default_exists():
    result = await _calculate(
        IntensityPoint(value=42.0, observed_at=HOUR - timedelta(days=2), is_estimated=False), zone=UNKNOWN_ZONE
    )

    assert result is not None
    assert result.grid_intensity == 42.0
    assert result.intensity_source == "stale"


async def test_missing_values_use_zone_then_global_defaults():
    zone_result = await _calculate(None)
    global_result = await _calculate(None, zone=UNKNOWN_ZONE)

    assert zone_result is not None and zone_result.intensity_source == "zone_default"
    assert global_result is not None and global_result.intensity_source == "global_default"
    assert global_result.grid_intensity == pytest.approx(500.0)


async def test_cache_keeps_provenance_per_zone_and_hour():
    repository = PointRepository(IntensityPoint(value=42.0, observed_at=HOUR, is_estimated=False))
    calculator = CarbonCalculator(repository, pue=1.0)

    first = await calculator.calculate_emissions(joules=1.0, zone="FR", timestamp=AT)
    second = await calculator.calculate_emissions(joules=1.0, zone="FR", timestamp=AT + timedelta(minutes=10))

    assert repository.calls == 1
    assert first is not None and second is not None
    assert first.intensity_source == second.intensity_source == "consolidated"


async def test_prefetched_values_without_provenance_are_realtime():
    calculator = CarbonCalculator(PointRepository(None), pue=1.0)
    await calculator.prefetch_intensity("FR", AT.isoformat(), 33.0)

    result = await calculator.calculate_emissions(joules=1.0, zone="FR", timestamp=AT)

    assert result is not None
    assert result.grid_intensity == 33.0
    assert result.intensity_source == "realtime"


async def test_duck_typed_repositories_keep_working():
    repository = MagicMock()
    repository.get_for_zone_at_time = AsyncMock(return_value=77.0)
    calculator = CarbonCalculator(repository, pue=1.0)

    result = await calculator.calculate_emissions(joules=1.0, zone="FR", timestamp=AT)

    assert result is not None
    assert result.grid_intensity == 77.0
    assert result.intensity_source == "realtime"


async def test_without_a_token_rows_use_the_zone_default_and_history_stays_empty(monkeypatch):
    """A8 acceptance: no token → zone_default provenance and an empty intensity history."""
    from greenkube.collectors.electricity_maps_collector import ElectricityMapsCollector
    from greenkube.core.db import DatabaseManager
    from greenkube.storage.sqlite.repository import SQLiteCarbonIntensityRepository

    monkeypatch.setattr("greenkube.collectors.electricity_maps_collector.config.ELECTRICITY_MAPS_TOKEN", None)
    manager = DatabaseManager()
    await manager.setup_sqlite(db_path=":memory:")
    try:
        repository = SQLiteCarbonIntensityRepository(manager)
        collector = ElectricityMapsCollector()
        try:
            history = await collector.collect(zone="FR", target_datetime=AT)
        finally:
            await collector.close()
        assert await repository.save_history(history, "FR") == 0

        result = await CarbonCalculator(repository, pue=1.0).calculate_emissions(joules=1.0, zone="FR", timestamp=AT)

        async with manager.connection_scope() as conn:
            async with conn.execute("SELECT COUNT(*) FROM carbon_intensity_history") as cursor:
                assert (await cursor.fetchone())[0] == 0
    finally:
        await manager.close()
    assert result is not None
    assert result.intensity_source == "zone_default"
