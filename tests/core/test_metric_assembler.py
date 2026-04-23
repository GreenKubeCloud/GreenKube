# tests/core/test_metric_assembler.py
"""
Tests for MetricAssembler — the core component that builds CombinedMetric objects.

These tests guarantee that:
1. CombinedMetric is correctly assembled from energy, cost, node context, and pod data.
2. co2e_grams comes directly from the CarbonCalculator result.
3. Negative joules are clamped to 0 and co2e set to 0 accordingly.
4. When no node context exists, DEFAULT_ZONE is used and is_estimated is True.
5. When the calculator fails, the metric is skipped (no crash, empty list).
6. When cost data is available, it is applied to total_cost.
7. The emaps_zone from NodeZoneContext is stored in CombinedMetric.

MetricAssembler is intentionally untested so far — it is the critical bridge
between collection and storage.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.core.calculator import CarbonCalculationResult
from greenkube.core.metric_assembler import MetricAssembler
from greenkube.core.prometheus_resource_mapper import PodResourceMaps
from greenkube.models.metrics import CostMetric, EnergyMetric
from greenkube.models.node import NodeInfo, NodeZoneContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _energy(pod="pod-a", namespace="ns-1", joules=1_000_000.0, node="node-1") -> EnergyMetric:
    return EnergyMetric(pod_name=pod, namespace=namespace, joules=joules, timestamp=_TS, node=node)


def _context(emaps_zone="FR", is_estimated=False) -> NodeZoneContext:
    return NodeZoneContext(node="node-1", emaps_zone=emaps_zone, is_estimated=is_estimated)


def _node_info(provider="aws", instance_type="m5.large") -> NodeInfo:
    return NodeInfo(
        name="node-1",
        zone="us-east-1a",
        region="us-east-1",
        cloud_provider=provider,
        instance_type=instance_type,
        architecture="amd64",
        node_pool=None,
    )


def _empty_resource_maps() -> PodResourceMaps:
    return PodResourceMaps(
        cpu_usage_map={},
        memory_usage_map={},
        network_rx_map={},
        network_tx_map={},
        disk_read_map={},
        disk_write_map={},
        restart_map={},
    )


def _carbon_result(co2e=50.0, intensity=100.0) -> CarbonCalculationResult:
    return CarbonCalculationResult(co2e_grams=co2e, grid_intensity=intensity, grid_intensity_timestamp=_TS)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_calculator():
    calc = MagicMock()
    calc.calculate_emissions = AsyncMock(return_value=_carbon_result())
    calc.prefetch_intensity = AsyncMock()
    calc.clear_cache = AsyncMock()
    return calc


@pytest.fixture
def mock_estimator():
    est = MagicMock()
    est.query_range_step_sec = 300
    return est


@pytest.fixture
def mock_repository():
    repo = MagicMock()
    repo.get_for_zone_at_time = AsyncMock(return_value=100.0)
    repo.save_history = AsyncMock()
    return repo


@pytest.fixture
def mock_electricity_maps():
    em = MagicMock()
    em.collect = AsyncMock(return_value=[])
    return em


@pytest.fixture
def mock_zone_mapper():
    return MagicMock()


@pytest.fixture
def mock_embodied_service():
    es = MagicMock()
    es.calculate_pod_embodied = MagicMock(return_value=10.0)
    es.is_embodied_fallback = MagicMock(return_value=False)
    return es


@pytest.fixture
def assembler(
    mock_calculator,
    mock_estimator,
    mock_repository,
    mock_electricity_maps,
    mock_zone_mapper,
    mock_embodied_service,
) -> MetricAssembler:
    return MetricAssembler(
        calculator=mock_calculator,
        estimator=mock_estimator,
        repository=mock_repository,
        electricity_maps_collector=mock_electricity_maps,
        zone_mapper=mock_zone_mapper,
        embodied_service=mock_embodied_service,
    )


async def _assemble_one(assembler, energy_metric, *, context=None, node_info=None, cost_map=None):
    """Helper to call assemble() with a single energy metric."""
    node_contexts = {"node-1": context} if context else {}
    nodes_info = {"node-1": node_info} if node_info else {}
    return await assembler.assemble(
        energy_metrics=[energy_metric],
        cost_map=cost_map or {},
        pod_request_map={},
        node_contexts=node_contexts,
        nodes_info=nodes_info,
        node_instance_map={"node-1": "m5.large"} if node_info else {},
        boavizta_cache={},
        cpu_adjusted_nodes=set(),
        steps_per_day=288.0,
        resource_maps=_empty_resource_maps(),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestMetricAssemblerHappyPath:
    """Correct CombinedMetric assembly from complete data."""

    @pytest.mark.asyncio
    async def test_assemble_produces_one_combined_metric(self, assembler):
        """One energy metric produces exactly one CombinedMetric."""
        result = await _assemble_one(assembler, _energy(), context=_context(), node_info=_node_info())

        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_combined_metric_pod_and_namespace(self, assembler):
        """pod_name and namespace are correctly carried into CombinedMetric."""
        result = await _assemble_one(
            assembler, _energy(pod="my-pod", namespace="my-ns"), context=_context(), node_info=_node_info()
        )

        assert result[0].pod_name == "my-pod"
        assert result[0].namespace == "my-ns"

    @pytest.mark.asyncio
    async def test_co2e_comes_from_calculator(self, assembler, mock_calculator):
        """co2e_grams in CombinedMetric equals what the calculator returned."""
        mock_calculator.calculate_emissions = AsyncMock(return_value=_carbon_result(co2e=123.45))

        result = await _assemble_one(assembler, _energy(), context=_context(), node_info=_node_info())

        assert result[0].co2e_grams == pytest.approx(123.45)

    @pytest.mark.asyncio
    async def test_grid_intensity_from_calculator(self, assembler, mock_calculator):
        """grid_intensity in CombinedMetric equals the intensity used by the calculator."""
        mock_calculator.calculate_emissions = AsyncMock(return_value=_carbon_result(intensity=250.0))

        result = await _assemble_one(assembler, _energy(), context=_context(), node_info=_node_info())

        assert result[0].grid_intensity == pytest.approx(250.0)

    @pytest.mark.asyncio
    async def test_emaps_zone_from_node_context(self, assembler):
        """emaps_zone in CombinedMetric is taken from the node context."""
        result = await _assemble_one(assembler, _energy(), context=_context(emaps_zone="DE"), node_info=_node_info())

        assert result[0].emaps_zone == "DE"

    @pytest.mark.asyncio
    async def test_joules_preserved_in_combined_metric(self, assembler):
        """joules in CombinedMetric matches the input EnergyMetric."""
        result = await _assemble_one(assembler, _energy(joules=750_000.0), context=_context(), node_info=_node_info())

        assert result[0].joules == pytest.approx(750_000.0)

    @pytest.mark.asyncio
    async def test_cost_applied_from_cost_map(self, assembler):
        """total_cost is non-zero when a matching CostMetric exists in cost_map."""
        cost = CostMetric(pod_name="pod-a", namespace="ns-1", cpu_cost=0.10, ram_cost=0.20, total_cost=0.30)
        result = await _assemble_one(
            assembler,
            _energy(),
            context=_context(),
            node_info=_node_info(),
            cost_map={"pod-a": cost},
        )

        # Cost is normalised per step; steps_per_day=288 → per-step cost
        assert result[0].total_cost > 0

    @pytest.mark.asyncio
    async def test_pue_applied_from_provider_profile(self, assembler):
        """pue in CombinedMetric is non-zero and ≥ 1.0."""
        result = await _assemble_one(assembler, _energy(), context=_context(), node_info=_node_info())

        assert result[0].pue >= 1.0

    @pytest.mark.asyncio
    async def test_node_instance_type_stored(self, assembler):
        """node_instance_type is copied from nodes_info into CombinedMetric."""
        result = await _assemble_one(assembler, _energy(), context=_context(), node_info=_node_info())

        assert result[0].node_instance_type == "m5.large"

    @pytest.mark.asyncio
    async def test_empty_energy_metrics_produces_empty_list(self, assembler):
        """No energy metrics → empty result list."""
        result = await assembler.assemble(
            energy_metrics=[],
            cost_map={},
            pod_request_map={},
            node_contexts={},
            nodes_info={},
            node_instance_map={},
            boavizta_cache={},
            cpu_adjusted_nodes=set(),
            steps_per_day=288.0,
            resource_maps=_empty_resource_maps(),
        )
        assert result == []


# ---------------------------------------------------------------------------
# Estimation flags & fallback behaviour
# ---------------------------------------------------------------------------


class TestMetricAssemblerEstimationFlags:
    """Tests for is_estimated flag and estimation_reasons population."""

    @pytest.mark.asyncio
    async def test_is_estimated_true_when_no_node_context(self, assembler):
        """When no node context exists for the node, is_estimated must be True."""
        # No context for "node-1"
        result = await assembler.assemble(
            energy_metrics=[_energy(node="orphan-node")],
            cost_map={},
            pod_request_map={},
            node_contexts={},
            nodes_info={},
            node_instance_map={},
            boavizta_cache={},
            cpu_adjusted_nodes=set(),
            steps_per_day=288.0,
            resource_maps=_empty_resource_maps(),
        )

        assert len(result) == 1
        assert result[0].is_estimated is True
        assert any("orphan-node" in reason for reason in result[0].estimation_reasons)

    @pytest.mark.asyncio
    async def test_is_estimated_true_when_no_cost_data(self, assembler):
        """When no cost data exists for the pod, is_estimated must be True."""
        result = await _assemble_one(
            assembler,
            _energy(),
            context=_context(),
            node_info=_node_info(),
            cost_map={},  # No cost for pod-a
        )

        assert result[0].is_estimated is True
        assert any("cost" in r.lower() for r in result[0].estimation_reasons)

    @pytest.mark.asyncio
    async def test_is_estimated_false_for_fully_resolved_metric(self, assembler):
        """With all data available from known provider, is_estimated should be False."""
        from greenkube.core.config import get_config

        cfg = get_config()
        # Use a provider whose PUE profile IS defined so no estimation reason is added
        provider = next(
            (p.replace("default_", "") for p in cfg.DATACENTER_PUE_PROFILES if p.startswith("default_")),
            "aws",
        )
        cost = CostMetric(pod_name="pod-a", namespace="ns-1", cpu_cost=0.1, ram_cost=0.1, total_cost=0.2)
        context = NodeZoneContext(node="node-1", emaps_zone="FR", is_estimated=False)
        node_info = _node_info(provider=provider)

        result = await _assemble_one(
            assembler,
            _energy(),
            context=context,
            node_info=node_info,
            cost_map={"pod-a": cost},
        )

        assert len(result) == 1
        assert result[0].is_estimated is False

    @pytest.mark.asyncio
    async def test_estimation_reasons_not_empty_when_estimated(self, assembler):
        """When is_estimated is True, estimation_reasons must contain at least one entry."""
        result = await _assemble_one(assembler, _energy(), context=None)

        assert result[0].is_estimated is True
        assert len(result[0].estimation_reasons) > 0


# ---------------------------------------------------------------------------
# Data integrity — sanity checks & edge cases
# ---------------------------------------------------------------------------


class TestMetricAssemblerDataIntegrity:
    """Tests that guard against obviously wrong values in assembled metrics."""

    @pytest.mark.asyncio
    async def test_negative_joules_clamped_to_zero(self, assembler, mock_calculator):
        """Negative joules must be clamped to 0; co2e must also be 0."""
        mock_calculator.calculate_emissions = AsyncMock(return_value=_carbon_result(co2e=50.0))

        result = await _assemble_one(assembler, _energy(joules=-500.0), context=_context(), node_info=_node_info())

        assert len(result) == 1
        assert result[0].joules == pytest.approx(0.0)
        assert result[0].co2e_grams == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_calculator_failure_skips_metric(self, assembler, mock_calculator):
        """When the calculator raises, the metric is skipped — no crash, empty result."""
        mock_calculator.calculate_emissions = AsyncMock(side_effect=RuntimeError("Calculator failed"))

        result = await _assemble_one(assembler, _energy(), context=_context(), node_info=_node_info())

        assert result == []

    @pytest.mark.asyncio
    async def test_calculator_returns_none_skips_metric(self, assembler, mock_calculator):
        """When calculate_emissions returns None, the metric is skipped gracefully."""
        mock_calculator.calculate_emissions = AsyncMock(return_value=None)

        result = await _assemble_one(assembler, _energy(), context=_context(), node_info=_node_info())

        assert result == []

    @pytest.mark.asyncio
    async def test_multiple_energy_metrics_produce_multiple_combined_metrics(self, assembler):
        """Multiple energy metrics all produce corresponding CombinedMetric objects."""
        energy_metrics = [
            EnergyMetric(pod_name="pod-a", namespace="ns-1", joules=100_000.0, timestamp=_TS, node="node-1"),
            EnergyMetric(pod_name="pod-b", namespace="ns-2", joules=200_000.0, timestamp=_TS, node="node-1"),
        ]
        node_contexts = {"node-1": _context()}
        nodes_info = {"node-1": _node_info()}

        result = await assembler.assemble(
            energy_metrics=energy_metrics,
            cost_map={},
            pod_request_map={},
            node_contexts=node_contexts,
            nodes_info=nodes_info,
            node_instance_map={},
            boavizta_cache={},
            cpu_adjusted_nodes=set(),
            steps_per_day=288.0,
            resource_maps=_empty_resource_maps(),
        )

        assert len(result) == 2
        pod_names = {m.pod_name for m in result}
        assert pod_names == {"pod-a", "pod-b"}

    @pytest.mark.asyncio
    async def test_duration_seconds_set_from_estimator(self, assembler, mock_estimator):
        """duration_seconds in CombinedMetric equals estimator.query_range_step_sec."""
        mock_estimator.query_range_step_sec = 600

        result = await _assemble_one(assembler, _energy(), context=_context(), node_info=_node_info())

        assert result[0].duration_seconds == 600

    @pytest.mark.asyncio
    async def test_timestamp_preserved(self, assembler):
        """The timestamp from EnergyMetric is preserved in CombinedMetric."""
        ts = datetime(2026, 3, 15, 8, 30, 0, tzinfo=timezone.utc)
        metric = EnergyMetric(pod_name="pod-a", namespace="ns-1", joules=100_000.0, timestamp=ts, node="node-1")

        result = await _assemble_one(assembler, metric, context=_context(), node_info=_node_info())

        assert result[0].timestamp == ts
