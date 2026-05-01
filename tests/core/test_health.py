# tests/core/test_health.py
"""
Tests for the health check service.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from greenkube.core.health import (
    check_boavizta,
    check_electricity_maps,
    check_kubernetes,
    check_opencost,
    check_prometheus,
    invalidate_health_cache,
    run_health_checks,
)
from greenkube.models.health import ServiceHealth, ServiceStatus


class TestCheckPrometheus:
    """Tests for the Prometheus health check."""

    @pytest.mark.asyncio
    async def test_unconfigured_no_discovery(self, monkeypatch):
        """When PROMETHEUS_URL is empty and discovery fails, status is UNCONFIGURED."""
        from greenkube.core.config import config

        monkeypatch.setenv("PROMETHEUS_URL", "")
        config.reload()

        with patch(
            "greenkube.collectors.discovery.prometheus.PrometheusDiscovery.discover",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await check_prometheus(config)

        assert result.status == ServiceStatus.UNCONFIGURED
        assert result.name == "prometheus"
        assert "not configured" in result.message.lower()

    @pytest.mark.asyncio
    async def test_healthy_configured(self, monkeypatch):
        """When Prometheus responds with status=success, health is HEALTHY."""
        from greenkube.core.config import config

        monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus:9090")
        config.reload()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "success"}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("greenkube.core.health.get_async_http_client", return_value=mock_client):
            result = await check_prometheus(config)

        assert result.status == ServiceStatus.HEALTHY
        assert result.configured is True
        assert result.url == "http://prometheus:9090"
        assert result.latency_ms is not None

    @pytest.mark.asyncio
    async def test_unreachable(self, monkeypatch):
        """When Prometheus connection fails, status is UNREACHABLE."""
        from greenkube.core.config import config

        monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus:9090")
        config.reload()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("greenkube.core.health.get_async_http_client", return_value=mock_client):
            result = await check_prometheus(config)

        assert result.status == ServiceStatus.UNREACHABLE
        assert "cannot reach" in result.message.lower()

    @pytest.mark.asyncio
    async def test_degraded_non_200(self, monkeypatch):
        """When Prometheus returns non-200, status is DEGRADED."""
        from greenkube.core.config import config

        monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus:9090")
        config.reload()

        mock_response = MagicMock()
        mock_response.status_code = 503

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("greenkube.core.health.get_async_http_client", return_value=mock_client):
            result = await check_prometheus(config)

        assert result.status == ServiceStatus.DEGRADED

    @pytest.mark.asyncio
    async def test_discovered_url_is_probed(self, monkeypatch):
        from greenkube.core.config import config

        monkeypatch.setenv("PROMETHEUS_URL", "")
        config.reload()
        with (
            patch(
                "greenkube.collectors.discovery.prometheus.PrometheusDiscovery.discover",
                new_callable=AsyncMock,
                return_value="http://discovered-prometheus:9090",
            ),
            patch("greenkube.core.health._probe_prometheus", new_callable=AsyncMock) as probe,
        ):
            probe.return_value = ServiceHealth(
                name="prometheus",
                status=ServiceStatus.HEALTHY,
                message="OK",
                last_check=datetime.now(timezone.utc),
                discovered=True,
            )
            result = await check_prometheus(config)

        assert result.discovered is True
        probe.assert_awaited_once_with("http://discovered-prometheus:9090", configured=False, discovered=True)

    @pytest.mark.asyncio
    async def test_discovery_exception_returns_unconfigured(self, monkeypatch):
        from greenkube.core.config import config

        monkeypatch.setenv("PROMETHEUS_URL", "")
        config.reload()
        with patch(
            "greenkube.collectors.discovery.prometheus.PrometheusDiscovery.discover",
            new_callable=AsyncMock,
            side_effect=RuntimeError("discovery failed"),
        ):
            result = await check_prometheus(config)

        assert result.status == ServiceStatus.UNCONFIGURED

    @pytest.mark.asyncio
    async def test_degraded_when_prometheus_json_is_invalid(self, monkeypatch):
        from greenkube.core.config import config

        monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus:9090")
        config.reload()
        mock_response = MagicMock(status_code=200)
        mock_response.json.side_effect = ValueError("bad json")
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("greenkube.core.health.get_async_http_client", return_value=mock_client):
            result = await check_prometheus(config)

        assert result.status == ServiceStatus.DEGRADED


class TestCheckOpenCost:
    """Tests for the OpenCost health check."""

    @pytest.mark.asyncio
    async def test_unconfigured_no_discovery(self, monkeypatch):
        """When OPENCOST_API_URL is empty and discovery fails, status is UNCONFIGURED."""
        from greenkube.core.config import config

        monkeypatch.setenv("OPENCOST_API_URL", "")
        config.reload()

        with patch(
            "greenkube.collectors.discovery.opencost.OpenCostDiscovery.discover",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await check_opencost(config)

        assert result.status == ServiceStatus.UNCONFIGURED
        assert result.name == "opencost"

    @pytest.mark.asyncio
    async def test_healthy(self, monkeypatch):
        """When OpenCost /healthz returns 200, status is HEALTHY."""
        from greenkube.core.config import config

        monkeypatch.setenv("OPENCOST_API_URL", "http://opencost:9003")
        config.reload()

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("greenkube.core.health.get_async_http_client", return_value=mock_client):
            result = await check_opencost(config)

        assert result.status == ServiceStatus.HEALTHY
        assert result.configured is True

    @pytest.mark.asyncio
    async def test_discovered_url_is_probed(self, monkeypatch):
        from greenkube.core.config import config

        monkeypatch.setenv("OPENCOST_API_URL", "")
        config.reload()
        with (
            patch(
                "greenkube.collectors.discovery.opencost.OpenCostDiscovery.discover",
                new_callable=AsyncMock,
                return_value="http://discovered-opencost:9003",
            ),
            patch("greenkube.core.health._probe_opencost", new_callable=AsyncMock) as probe,
        ):
            probe.return_value = ServiceHealth(
                name="opencost",
                status=ServiceStatus.HEALTHY,
                message="OK",
                last_check=datetime.now(timezone.utc),
                discovered=True,
            )
            result = await check_opencost(config)

        assert result.discovered is True
        probe.assert_awaited_once_with("http://discovered-opencost:9003", configured=False, discovered=True)

    @pytest.mark.asyncio
    async def test_discovery_exception_returns_unconfigured(self, monkeypatch):
        from greenkube.core.config import config

        monkeypatch.setenv("OPENCOST_API_URL", "")
        config.reload()
        with patch(
            "greenkube.collectors.discovery.opencost.OpenCostDiscovery.discover",
            new_callable=AsyncMock,
            side_effect=RuntimeError("discovery failed"),
        ):
            result = await check_opencost(config)

        assert result.status == ServiceStatus.UNCONFIGURED

    @pytest.mark.asyncio
    async def test_degraded_and_unreachable(self, monkeypatch):
        from greenkube.core.config import config

        monkeypatch.setenv("OPENCOST_API_URL", "http://opencost:9003")
        config.reload()
        mock_response = MagicMock(status_code=503)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("greenkube.core.health.get_async_http_client", return_value=mock_client):
            result = await check_opencost(config)
        assert result.status == ServiceStatus.DEGRADED

        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        with patch("greenkube.core.health.get_async_http_client", return_value=mock_client):
            result = await check_opencost(config)
        assert result.status == ServiceStatus.UNREACHABLE


class TestCheckElectricityMaps:
    """Tests for the Electricity Maps health check."""

    @pytest.mark.asyncio
    async def test_unconfigured_no_token(self, monkeypatch):
        """When token is not set, status is UNCONFIGURED."""
        from greenkube.core.config import config

        monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "")
        config.reload()

        result = await check_electricity_maps(config)
        assert result.status == ServiceStatus.UNCONFIGURED
        assert "token" in result.message.lower()

    @pytest.mark.asyncio
    async def test_healthy_with_token(self, monkeypatch):
        """When API returns 200, status is HEALTHY."""
        from greenkube.core.config import config

        monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "valid-token")
        config.reload()

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("greenkube.core.health.get_async_http_client", return_value=mock_client):
            result = await check_electricity_maps(config)

        assert result.status == ServiceStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_probe_uses_fallback_zone_when_default_is_unknown(self, monkeypatch):
        """When DEFAULT_ZONE is 'unknown', the probe uses 'FR' as fallback zone."""
        from greenkube.core.config import config

        monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "valid-token")
        monkeypatch.setenv("DEFAULT_ZONE", "unknown")
        config.reload()

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("greenkube.core.health.get_async_http_client", return_value=mock_client):
            result = await check_electricity_maps(config)

        assert result.status == ServiceStatus.HEALTHY
        # Verify the probe URL used the fallback zone, not 'unknown'
        call_args = mock_client.get.call_args
        probe_url = call_args[0][0]
        assert "zone=FR" in probe_url
        assert "zone=unknown" not in probe_url

    @pytest.mark.asyncio
    async def test_invalid_token_401(self, monkeypatch):
        """When API returns 401, status is DEGRADED (bad token)."""
        from greenkube.core.config import config

        monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "bad-token")
        config.reload()

        mock_response = MagicMock()
        mock_response.status_code = 401

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("greenkube.core.health.get_async_http_client", return_value=mock_client):
            result = await check_electricity_maps(config)

        assert result.status == ServiceStatus.DEGRADED
        assert "401" in result.message

    @pytest.mark.asyncio
    async def test_non_401_errors_and_unreachable(self, monkeypatch):
        from greenkube.core.config import config

        monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "token")
        config.reload()
        mock_response = MagicMock(status_code=500)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("greenkube.core.health.get_async_http_client", return_value=mock_client):
            result = await check_electricity_maps(config)
        assert result.status == ServiceStatus.DEGRADED

        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        with patch("greenkube.core.health.get_async_http_client", return_value=mock_client):
            result = await check_electricity_maps(config)
        assert result.status == ServiceStatus.UNREACHABLE


class TestCheckBoavizta:
    """Tests for the Boavizta health check."""

    @pytest.mark.asyncio
    async def test_unconfigured(self, monkeypatch):
        """When Boavizta URL is empty, status is UNCONFIGURED."""
        from greenkube.core.config import config

        monkeypatch.setenv("BOAVIZTA_API_URL", "")
        config.reload()

        result = await check_boavizta(config)
        assert result.status == ServiceStatus.UNCONFIGURED

    @pytest.mark.asyncio
    async def test_healthy(self, monkeypatch):
        """When Boavizta returns 200, status is HEALTHY."""
        from greenkube.core.config import config

        monkeypatch.setenv("BOAVIZTA_API_URL", "https://api.boavizta.org")
        config.reload()

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("greenkube.core.health.get_async_http_client", return_value=mock_client):
            result = await check_boavizta(config)

        assert result.status == ServiceStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_degraded_and_unreachable(self, monkeypatch):
        from greenkube.core.config import config

        monkeypatch.setenv("BOAVIZTA_API_URL", "https://api.boavizta.org")
        config.reload()
        mock_response = MagicMock(status_code=500)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("greenkube.core.health.get_async_http_client", return_value=mock_client):
            result = await check_boavizta(config)
        assert result.status == ServiceStatus.DEGRADED

        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        with patch("greenkube.core.health.get_async_http_client", return_value=mock_client):
            result = await check_boavizta(config)
        assert result.status == ServiceStatus.UNREACHABLE


class TestCheckKubernetes:
    """Tests for the Kubernetes health check."""

    @pytest.mark.asyncio
    async def test_unreachable_no_client(self):
        """When K8s client cannot be initialized, status is UNREACHABLE."""
        with patch("greenkube.core.k8s_client.get_core_v1_api", new_callable=AsyncMock, return_value=None):
            result = await check_kubernetes()

        assert result.status == ServiceStatus.UNREACHABLE
        assert "could not be initialized" in result.message.lower()

    @pytest.mark.asyncio
    async def test_healthy_version_probe(self):
        version = MagicMock(git_version="1.30.0")
        api_client_cm = MagicMock()
        api_client_cm.__aenter__ = AsyncMock(return_value="api-client")
        api_client_cm.__aexit__ = AsyncMock(return_value=False)
        version_api = MagicMock()
        version_api.get_code = AsyncMock(return_value=version)

        with (
            patch("greenkube.core.k8s_client.get_core_v1_api", new_callable=AsyncMock, return_value=MagicMock()),
            patch("kubernetes_asyncio.client.ApiClient", return_value=api_client_cm),
            patch("kubernetes_asyncio.client.VersionApi", return_value=version_api),
        ):
            result = await check_kubernetes()

        assert result.status == ServiceStatus.HEALTHY
        assert "1.30.0" in result.message

    @pytest.mark.asyncio
    async def test_version_probe_exception_returns_unreachable(self):
        api_client_cm = MagicMock()
        api_client_cm.__aenter__ = AsyncMock(return_value="api-client")
        api_client_cm.__aexit__ = AsyncMock(return_value=False)
        version_api = MagicMock()
        version_api.get_code = AsyncMock(side_effect=RuntimeError("version failed"))

        with (
            patch("greenkube.core.k8s_client.get_core_v1_api", new_callable=AsyncMock, return_value=MagicMock()),
            patch("kubernetes_asyncio.client.ApiClient", return_value=api_client_cm),
            patch("kubernetes_asyncio.client.VersionApi", return_value=version_api),
        ):
            result = await check_kubernetes()

        assert result.status == ServiceStatus.UNREACHABLE


class TestRunHealthChecks:
    """Tests for the aggregated health check runner."""

    @pytest.mark.asyncio
    async def test_returns_all_services(self, monkeypatch):
        """run_health_checks should return status for all services."""
        from greenkube.core.config import config

        monkeypatch.setenv("PROMETHEUS_URL", "")
        monkeypatch.setenv("OPENCOST_API_URL", "")
        monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "")
        monkeypatch.setenv("BOAVIZTA_API_URL", "")
        config.reload()

        invalidate_health_cache()

        # Mock all individual checks to return quickly
        prom_health = ServiceHealth(
            name="prometheus",
            status=ServiceStatus.UNCONFIGURED,
            message="Not configured.",
            last_check=datetime.now(timezone.utc),
        )
        oc_health = ServiceHealth(
            name="opencost",
            status=ServiceStatus.UNCONFIGURED,
            message="Not configured.",
            last_check=datetime.now(timezone.utc),
        )
        emaps_health = ServiceHealth(
            name="electricity_maps",
            status=ServiceStatus.UNCONFIGURED,
            message="Not configured.",
            last_check=datetime.now(timezone.utc),
        )
        boavizta_health = ServiceHealth(
            name="boavizta",
            status=ServiceStatus.UNCONFIGURED,
            message="Not configured.",
            last_check=datetime.now(timezone.utc),
        )
        k8s_health = ServiceHealth(
            name="kubernetes",
            status=ServiceStatus.UNREACHABLE,
            message="Not available.",
            last_check=datetime.now(timezone.utc),
        )

        with (
            patch("greenkube.core.health.check_prometheus", new_callable=AsyncMock, return_value=prom_health),
            patch("greenkube.core.health.check_opencost", new_callable=AsyncMock, return_value=oc_health),
            patch("greenkube.core.health.check_electricity_maps", new_callable=AsyncMock, return_value=emaps_health),
            patch("greenkube.core.health.check_boavizta", new_callable=AsyncMock, return_value=boavizta_health),
            patch("greenkube.core.health.check_kubernetes", new_callable=AsyncMock, return_value=k8s_health),
        ):
            result = await run_health_checks(force=True)

        assert result.version is not None
        assert "prometheus" in result.services
        assert "opencost" in result.services
        assert "electricity_maps" in result.services
        assert "boavizta" in result.services
        assert "kubernetes" in result.services

    @pytest.mark.asyncio
    async def test_cache_is_used(self, monkeypatch):
        """Second call within TTL should return cached result."""

        invalidate_health_cache()

        health = ServiceHealth(
            name="prometheus",
            status=ServiceStatus.HEALTHY,
            message="OK",
            last_check=datetime.now(timezone.utc),
        )

        call_count = 0

        async def mock_check_prom(cfg):
            nonlocal call_count
            call_count += 1
            return health

        with (
            patch("greenkube.core.health.check_prometheus", side_effect=mock_check_prom),
            patch(
                "greenkube.core.health.check_opencost",
                new_callable=AsyncMock,
                return_value=ServiceHealth(
                    name="opencost",
                    status=ServiceStatus.UNCONFIGURED,
                    message="",
                    last_check=datetime.now(timezone.utc),
                ),
            ),
            patch(
                "greenkube.core.health.check_electricity_maps",
                new_callable=AsyncMock,
                return_value=ServiceHealth(
                    name="electricity_maps",
                    status=ServiceStatus.UNCONFIGURED,
                    message="",
                    last_check=datetime.now(timezone.utc),
                ),
            ),
            patch(
                "greenkube.core.health.check_boavizta",
                new_callable=AsyncMock,
                return_value=ServiceHealth(
                    name="boavizta",
                    status=ServiceStatus.UNCONFIGURED,
                    message="",
                    last_check=datetime.now(timezone.utc),
                ),
            ),
            patch(
                "greenkube.core.health.check_kubernetes",
                new_callable=AsyncMock,
                return_value=ServiceHealth(
                    name="kubernetes",
                    status=ServiceStatus.UNREACHABLE,
                    message="",
                    last_check=datetime.now(timezone.utc),
                ),
            ),
        ):
            await run_health_checks(force=True)
            await run_health_checks(force=False)  # should use cache
            assert call_count == 1

    @pytest.mark.asyncio
    async def test_force_bypasses_cache(self, monkeypatch):
        """force=True should bypass the cache."""

        invalidate_health_cache()

        call_count = 0

        async def mock_check_prom(cfg):
            nonlocal call_count
            call_count += 1
            return ServiceHealth(
                name="prometheus",
                status=ServiceStatus.HEALTHY,
                message="OK",
                last_check=datetime.now(timezone.utc),
            )

        with (
            patch("greenkube.core.health.check_prometheus", side_effect=mock_check_prom),
            patch(
                "greenkube.core.health.check_opencost",
                new_callable=AsyncMock,
                return_value=ServiceHealth(
                    name="opencost",
                    status=ServiceStatus.UNCONFIGURED,
                    message="",
                    last_check=datetime.now(timezone.utc),
                ),
            ),
            patch(
                "greenkube.core.health.check_electricity_maps",
                new_callable=AsyncMock,
                return_value=ServiceHealth(
                    name="electricity_maps",
                    status=ServiceStatus.UNCONFIGURED,
                    message="",
                    last_check=datetime.now(timezone.utc),
                ),
            ),
            patch(
                "greenkube.core.health.check_boavizta",
                new_callable=AsyncMock,
                return_value=ServiceHealth(
                    name="boavizta",
                    status=ServiceStatus.UNCONFIGURED,
                    message="",
                    last_check=datetime.now(timezone.utc),
                ),
            ),
            patch(
                "greenkube.core.health.check_kubernetes",
                new_callable=AsyncMock,
                return_value=ServiceHealth(
                    name="kubernetes",
                    status=ServiceStatus.UNREACHABLE,
                    message="",
                    last_check=datetime.now(timezone.utc),
                ),
            ),
        ):
            await run_health_checks(force=True)
            await run_health_checks(force=True)
            assert call_count == 2

    @pytest.mark.asyncio
    async def test_runner_skips_exceptions_and_reports_ok_when_remaining_services_are_healthy(self):
        invalidate_health_cache()
        healthy = ServiceHealth(
            name="prometheus",
            status=ServiceStatus.HEALTHY,
            message="OK",
            last_check=datetime.now(timezone.utc),
        )

        with (
            patch("greenkube.core.health.check_prometheus", new_callable=AsyncMock, return_value=healthy),
            patch("greenkube.core.health.check_opencost", new_callable=AsyncMock, side_effect=RuntimeError("boom")),
            patch("greenkube.core.health.check_electricity_maps", new_callable=AsyncMock, return_value=healthy),
            patch("greenkube.core.health.check_boavizta", new_callable=AsyncMock, return_value=healthy),
            patch("greenkube.core.health.check_kubernetes", new_callable=AsyncMock, return_value=healthy),
        ):
            result = await run_health_checks(force=True)

        assert result.status == "ok"
        assert "opencost" not in result.services

    @pytest.mark.asyncio
    async def test_runner_reports_degraded_for_configured_but_not_unreachable_services(self):
        invalidate_health_cache()
        service = ServiceHealth(
            name="prometheus",
            status=ServiceStatus.UNCONFIGURED,
            message="not configured",
            last_check=datetime.now(timezone.utc),
        )

        with (
            patch("greenkube.core.health.check_prometheus", new_callable=AsyncMock, return_value=service),
            patch("greenkube.core.health.check_opencost", new_callable=AsyncMock, return_value=service),
            patch("greenkube.core.health.check_electricity_maps", new_callable=AsyncMock, return_value=service),
            patch("greenkube.core.health.check_boavizta", new_callable=AsyncMock, return_value=service),
            patch("greenkube.core.health.check_kubernetes", new_callable=AsyncMock, return_value=service),
        ):
            result = await run_health_checks(force=True)

        assert result.status == "degraded"


class TestInvalidateHealthCache:
    """Tests for cache invalidation."""

    def test_invalidate_resets_cache(self):
        """invalidate_health_cache should reset the cached result."""
        import greenkube.core.health as health_mod

        health_mod._cached_result = "something"
        health_mod._cached_at = 9999.0

        invalidate_health_cache()

        assert health_mod._cached_result is None
        assert health_mod._cached_at == 0.0
