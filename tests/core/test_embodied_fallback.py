# tests/core/test_embodied_fallback.py
"""Tests for Boavizta embodied emissions fallback when API returns no data."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from greenkube.core.config import Config
from greenkube.core.embodied_service import EmbodiedEmissionsService
from greenkube.models.node import NodeInfo


@pytest.fixture
def mock_config():
    """Config with known fallback values."""
    with patch.dict(
        "os.environ",
        {
            "DEFAULT_EMBODIED_EMISSIONS_KG": "350",
            "DEFAULT_HARDWARE_LIFESPAN_YEARS": "4",
        },
    ):
        return Config()


@pytest.fixture
def service(mock_config):
    """EmbodiedEmissionsService wired with mocks."""
    boavizta = MagicMock()
    boavizta.get_server_impact = AsyncMock(return_value=None)
    embodied_repo = MagicMock()
    embodied_repo.get_profile = AsyncMock(return_value=None)
    embodied_repo.save_profile = AsyncMock()
    node_repo = MagicMock()
    node_repo.save_nodes = AsyncMock()
    calculator = MagicMock()
    calculator.calculate_embodied_emissions = MagicMock(return_value=5.0)
    estimator = MagicMock()
    estimator.instance_profiles = {}
    estimator.query_range_step_sec = 300

    return EmbodiedEmissionsService(
        boavizta_collector=boavizta,
        embodied_repository=embodied_repo,
        node_repository=node_repo,
        calculator=calculator,
        estimator=estimator,
        config=mock_config,
    )


# ------------------------------------------------------------------
# Tests for prepare_embodied_data fallback
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_embodied_data_injects_fallback_when_api_returns_none(service, mock_config):
    """When Boavizta API returns None (unknown provider/instance),
    prepare_embodied_data should inject a fallback profile with is_fallback=True."""
    nodes_info = {
        "node-1": NodeInfo(
            name="node-1",
            cloud_provider="unknowncloud",
            instance_type="x1.mystery",
            zone="eu-west-1a",
        )
    }

    cache = await service.prepare_embodied_data(nodes_info)

    key = ("unknowncloud", "x1.mystery")
    assert key in cache, "Fallback profile should be injected into cache"
    profile = cache[key]
    assert profile["is_fallback"] is True
    assert profile["gwp_manufacture"] == mock_config.DEFAULT_EMBODIED_EMISSIONS_KG
    assert profile["lifespan_hours"] == mock_config.DEFAULT_HARDWARE_LIFESPAN_YEARS * 8760


@pytest.mark.asyncio
async def test_prepare_embodied_data_no_fallback_when_api_succeeds(mock_config):
    """When Boavizta API returns real data, no fallback flag should be set."""
    impact = MagicMock()
    impact.impacts.gwp.manufacture = 500.0

    boavizta = MagicMock()
    boavizta.get_server_impact = AsyncMock(return_value=impact)
    embodied_repo = MagicMock()
    embodied_repo.get_profile = AsyncMock(return_value=None)
    embodied_repo.save_profile = AsyncMock()
    node_repo = MagicMock()
    node_repo.save_nodes = AsyncMock()
    calculator = MagicMock()
    estimator = MagicMock()
    estimator.instance_profiles = {}

    svc = EmbodiedEmissionsService(
        boavizta_collector=boavizta,
        embodied_repository=embodied_repo,
        node_repository=node_repo,
        calculator=calculator,
        estimator=estimator,
        config=mock_config,
    )

    nodes_info = {
        "node-1": NodeInfo(
            name="node-1",
            cloud_provider="aws",
            instance_type="m5.large",
            zone="us-east-1a",
        )
    }

    cache = await svc.prepare_embodied_data(nodes_info)
    key = ("aws", "m5.large")
    assert key in cache
    assert cache[key].get("is_fallback") is not True


@pytest.mark.asyncio
async def test_prepare_embodied_data_no_fallback_when_cached_in_db(mock_config):
    """When the profile exists in the DB cache, no fallback should be used."""
    boavizta = MagicMock()
    boavizta.get_server_impact = AsyncMock()
    embodied_repo = MagicMock()
    embodied_repo.get_profile = AsyncMock(return_value={"gwp_manufacture": 400.0, "lifespan_hours": 35040})
    node_repo = MagicMock()
    node_repo.save_nodes = AsyncMock()
    calculator = MagicMock()
    estimator = MagicMock()
    estimator.instance_profiles = {}

    svc = EmbodiedEmissionsService(
        boavizta_collector=boavizta,
        embodied_repository=embodied_repo,
        node_repository=node_repo,
        calculator=calculator,
        estimator=estimator,
        config=mock_config,
    )

    nodes_info = {
        "node-1": NodeInfo(
            name="node-1",
            cloud_provider="aws",
            instance_type="m5.large",
            zone="us-east-1a",
        )
    }

    cache = await svc.prepare_embodied_data(nodes_info)
    key = ("aws", "m5.large")
    assert key in cache
    assert cache[key].get("is_fallback") is not True
    # API should NOT have been called
    boavizta.get_server_impact.assert_not_called()


# ------------------------------------------------------------------
# Tests for is_embodied_fallback helper
# ------------------------------------------------------------------


def test_is_embodied_fallback_returns_true_for_fallback_profile(service):
    """is_embodied_fallback should return True when the cache entry has is_fallback."""
    node = NodeInfo(name="n1", cloud_provider="x", instance_type="y", zone="z")
    cache = {("x", "y"): {"gwp_manufacture": 350, "lifespan_hours": 35040, "is_fallback": True}}
    assert service.is_embodied_fallback(node, cache) is True


def test_is_embodied_fallback_returns_false_for_real_profile(service):
    """is_embodied_fallback should return False for a real Boavizta profile."""
    node = NodeInfo(name="n1", cloud_provider="aws", instance_type="m5.large", zone="z")
    cache = {("aws", "m5.large"): {"gwp_manufacture": 500, "lifespan_hours": 35040}}
    assert service.is_embodied_fallback(node, cache) is False


def test_is_embodied_fallback_returns_false_when_no_node(service):
    """is_embodied_fallback should return False when node_info is None."""
    assert service.is_embodied_fallback(None, {}) is False


# ------------------------------------------------------------------
# Tests for calculate_pod_embodied with fallback
# ------------------------------------------------------------------


def test_calculate_pod_embodied_uses_fallback_profile(service):
    """calculate_pod_embodied should use the fallback profile and return non-zero."""
    node = NodeInfo(
        name="n1",
        cloud_provider="unknown",
        instance_type="mystery",
        zone="z",
        cpu_capacity_cores=4,
    )
    cache = {
        ("unknown", "mystery"): {
            "gwp_manufacture": 350,
            "lifespan_hours": 35040,
            "is_fallback": True,
        }
    }
    pod_requests = {"cpu": 500, "memory": 1024}

    result = service.calculate_pod_embodied(node, cache, pod_requests)
    # Should delegate to calculator which returns 5.0 in fixture
    assert result == 5.0
    service.calculator.calculate_embodied_emissions.assert_called_once()


def test_calculate_pod_embodied_uses_cpu_usage_when_no_requests(service):
    """When cpu request is 0 but CPU usage is provided, embodied should be non-zero."""
    node = NodeInfo(
        name="n1",
        cloud_provider="unknown",
        instance_type="minikube",
        zone="z",
        cpu_capacity_cores=4,
    )
    cache = {
        ("unknown", "minikube"): {
            "gwp_manufacture": 350,
            "lifespan_hours": 35040,
            "is_fallback": True,
        }
    }
    # No CPU request set (common in local/dev environments)
    pod_requests = {"cpu": 0, "memory": 0}

    result = service.calculate_pod_embodied(node, cache, pod_requests, cpu_usage_millicores=200.0)
    assert result == 5.0, "Should use CPU usage as fallback for share calculation"
    service.calculator.calculate_embodied_emissions.assert_called_once()


def test_calculate_pod_embodied_returns_zero_when_no_cpu_at_all(service):
    """When both cpu request and cpu usage are 0, embodied should still be 0."""
    node = NodeInfo(
        name="n1",
        cloud_provider="unknown",
        instance_type="minikube",
        zone="z",
        cpu_capacity_cores=4,
    )
    cache = {
        ("unknown", "minikube"): {
            "gwp_manufacture": 350,
            "lifespan_hours": 35040,
            "is_fallback": True,
        }
    }
    pod_requests = {"cpu": 0, "memory": 0}

    result = service.calculate_pod_embodied(node, cache, pod_requests, cpu_usage_millicores=0.0)
    assert result == 0.0


# ------------------------------------------------------------------
# Config default value
# ------------------------------------------------------------------


def test_default_embodied_emissions_kg_config():
    """DEFAULT_EMBODIED_EMISSIONS_KG should default to 350 and be overridable."""
    with patch.dict("os.environ", {}, clear=False):
        c = Config()
        assert c.DEFAULT_EMBODIED_EMISSIONS_KG == 350.0

    with patch.dict("os.environ", {"DEFAULT_EMBODIED_EMISSIONS_KG": "700"}):
        c2 = Config()
        assert c2.DEFAULT_EMBODIED_EMISSIONS_KG == 700.0
