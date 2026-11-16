"""The bundled zone intensity table is complete, well-formed and annual (NRG-04)."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.collectors.wattnet_collector import EM_TO_WATTNET_ZONE
from greenkube.core.calculator import CarbonCalculator
from greenkube.core.config import Config
from greenkube.data import electricity_maps_regions_grid_intensity_default as defaults

DATA = Path(defaults.__file__).parent


def _mapped_zones() -> set[str]:
    with open(DATA / "cloud_region_electricity_maps_mapping.csv", newline="", encoding="utf-8") as handle:
        return {row["electricity_maps_zone"] for row in csv.DictReader(handle) if row["electricity_maps_zone"]}


def test_every_zone_a_cloud_region_maps_to_has_a_default():
    missing = sorted(zone for zone in _mapped_zones() if defaults.resolve_default_intensity(zone) is None)
    assert missing == []


@pytest.mark.parametrize("zone", ["ES", "SE", "NO", "DK", "IT", "NL", "CH", "AT", "GB", "DE", "FR", "IE", "US"])
def test_common_country_zones_have_their_own_annual_value(zone):
    resolved = defaults.resolve_default_intensity(zone)
    assert resolved is not None and resolved.level == defaults.LEVEL_ZONE
    assert resolved.year == defaults.DEFAULT_INTENSITY_YEAR


def test_every_wattnet_zone_has_a_default():
    missing = sorted(zone for zone in EM_TO_WATTNET_ZONE if defaults.resolve_default_intensity(zone) is None)
    assert missing == []


def test_table_is_well_formed_and_values_are_plausible():
    rows = defaults.load_rows()
    assert len({row.zone for row in rows}) == len(rows)
    valued = [(row, row.intensity_g_per_kwh) for row in rows if row.intensity_g_per_kwh is not None]
    assert len(valued) >= 150
    for row, value in valued:
        assert 5 <= value <= 1500  # Turkmenistan 2024: 1306
        assert row.year == defaults.DEFAULT_INTENSITY_YEAR
        assert row.source and row.emission_factor_type == "lifecycle"


def test_a_column_shifted_row_fails_the_load(tmp_path):
    path = tmp_path / "defaults.csv"
    path.write_text(",".join(defaults.CSV_COLUMNS) + "\nES,Spain,Spain,lifecycle,146.3,2024,Ember\n")
    with pytest.raises(ValueError):
        defaults.load_rows(path)


def test_gb_is_the_annual_mean_not_a_windy_day():
    """GB 2024 per Ember (life-cycle) is 216 gCO2e/kWh; the former single-day value was 67 (acceptance e)."""
    gb = defaults.resolve_default_intensity("GB")
    assert gb is not None and gb.value == pytest.approx(216.2, rel=0.15)


def test_sub_zone_falls_back_to_its_country():
    resolved = defaults.resolve_default_intensity("US-XYZ-UNKNOWN")
    assert resolved is not None
    assert (resolved.level, resolved.value) == (defaults.LEVEL_COUNTRY, defaults.DEFAULT_GRID_INTENSITY_BY_ZONE["US"])


def _calculator() -> CarbonCalculator:
    repository = MagicMock()
    repository.get_for_zone_at_time = AsyncMock(return_value=None)
    return CarbonCalculator(repository=repository, config=Config())


async def test_calculate_emissions_uses_the_nl_annual_default_not_the_global_one():
    result = await _calculator().calculate_emissions(3.6e6, "NL", datetime(2026, 11, 16, 12, tzinfo=timezone.utc))
    assert result is not None
    assert result.grid_intensity == pytest.approx(defaults.DEFAULT_GRID_INTENSITY_BY_ZONE["NL"])
    assert result.intensity_source == "zone_default"


async def test_calculate_emissions_of_an_unknown_sub_zone_uses_the_country_default():
    result = await _calculator().calculate_emissions(
        3.6e6, "US-XYZ-UNKNOWN", datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
    )
    assert result is not None
    assert result.grid_intensity == pytest.approx(defaults.DEFAULT_GRID_INTENSITY_BY_ZONE["US"])
    assert result.intensity_source == "country_default"
