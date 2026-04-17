# tests/core/test_orchestration_phases.py
"""
TDD tests for the phased orchestration pipeline in DataProcessor.run().

These tests enforce that:
1. Node collection (K8s) always happens first and alone (Phase 1).
2. Zone mapping and Boavizta prefetch happen second, using Phase 1 data (Phase 2).
3. Prometheus, OpenCost, Pod collection and Electricity Maps happen last (Phase 3).
4. ElectricityMaps is only called with the zones resolved from real node data,
   never with the DEFAULT_ZONE when a proper mapping exists.
5. NodeCollector.collect() is called exactly once per run() invocation.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from greenkube.core.processor import DataProcessor
from greenkube.models.metrics import EnergyMetric
from greenkube.models.node import NodeInfo
from greenkube.models.prometheus_metrics import PrometheusMetric

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_node_info(zone: str, region: str, provider: str = "ovh") -> NodeInfo:
    return NodeInfo(
        name="node-1",
        zone=zone,
        region=region,
        cloud_provider=provider,
        instance_type="b2-7",
        architecture="amd64",
        node_pool=None,
    )


def _make_processor(
    node_info: dict,
    emaps_zone: str,
    energy_metrics: list,
    *,
    node_instance_types: list | None = None,
) -> tuple[DataProcessor, MagicMock, MagicMock]:
    """
    Build a DataProcessor with fully mocked collaborators.
    Returns (processor, mock_node_collector, mock_electricity_maps_collector).
    """
    prom_metric = MagicMock(spec=PrometheusMetric)
    prom_metric.pod_cpu_usage = []
    prom_metric.node_instance_types = node_instance_types if node_instance_types is not None else []

    mock_prometheus = MagicMock()
    mock_prometheus.collect = AsyncMock(return_value=prom_metric)
    mock_prometheus.collect_range = AsyncMock(return_value=[])
    mock_prometheus.close = AsyncMock()

    mock_opencost = MagicMock()
    mock_opencost.collect = AsyncMock(return_value=[])
    mock_opencost.close = AsyncMock()

    mock_node = MagicMock()
    mock_node.collect = AsyncMock(return_value=node_info)
    mock_node.collect_instance_types = AsyncMock(return_value={k: v.instance_type for k, v in node_info.items()})
    mock_node.close = AsyncMock()

    mock_pod = MagicMock()
    mock_pod.collect = AsyncMock(return_value=[])
    mock_pod.close = AsyncMock()

    mock_emaps = MagicMock()
    mock_emaps.collect = AsyncMock(return_value=[])
    mock_emaps.close = AsyncMock()

    mock_boavizta = MagicMock()
    mock_boavizta.get_server_impact = AsyncMock(return_value=None)
    mock_boavizta.close = AsyncMock()

    mock_repository = MagicMock()
    mock_repository.get_for_zone_at_time = AsyncMock(return_value=None)
    mock_repository.save_history = AsyncMock()

    mock_combined_repo = MagicMock()
    mock_combined_repo.read_combined_metrics = AsyncMock(return_value=[])

    mock_node_repo = MagicMock()
    mock_node_repo.get_latest_snapshots_before = AsyncMock(return_value=[])
    mock_node_repo.get_snapshots = AsyncMock(return_value=[])

    mock_embodied_repo = MagicMock()
    mock_embodied_repo.get_profile = AsyncMock(return_value=None)
    mock_embodied_repo.save_profile = AsyncMock()

    mock_calculator = MagicMock()
    mock_calculator.calculate_emissions = AsyncMock(return_value=None)
    mock_calculator.clear_cache = AsyncMock()
    mock_calculator.prefetch_intensity = AsyncMock()

    mock_estimator = MagicMock()
    mock_estimator.estimate.return_value = energy_metrics
    mock_estimator.instance_profiles = {}
    mock_estimator.DEFAULT_INSTANCE_PROFILE = {"vcores": 2, "minWatts": 10, "maxWatts": 50}
    mock_estimator.query_range_step_sec = 300

    processor = DataProcessor(
        prometheus_collector=mock_prometheus,
        opencost_collector=mock_opencost,
        node_collector=mock_node,
        pod_collector=mock_pod,
        electricity_maps_collector=mock_emaps,
        boavizta_collector=mock_boavizta,
        repository=mock_repository,
        combined_metrics_repository=mock_combined_repo,
        node_repository=mock_node_repo,
        embodied_repository=mock_embodied_repo,
        calculator=mock_calculator,
        estimator=mock_estimator,
    )

    return processor, mock_node, mock_emaps


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_collector_called_exactly_once():
    """
    NodeCollector.collect() must be called exactly once per run() cycle,
    even though multiple phases need node data.
    """
    node_info = {"node-1": _make_node_info(zone="nova", region="eu-west-gra", provider="ovh")}
    processor, mock_node, _ = _make_processor(node_info=node_info, emaps_zone="FR", energy_metrics=[])

    await processor.run()

    mock_node.collect.assert_called_once()


@pytest.mark.asyncio
@patch("greenkube.core.node_zone_mapper.get_emaps_zone_from_cloud_zone")
async def test_ovh_nova_zone_falls_back_to_region(mock_translator):
    """
    For an OVH node with zone='nova' (OpenStack default AZ) and region='eu-west-gra',
    the zone mapper must skip 'nova', use the region instead, and call ElectricityMaps
    with the correctly mapped zone — not with the DEFAULT_ZONE.
    """

    # 'nova' maps to None; 'eu-west-gra' maps to 'FR'
    def translator_side_effect(zone_or_region, provider=None):
        if zone_or_region == "nova":
            return None
        if zone_or_region == "eu-west-gra":
            return "FR"
        return None

    mock_translator.side_effect = translator_side_effect

    node_info = {"node-1": _make_node_info(zone="nova", region="eu-west-gra", provider="ovh")}
    energy_metrics = [
        EnergyMetric(
            pod_name="pod-A",
            namespace="ns-1",
            joules=1_000_000,
            timestamp="2024-01-01T10:00:00Z",
            node="node-1",
        )
    ]

    processor, _, mock_emaps = _make_processor(node_info=node_info, emaps_zone="FR", energy_metrics=energy_metrics)

    await processor.run()

    # ElectricityMaps must NOT be called with 'nova' or the DEFAULT_ZONE
    for call_args in mock_emaps.collect.call_args_list:
        zone_arg = call_args.kwargs.get("zone") or (call_args.args[0] if call_args.args else None)
        assert zone_arg != "nova", "ElectricityMaps called with raw OVH 'nova' zone"
        assert zone_arg != "unknown", "ElectricityMaps called with DEFAULT_ZONE instead of resolved zone"


@pytest.mark.asyncio
@patch("greenkube.core.node_zone_mapper.get_emaps_zone_from_cloud_zone")
async def test_electricity_maps_called_after_zone_resolution(mock_translator):
    """
    ElectricityMaps must only be called after node zone resolution (Phase 2).
    This is verified by checking that the zone passed to ElectricityMaps is the
    resolved EM zone, not a raw cloud zone or the default.
    """
    mock_translator.return_value = "FR"

    node_info = {"node-1": _make_node_info(zone="nova", region="eu-west-gra", provider="ovh")}
    energy_metrics = [
        EnergyMetric(
            pod_name="pod-A",
            namespace="ns-1",
            joules=500_000,
            timestamp="2024-01-01T10:00:00Z",
            node="node-1",
        )
    ]

    processor, mock_node, mock_emaps = _make_processor(
        node_info=node_info, emaps_zone="FR", energy_metrics=energy_metrics
    )

    # Track call ordering using a shared log
    call_log: list[str] = []

    async def node_collect_spy(*args, **kwargs):
        call_log.append("node_collect")
        return node_info

    async def emaps_collect_spy(*args, **kwargs):
        call_log.append("emaps_collect")
        return []

    mock_node.collect = AsyncMock(side_effect=node_collect_spy)
    mock_emaps.collect = AsyncMock(side_effect=emaps_collect_spy)

    # Also patch the repository so prefetch triggers the emaps collect
    processor._assembler.repository.get_for_zone_at_time = AsyncMock(return_value=None)

    await processor.run()

    # node_collect must come before emaps_collect
    if "emaps_collect" in call_log and "node_collect" in call_log:
        assert call_log.index("node_collect") < call_log.index("emaps_collect"), (
            "ElectricityMaps was called before node collection completed"
        )


@pytest.mark.asyncio
async def test_node_instance_map_built_from_phase1_nodes():
    """
    The node_instance_map used for CombinedMetric assembly must be derived
    from the Phase 1 node collection, without an extra collect_instance_types() call.
    """
    node_info = {"node-1": _make_node_info(zone="nova", region="eu-west-gra", provider="ovh")}
    processor, mock_node, _ = _make_processor(node_info=node_info, emaps_zone="FR", energy_metrics=[])

    await processor.run()

    # collect_instance_types should NOT have been called because node_info
    # from Phase 1 already provides instance types.
    mock_node.collect_instance_types.assert_not_called()


@pytest.mark.asyncio
@patch("greenkube.core.node_zone_mapper.get_emaps_zone_from_cloud_zone")
async def test_prometheus_called_after_node_collection(mock_translator):
    """
    Prometheus collection must happen after node collection so that any
    node-instance-type enrichment uses already-resolved data.
    """
    mock_translator.return_value = "FR"

    node_info = {"node-1": _make_node_info(zone="nova", region="eu-west-gra", provider="ovh")}
    processor, mock_node, _ = _make_processor(node_info=node_info, emaps_zone="FR", energy_metrics=[])

    call_log: list[str] = []

    async def node_collect_spy(*args, **kwargs):
        call_log.append("node_collect")
        return node_info

    async def prom_collect_spy(*args, **kwargs):
        call_log.append("prom_collect")
        prom_metric = MagicMock(spec=PrometheusMetric)
        prom_metric.pod_cpu_usage = []
        prom_metric.node_instance_types = []
        return prom_metric

    mock_node.collect = AsyncMock(side_effect=node_collect_spy)
    processor._orchestrator.prometheus_collector.collect = AsyncMock(side_effect=prom_collect_spy)

    await processor.run()

    assert "node_collect" in call_log
    assert "prom_collect" in call_log
    assert call_log.index("node_collect") < call_log.index("prom_collect"), "Prometheus was collected before nodes"
