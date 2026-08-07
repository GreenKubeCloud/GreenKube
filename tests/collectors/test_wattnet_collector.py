# tests/collectors/test_wattnet_collector.py

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
import respx
from httpx import Response

from greenkube.collectors.wattnet_collector import (
    DEFAULT_API_BASE_URL,
    DEFAULT_TOKEN_SERVICE_URL,
    WattnetCollector,
    to_wattnet_zone,
)

MOCK_TOKEN_RESPONSE = {
    "access_token": "test-token-123",
    "expires_at": (datetime.now(timezone.utc) + timedelta(hours=23)).isoformat(),
}

MOCK_FOOTPRINTS_RESPONSE = [
    {
        "footprint_type": "carbon",
        "scope": "life-cycle",
        "zone": "FR",
        "unit": "gCO2/kWh",
        "coverage": "global",
        "series": [
            {
                "valid": True,
                "zone_status": "complete",
                "values": [
                    ["2026-08-07T10:00:00Z", 36.26],
                    ["2026-08-07T10:15:00Z", 37.5],
                ],
            },
            {
                "valid": False,
                "zone_status": "preview",
                "values": [["2026-08-07T10:00:00Z", 99.0]],
            },
        ],
    }
]


def _configure_collector(mock_config, email="user@example.com", password="pass123"):
    """Apply mocked config attributes to the collector config."""
    mock_config.WATTNET_EMAIL = email
    mock_config.WATTNET_PASSWORD = password
    mock_config.WATTNET_API_BASE_URL = DEFAULT_API_BASE_URL
    mock_config.WATTNET_TOKEN_SERVICE_URL = DEFAULT_TOKEN_SERVICE_URL


# --------------------------------------------------------------------------
# Zone translation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("em_zone", "expected"),
    [
        ("FR", "FR"),
        ("DE", "DE"),
        ("IT", "IT_NORTH"),
        ("IT-NO", "IT_NORTH"),
        ("IT-CNO", "IT_CNORTH"),
        ("IT-CSO", "IT_CSOUTH"),
        ("IT-SO", "IT_SOUTH"),
        ("IT-SAR", "IT_SARDINIA"),
        ("IT-SIC", "IT_SICILY"),
        ("SE", "SE3"),
        ("SE-SE1", "SE1"),
        ("SE-SE2", "SE2"),
        ("SE-SE3", "SE3"),
        ("SE-SE4", "SE4"),
        ("NO", "NO2"),
        ("NO-NO1", "NO1"),
        ("NO-NO2", "NO2"),
        ("NO-NO3", "NO3"),
        ("NO-NO4", "NO4"),
        ("NO-NO5", "NO5"),
        ("DK", "DK1"),
        ("DK-DK1", "DK1"),
        ("DK-DK2", "DK2"),
        ("NI", "NIE"),
        ("GB-NIR", "NIE"),
        ("US-CAL-CISO", None),
        ("unknown", None),
    ],
)
def test_to_wattnet_zone(em_zone, expected):
    assert to_wattnet_zone(em_zone) == expected


def test_all_eu_em_codes_from_default_map_are_translated():
    """Every EU zone code in the default intensity map must map to a real
    Wattnet zone or deliberately fall back to None (islands not covered)."""
    from greenkube.collectors.wattnet_collector import EM_TO_WATTNET_ZONE, WATTNET_ZONES
    from greenkube.data.electricity_maps_regions_grid_intensity_default import DEFAULT_GRID_INTENSITY_BY_ZONE

    wattnet_countries = {
        "AT",
        "BA",
        "BE",
        "BG",
        "CH",
        "CY",
        "CZ",
        "DE",
        "DK",
        "EE",
        "ES",
        "FI",
        "FR",
        "GB",
        "GE",
        "GR",
        "HR",
        "HU",
        "IE",
        "IT",
        "LT",
        "LU",
        "LV",
        "MD",
        "ME",
        "MK",
        "NI",
        "NL",
        "NO",
        "PL",
        "PT",
        "RO",
        "RS",
        "SE",
        "SI",
        "SK",
        "TR",
        "XK",
    }
    # Islands / exclaves with no Wattnet counterpart — expected to fall back.
    no_wattnet_equivalent = {
        "DK-BHM",  # Bornholm
        "ES-CE",  # Ceuta
        "ES-CN-FV",
        "ES-CN-GC",
        "ES-CN-HI",
        "ES-CN-IG",
        "ES-CN-LP",
        "ES-CN-LZ",
        "ES-CN-TE",  # Canary Islands
        "ES-IB-FO",
        "ES-IB-IZ",
        "ES-IB-MA",
        "ES-IB-ME",  # Balearic Islands
        "ES-ML",  # Melilla
        "FR-COR",  # Corsica
        "GB-ORK",  # Orkney
        "PT-MA",  # Madeira
    }

    for zone in DEFAULT_GRID_INTENSITY_BY_ZONE:
        if zone.split("-")[0] not in wattnet_countries:
            continue
        result = to_wattnet_zone(zone)
        if zone in no_wattnet_equivalent:
            assert result is None, f"{zone} should have no Wattnet equivalent, got {result}"
            continue
        assert result is not None, f"{zone} has no translation"
        assert result in WATTNET_ZONES, f"{zone} -> {result} is not a Wattnet zone"
        # Every explicit entry in the table must be a real Wattnet zone.
    for em_zone, wn_zone in EM_TO_WATTNET_ZONE.items():
        assert wn_zone in WATTNET_ZONES, f"{em_zone} maps to unknown Wattnet zone {wn_zone}"


def test_all_cloud_regions_map_automatically():
    """Every EU cloud region in the mapping CSV resolves to a valid Wattnet
    zone; every non-EU region gracefully falls back to None."""
    import csv
    from pathlib import Path

    from greenkube.collectors.wattnet_collector import WATTNET_ZONES

    wattnet_countries = {
        "AT",
        "BA",
        "BE",
        "BG",
        "CH",
        "CY",
        "CZ",
        "DE",
        "DK",
        "EE",
        "ES",
        "FI",
        "FR",
        "GB",
        "GE",
        "GR",
        "HR",
        "HU",
        "IE",
        "IT",
        "LT",
        "LU",
        "LV",
        "MD",
        "ME",
        "MK",
        "NI",
        "NL",
        "NO",
        "PL",
        "PT",
        "RO",
        "RS",
        "SE",
        "SI",
        "SK",
        "TR",
        "XK",
    }
    mapping_file = (
        Path(__file__).parents[2] / "src" / "greenkube" / "data" / ("cloud_region_electricity_maps_mapping.csv")
    )
    with open(mapping_file) as f:
        rows = list(csv.DictReader(f))

    assert rows, "mapping CSV is empty"
    for row in rows:
        em_zone = row["electricity_maps_zone"].strip()
        is_eu = em_zone.split("-")[0] in wattnet_countries
        result = to_wattnet_zone(em_zone)
        if is_eu:
            assert result in WATTNET_ZONES, (
                f"{row['cloud_provider']} {row['region_id']} -> EM {em_zone} "
                f"did not resolve to a Wattnet zone (got {result})"
            )
        else:
            assert result is None, f"non-EU zone {em_zone} unexpectedly mapped to {result}"


# --------------------------------------------------------------------------
# collect() — token + data flow
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
@patch("greenkube.collectors.wattnet_collector.config")
async def test_collect_success(mock_config):
    _configure_collector(mock_config)

    respx.post(f"{DEFAULT_TOKEN_SERVICE_URL}/get_token").mock(return_value=Response(200, json=MOCK_TOKEN_RESPONSE))
    route = respx.get(url__startswith=f"{DEFAULT_API_BASE_URL}/footprints").mock(
        return_value=Response(200, json=MOCK_FOOTPRINTS_RESPONSE)
    )

    collector = WattnetCollector()
    try:
        result = await collector.collect(zone="FR", target_datetime=None)
    finally:
        await collector.close()

    assert route.called
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer test-token-123"

    assert len(result) == 2
    # The valid/complete series wins over the preview series for the same ts.
    assert result[0] == {
        "carbonIntensity": 36.26,
        "datetime": "2026-08-07T10:00:00Z",
        "zone": "FR",
        "isEstimated": False,
        "estimationMethod": "wattnet_complete",
        "emissionFactorType": "wattnet_life-cycle_global",
    }
    assert result[1]["carbonIntensity"] == 37.5


@pytest.mark.asyncio
@respx.mock
@patch("greenkube.collectors.wattnet_collector.config")
async def test_collect_sends_start_end_window(mock_config):
    _configure_collector(mock_config)

    respx.post(f"{DEFAULT_TOKEN_SERVICE_URL}/get_token").mock(return_value=Response(200, json=MOCK_TOKEN_RESPONSE))
    route = respx.get(url__startswith=f"{DEFAULT_API_BASE_URL}/footprints").mock(
        return_value=Response(200, json=MOCK_FOOTPRINTS_RESPONSE)
    )

    target = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    collector = WattnetCollector()
    try:
        await collector.collect(zone="FR", target_datetime=target)
    finally:
        await collector.close()

    params = route.calls.last.request.url.params
    assert params["start"] == "2026-08-07T00:00:00Z"
    assert params["end"] == "2026-08-08T00:00:00Z"


@pytest.mark.asyncio
@respx.mock
@patch("greenkube.collectors.wattnet_collector.config")
async def test_collect_token_retry_on_401(mock_config):
    _configure_collector(mock_config)

    respx.post(f"{DEFAULT_TOKEN_SERVICE_URL}/get_token").mock(return_value=Response(200, json=MOCK_TOKEN_RESPONSE))
    footprints_route = respx.get(url__startswith=f"{DEFAULT_API_BASE_URL}/footprints")
    footprints_route.mock(
        side_effect=[
            Response(401, json={"detail": "unauthorized"}),
            Response(200, json=MOCK_FOOTPRINTS_RESPONSE),
        ]
    )

    collector = WattnetCollector()
    try:
        result = await collector.collect(zone="FR")
    finally:
        await collector.close()

    assert footprints_route.call_count == 2
    assert len(result) == 2


@pytest.mark.asyncio
@respx.mock
@patch("greenkube.collectors.wattnet_collector.config")
async def test_collect_zone_not_covered_uses_default(mock_config):
    _configure_collector(mock_config)

    collector = WattnetCollector()
    try:
        result = await collector.collect(zone="US-CAL-CISO")
    finally:
        await collector.close()

    assert len(result) == 1
    assert result[0]["zone"] == "US-CAL-CISO"
    assert result[0]["isEstimated"] is True
    assert result[0]["estimationMethod"] == "default_fallback"
    # Default for US-CAL-CISO in the fallback map
    assert result[0]["carbonIntensity"] is not None


@pytest.mark.asyncio
@respx.mock
@patch("greenkube.collectors.wattnet_collector.config")
async def test_collect_missing_credentials_uses_default(mock_config):
    _configure_collector(mock_config, email=None, password=None)

    collector = WattnetCollector()
    try:
        result = await collector.collect(zone="FR")
    finally:
        await collector.close()

    assert len(result) == 1
    assert result[0]["isEstimated"] is True
    assert result[0]["carbonIntensity"] == 26  # FR default


@pytest.mark.asyncio
@respx.mock
@patch("greenkube.collectors.wattnet_collector.config")
async def test_collect_api_error_uses_default(mock_config):
    _configure_collector(mock_config)

    respx.post(f"{DEFAULT_TOKEN_SERVICE_URL}/get_token").mock(return_value=Response(200, json=MOCK_TOKEN_RESPONSE))
    respx.get(url__startswith=f"{DEFAULT_API_BASE_URL}/footprints").mock(side_effect=httpx.HTTPError("API Error"))

    collector = WattnetCollector()
    try:
        result = await collector.collect(zone="FR")
    finally:
        await collector.close()

    assert len(result) == 1
    assert result[0]["zone"] == "FR"
    assert result[0]["isEstimated"] is True


@pytest.mark.asyncio
@respx.mock
@patch("greenkube.collectors.wattnet_collector.config")
async def test_collect_token_failure_uses_default(mock_config):
    _configure_collector(mock_config)

    respx.post(f"{DEFAULT_TOKEN_SERVICE_URL}/get_token").mock(
        return_value=Response(401, json={"detail": "bad credentials"})
    )

    collector = WattnetCollector()
    try:
        result = await collector.collect(zone="FR")
    finally:
        await collector.close()

    assert len(result) == 1
    assert result[0]["isEstimated"] is True


@pytest.mark.asyncio
@respx.mock
@patch("greenkube.collectors.wattnet_collector.config")
async def test_collect_empty_payload_uses_default(mock_config):
    _configure_collector(mock_config)

    respx.post(f"{DEFAULT_TOKEN_SERVICE_URL}/get_token").mock(return_value=Response(200, json=MOCK_TOKEN_RESPONSE))
    respx.get(url__startswith=f"{DEFAULT_API_BASE_URL}/footprints").mock(return_value=Response(200, json=[]))

    collector = WattnetCollector()
    try:
        result = await collector.collect(zone="FR")
    finally:
        await collector.close()

    assert len(result) == 1
    assert result[0]["isEstimated"] is True


@pytest.mark.asyncio
@respx.mock
@patch("greenkube.collectors.wattnet_collector.config")
async def test_token_cached_and_reused(mock_config):
    _configure_collector(mock_config)

    token_route = respx.post(f"{DEFAULT_TOKEN_SERVICE_URL}/get_token").mock(
        return_value=Response(200, json=MOCK_TOKEN_RESPONSE)
    )
    respx.get(url__startswith=f"{DEFAULT_API_BASE_URL}/footprints").mock(
        return_value=Response(200, json=MOCK_FOOTPRINTS_RESPONSE)
    )

    collector = WattnetCollector()
    try:
        await collector.collect(zone="FR")
        await collector.collect(zone="DE")
    finally:
        await collector.close()

    assert token_route.call_count == 1
