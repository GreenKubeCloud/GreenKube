# src/greenkube/collectors/wattnet_collector.py
"""
Collector for grid carbon intensity data from the Wattnet API.

Wattnet (https://wattnet.eu) is an EU-funded open-source service tracking the
environmental footprint of electricity across Europe. It provides real-time,
historical and forecasted footprint data (carbon and water) at 15-minute
resolution for 52 European zones.

Authentication is token based: a Bearer token (valid for 1 day) is obtained
from the token-request service using an email/password pair registered on the
Wattnet platform.

This collector only uses the carbon footprint data. Water footprint data is
not consumed yet — see docs/wattnet.md for the roadmap.

API reference: https://api.wattnet.eu/v1/docs
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from ..core.config import config
from ..data.electricity_maps_regions_grid_intensity_default import DEFAULT_GRID_INTENSITY_BY_ZONE
from ..utils.http_client import get_async_http_client
from .base_electricity_provider import BaseElectricityProvider

logger = logging.getLogger(__name__)

DEFAULT_API_BASE_URL = "https://api.wattnet.eu/v1"
DEFAULT_TOKEN_SERVICE_URL = "https://api.wattnet.eu/token-request"

# Zones supported by Wattnet (as exposed by GET /zones).
WATTNET_ZONES = frozenset(
    {
        "AT",
        "BA",
        "BE",
        "BG",
        "CH",
        "CY",
        "CZ",
        "DE",
        "DK1",
        "DK2",
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
        "IT_CALABRIA",
        "IT_CNORTH",
        "IT_CSOUTH",
        "IT_NORTH",
        "IT_SARDINIA",
        "IT_SICILY",
        "IT_SOUTH",
        "LT",
        "LU",
        "LV",
        "MD",
        "ME",
        "MK",
        "NIE",
        "NL",
        "NO1",
        "NO2",
        "NO3",
        "NO4",
        "NO5",
        "PL",
        "PT",
        "RO",
        "RS",
        "SE1",
        "SE2",
        "SE3",
        "SE4",
        "SI",
        "SK",
        "TR",
        "XK",
    }
)

# Translation of Electricity Maps zone codes to Wattnet zone codes.
# Most EU country codes are identical in both naming schemes and are passed
# through unchanged. The entries below cover every country-level and
# sub-national code used by Electricity Maps in Europe.
#
# Sub-national defaults for country-level codes (no Wattnet aggregate exists):
#   - IT  -> IT_NORTH (Milan area, largest demand zone)
#   - SE  -> SE3     (Stockholm area, ~70 % of Swedish demand)
#   - NO  -> NO2     (Oslo/Southern Norway, largest demand zone)
#   - DK  -> DK1     (mainland Denmark / Jutland)
EM_TO_WATTNET_ZONE = {
    # Italy (EM sub-zones -> ENTSO-E bidding zones)
    "IT": "IT_NORTH",
    "IT-NO": "IT_NORTH",
    "IT-CNO": "IT_CNORTH",
    "IT-CSO": "IT_CSOUTH",
    "IT-SO": "IT_SOUTH",
    "IT-SAR": "IT_SARDINIA",
    "IT-SIC": "IT_SICILY",
    # Sweden
    "SE": "SE3",
    "SE-SE1": "SE1",
    "SE-SE2": "SE2",
    "SE-SE3": "SE3",
    "SE-SE4": "SE4",
    # Norway
    "NO": "NO2",
    "NO-NO1": "NO1",
    "NO-NO2": "NO2",
    "NO-NO3": "NO3",
    "NO-NO4": "NO4",
    "NO-NO5": "NO5",
    # Denmark
    "DK": "DK1",
    "DK-DK1": "DK1",
    "DK-DK2": "DK2",
    # Northern Ireland
    "NI": "NIE",
    "GB-NIR": "NIE",
}

# Priority of the series groups returned by the footprints endpoint when the
# same timestamp appears in several series: final data wins over provisional,
# and complete zones win over preview/missing ones.
_SERIES_RANK = {
    (True, "complete"): 0,
    (True, "preview"): 1,
    (True, "missing"): 2,
    (False, "complete"): 3,
    (False, "preview"): 4,
    (False, "missing"): 5,
}

# How much history to fetch around a requested timestamp. A wide window
# compensates for gaps in the upstream data so that the closest available
# point can be stored and later served by the repository lookups.
COLLECT_WINDOW = timedelta(hours=12)
DEFAULT_COLLECT_RANGE = timedelta(hours=24)

# Tokens are valid for 1 day; refresh slightly early to avoid races.
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)


def to_wattnet_zone(zone: str) -> Optional[str]:
    """
    Translate a GreenKube (Electricity Maps style) zone code to a Wattnet zone
    code.

    Resolution order:
    1. Zones already using the Wattnet naming (identical country codes).
    2. Explicit translation table (covers all sub-national European codes).
    3. Safety net: sub-national codes that differ only by separator, e.g.
       ``SE-SE3`` -> ``SE3`` (never used for codes with no Wattnet counterpart
       such as ``US-CAL-CISO``, since the last segment would not match).

    Returns ``None`` when no Wattnet zone corresponds to the given code
    (e.g. non-European zones such as ``US-CAL-CISO``).
    """
    if zone in WATTNET_ZONES:
        return zone
    if zone in EM_TO_WATTNET_ZONE:
        return EM_TO_WATTNET_ZONE[zone]
    # Safety net for future sub-national codes, e.g. "NO-NO1" -> "NO1".
    if "-" in zone:
        candidate = zone.rsplit("-", 1)[-1]
        if candidate in WATTNET_ZONES:
            return candidate
    return None


class WattnetCollector(BaseElectricityProvider):
    """
    A collector to retrieve carbon intensity data from the Wattnet API.

    Authentication credentials (``WATTNET_EMAIL`` / ``WATTNET_PASSWORD``) are
    read from the configuration. When they are missing or the API is
    unreachable, the collector falls back to the static default grid
    intensity map, mirroring the behaviour of the Electricity Maps collector.
    """

    def __init__(self):
        self.email = config.WATTNET_EMAIL
        self.password = config.WATTNET_PASSWORD
        self.api_base_url = getattr(config, "WATTNET_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/")
        self.token_service_url = getattr(config, "WATTNET_TOKEN_SERVICE_URL", DEFAULT_TOKEN_SERVICE_URL).rstrip("/")

        if not self.email or not self.password:
            logger.warning("WATTNET_EMAIL or WATTNET_PASSWORD is not set. Using default values.")

        # Reusable HTTP client (lazily initialized)
        self._client: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._token_expires_at: datetime | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the reusable HTTP client, creating it lazily if needed."""
        if self._client is None or self._client.is_closed:
            self._client = get_async_http_client()
        return self._client

    async def close(self):
        """Close the reusable HTTP client to release connection pool resources."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        self._token = None
        self._token_expires_at = None

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def _is_token_valid(self) -> bool:
        if not self._token or not self._token_expires_at:
            return False
        return self._token_expires_at - TOKEN_REFRESH_MARGIN > datetime.now(timezone.utc)

    async def _fetch_token(self) -> Optional[str]:
        """Obtain a fresh Bearer token from the Wattnet token service."""
        if not self.email or not self.password:
            return None
        client = await self._get_client()
        try:
            response = await client.post(
                f"{self.token_service_url}/get_token",
                json={"email": self.email, "password": self.password},
            )
            response.raise_for_status()
            data = response.json()
            token = data.get("access_token")
            if not token:
                logger.error("Wattnet token service response did not contain an access_token.")
                return None
            expires_at = data.get("expires_at")
            if expires_at:
                try:
                    self._token_expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                except ValueError:
                    self._token_expires_at = None
            self._token = token
            return token
        except httpx.HTTPError as e:
            logger.error("Failed to obtain a token from Wattnet token service: %s", e)
            return None
        except Exception as e:
            logger.error("Unexpected error obtaining a Wattnet token: %s", e)
            return None

    async def _get_token(self, force_refresh: bool = False) -> Optional[str]:
        """Return a cached valid token, fetching a new one when needed."""
        if not force_refresh and self._is_token_valid():
            return self._token
        return await self._fetch_token()

    # ------------------------------------------------------------------
    # Data collection
    # ------------------------------------------------------------------

    async def collect(self, zone: str, target_datetime: datetime | None = None) -> list:  # type: ignore[override]
        """
        Retrieves carbon intensity history for a zone from Wattnet.

        The zone is translated from the GreenKube (Electricity Maps) naming to
        the Wattnet naming. If no Wattnet zone exists (e.g. non-EU zone), or
        when authentication/API calls fail, the static default grid intensity
        is returned instead.
        """
        wattnet_zone = to_wattnet_zone(zone)
        if wattnet_zone is None:
            logger.info("Zone '%s' is not covered by Wattnet. Using default grid intensity.", zone)
            return self._default_records(zone, target_datetime)

        if not self.email or not self.password:
            logger.warning("Wattnet credentials missing. Using default grid intensity for zone '%s'.", zone)
            return self._default_records(zone, target_datetime)

        token = await self._get_token()
        if not token:
            return self._default_records(zone, target_datetime)

        start_dt, end_dt = self._compute_window(target_datetime)
        url = f"{self.api_base_url}/footprints"
        params = {
            "zone": wattnet_zone,
            "footprint_type": "carbon",
            "scope": "life-cycle",
            "use_global": True,
            "start": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        headers = {"Authorization": f"Bearer {token}"}
        logger.info("Fetching Wattnet carbon footprint for zone: %s (wattnet zone: %s)...", zone, wattnet_zone)

        client = await self._get_client()
        try:
            response = await client.get(url, params=params, headers=headers)
            if response.status_code in (401, 403):
                logger.info("Wattnet token rejected (status %s). Refreshing token and retrying.", response.status_code)
                token = await self._get_token(force_refresh=True)
                if not token:
                    return self._default_records(zone, target_datetime)
                headers = {"Authorization": f"Bearer {token}"}
                response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as e:
            logger.error("Error fetching data from Wattnet API: %s", e)
            return self._default_records(zone, target_datetime)
        except Exception as e:
            logger.error("Unexpected error fetching data from Wattnet API: %s", e)
            return self._default_records(zone, target_datetime)

        records = self._flatten_footprints(data, zone)
        if not records:
            logger.info("Wattnet returned no carbon footprint data for zone: %s", zone)
            return self._default_records(zone, target_datetime)
        return records

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_window(target_datetime: datetime | None) -> tuple[datetime, datetime]:
        """Compute the (start, end) window to request from the API."""
        now = datetime.now(timezone.utc)
        if target_datetime is None:
            end_dt = now
            start_dt = now - DEFAULT_COLLECT_RANGE
        else:
            if target_datetime.tzinfo is None:
                target_datetime = target_datetime.replace(tzinfo=timezone.utc)
            start_dt = target_datetime - COLLECT_WINDOW
            end_dt = target_datetime + COLLECT_WINDOW
        return start_dt, end_dt

    def _flatten_footprints(self, data: list, zone: str) -> list:
        """
        Flatten the Wattnet footprints payload into Electricity-Maps-shaped
        records, preferring final/complete data when timestamps overlap.
        """
        records: dict[str, dict] = {}
        for footprint in data:
            if not isinstance(footprint, dict) or footprint.get("footprint_type") != "carbon":
                continue
            scope = footprint.get("scope", "life-cycle")
            coverage = footprint.get("coverage", "global")
            for series in footprint.get("series", []):
                valid = bool(series.get("valid"))
                zone_status = series.get("zone_status", "missing")
                rank = _SERIES_RANK.get((valid, zone_status), 5)
                is_estimated = not (valid and zone_status == "complete")
                estimation_method = f"wattnet_{zone_status}"
                for ts, value in series.get("values", []):
                    ts_key = str(ts)
                    existing = records.get(ts_key)
                    if existing is not None and existing["_rank"] <= rank:
                        continue
                    records[ts_key] = {
                        "carbonIntensity": float(value),
                        "datetime": ts_key,
                        "zone": zone,
                        "isEstimated": is_estimated,
                        "estimationMethod": estimation_method,
                        "emissionFactorType": f"wattnet_{scope}_{coverage}",
                        "_rank": rank,
                    }
        # Strip the internal ranking key and sort chronologically.
        return [
            {k: v for k, v in record.items() if k != "_rank"}
            for record in sorted(records.values(), key=lambda r: r["datetime"])
        ]

    @staticmethod
    def _default_records(zone: str, target_datetime: datetime | None) -> list:
        """Build the static fallback record, mirroring the EM collector."""
        logger.info("Using default grid intensity for zone: %s", zone)
        default_intensity = DEFAULT_GRID_INTENSITY_BY_ZONE.get(zone)
        if default_intensity is not None:
            if target_datetime:
                if target_datetime.tzinfo is None:
                    target_datetime = target_datetime.replace(tzinfo=timezone.utc)
                dt_iso = target_datetime.isoformat()
            else:
                dt_iso = datetime.now(timezone.utc).isoformat()
            return [
                {
                    "carbonIntensity": default_intensity,
                    "datetime": dt_iso,
                    "zone": zone,
                    "isEstimated": True,
                    "estimationMethod": "default_fallback",
                }
            ]
        logger.warning("No default grid intensity found for zone: %s", zone)
        return []
