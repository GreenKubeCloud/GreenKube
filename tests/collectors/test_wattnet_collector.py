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
        ("IT-CALA", "IT_CALABRIA"),
        ("SE", "SE3"),
        ("SE-SE3", "SE3"),
        ("NO", "NO2"),
        ("NO-NO1", "NO1"),
        ("DK", "DK1"),
        ("US-CAL-CISO", None),
        ("unknown", None),
    ],
)
def test_to_wattnet_zone(em_zone, expected):
    assert to_wattnet_zone(em_zone) == expected


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
