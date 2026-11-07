"""Provenance of assembled pod and container rows (roadmap A4, A7, A11; BUG-04, BUG-29)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.core.calculator import CarbonCalculationResult
from greenkube.core.metric_assembler import MetricAssembler, cost_fields, power_source_for_profile
from greenkube.core.prometheus_resource_mapper import PodResourceMaps
from greenkube.models.metrics import CombinedMetric, CostMetric, EnergyMetric, PodMetric
from greenkube.models.node import NodeInfo, NodeZoneContext
from greenkube.models.provenance import is_cost_trusted, is_usage_trusted

TS = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)


def _assembler(intensity_source: str = "consolidated") -> MetricAssembler:
    calculator = MagicMock()
    calculator.calculate_emissions = AsyncMock(
        return_value=CarbonCalculationResult(
            co2e_grams=50.0, grid_intensity=100.0, grid_intensity_timestamp=TS, intensity_source=intensity_source
        )
    )
    estimator = MagicMock()
    estimator.query_range_step_sec = 300
    embodied = MagicMock()
    embodied.calculate_pod_embodied = MagicMock(return_value=0.0)
    embodied.is_embodied_fallback = MagicMock(return_value=False)
    return MetricAssembler(
        calculator=calculator,
        estimator=estimator,
        repository=MagicMock(),
        electricity_provider=MagicMock(),
        zone_mapper=MagicMock(),
        embodied_service=embodied,
    )


def _assemble_args(cost_map: dict, pods: list[EnergyMetric]) -> dict:
    return {
        "energy_metrics": pods,
        "cost_map": cost_map,
        "pod_request_map": {},
        "node_contexts": {"node-1": NodeZoneContext(node="node-1", emaps_zone="FR", is_estimated=False)},
        "nodes_info": {
            "node-1": NodeInfo(
                name="node-1",
                zone="eu-west-3a",
                region="eu-west-3",
                cloud_provider="aws",
                instance_type="m5.large",
                architecture="amd64",
                node_pool=None,
            )
        },
        "node_instance_map": {"node-1": "m5.large"},
        "boavizta_cache": {},
        "cpu_adjusted_nodes": set(),
        "steps_per_day": 1.0,
        "resource_maps": PodResourceMaps(cpu_usage_map={("cache", "redis-0"): 100, ("queue", "redis-0"): 50}),
    }


def _energy(namespace: str, power_source: str | None = "instance_profile") -> EnergyMetric:
    return EnergyMetric(
        pod_name="redis-0", namespace=namespace, joules=1000.0, timestamp=TS, node="node-1", power_source=power_source
    )


@pytest.mark.asyncio
async def test_same_named_pods_in_two_namespaces_keep_their_own_cost():
    cost_map = {
        ("cache", "redis-0"): CostMetric(
            pod_name="redis-0", namespace="cache", cpu_cost=0.6, ram_cost=0.3, storage_cost=0.1, total_cost=1.0
        ),
        ("queue", "redis-0"): CostMetric(
            pod_name="redis-0", namespace="queue", cpu_cost=0.1, ram_cost=0.1, total_cost=0.2
        ),
    }

    rows = await _assembler().assemble(**_assemble_args(cost_map, [_energy("cache"), _energy("queue")]))

    by_namespace = {row.namespace: row for row in rows}
    assert by_namespace["cache"].total_cost == pytest.approx(1.0)
    assert by_namespace["queue"].total_cost == pytest.approx(0.2)
    assert by_namespace["cache"].cost_cpu == pytest.approx(0.6)
    assert by_namespace["cache"].cost_storage == pytest.approx(0.1)
    assert all(row.cost_source == "opencost" for row in rows)


@pytest.mark.asyncio
async def test_a_legacy_pod_name_key_never_leaks_into_another_namespace():
    cost_map = {"redis-0": CostMetric(pod_name="redis-0", namespace="cache", cpu_cost=1, ram_cost=0, total_cost=1)}

    rows = await _assembler().assemble(**_assemble_args(cost_map, [_energy("queue")]))

    assert rows[0].cost_source == "none"
    assert rows[0].total_cost == 0.0


@pytest.mark.asyncio
async def test_pod_rows_carry_usage_power_and_intensity_provenance():
    rows = await _assembler("zone_default").assemble(**_assemble_args({}, [_energy("cache", power_source=None)]))

    row = rows[0]
    assert row.usage_source == "prometheus"
    assert row.intensity_source == "zone_default"
    assert row.cost_source == "none"
    assert row.is_estimated is True
    assert any("intensity" in reason.lower() for reason in row.estimation_reasons)


def _aggregate(**overrides) -> CombinedMetric:
    values = {
        "pod_name": "web-0",
        "namespace": "shop",
        "timestamp": TS,
        "duration_seconds": 300,
        "total_cost": 1.0,
        "cost_cpu": 0.5,
        "cost_ram": 0.3,
        "cost_storage": 0.1,
        "cost_network": 0.05,
        "cost_lb": 0.05,
        "joules": 1000.0,
        "co2e_grams": 10.0,
        "emaps_zone": "FR",
        "cost_source": "opencost",
        "usage_source": "prometheus",
        "power_source": "instance_profile",
        "intensity_source": "consolidated",
        "is_estimated": False,
    }
    values.update(overrides)
    return CombinedMetric(**values)


def _containers() -> list[PodMetric]:
    return [
        PodMetric(
            pod_name="web-0",
            namespace="shop",
            container_name="app",
            cpu_request=300,
            memory_request=300,
            cpu_limit=1000,
            memory_limit=600,
        ),
        PodMetric(
            pod_name="web-0",
            namespace="shop",
            container_name="proxy",
            cpu_request=100,
            memory_request=100,
            kind="sidecar",
        ),
        PodMetric(
            pod_name="web-0", namespace="shop", container_name="migrate", cpu_request=900, memory_request=0, kind="init"
        ),
    ]


def test_container_rows_are_not_blanket_estimated_and_keep_provenance():
    """BUG-29: OpenCost-priced, Prometheus-measured container rows are trusted, not estimated."""
    maps = PodResourceMaps(
        container_cpu_usage_map={("shop", "web-0", "app"): 75, ("shop", "web-0", "proxy"): 25},
        container_memory_usage_map={("shop", "web-0", "app"): 100, ("shop", "web-0", "proxy"): 300},
    )

    rows = MetricAssembler.assemble_container_metrics([_aggregate()], _containers(), maps)

    by_name = {row.container_name: row for row in rows}
    assert set(by_name) == {"app", "proxy"}
    for row in rows:
        assert row.is_estimated is False
        assert row.cost_source == "opencost"
        assert row.usage_source == "prometheus"
        assert row.intensity_source == "consolidated"
        assert row.emaps_zone == "FR"
        assert is_cost_trusted(row) and is_usage_trusted(row)
    # CPU cost follows CPU usage, RAM cost follows memory usage.
    assert by_name["app"].cost_cpu == pytest.approx(0.375)
    assert by_name["app"].cost_ram == pytest.approx(0.075)
    assert by_name["proxy"].cost_ram == pytest.approx(0.225)
    assert sum(row.total_cost for row in rows) == pytest.approx(1.0)
    assert sum(row.cost_cpu or 0 for row in rows) == pytest.approx(0.5)
    assert by_name["app"].cpu_limit == 1000 and by_name["app"].memory_limit == 600
    assert by_name["proxy"].cpu_limit is None


def test_container_rows_from_metrics_server_are_flagged_as_such():
    maps = PodResourceMaps(
        container_cpu_usage_map={("shop", "web-0", "app"): 75, ("shop", "web-0", "proxy"): 25},
        container_timestamp_map={("shop", "web-0", "proxy"): TS + timedelta(seconds=30)},
    )

    rows = MetricAssembler.assemble_container_metrics([_aggregate()], _containers(), maps)

    by_name = {row.container_name: row for row in rows}
    assert by_name["app"].usage_source == "prometheus"
    assert by_name["proxy"].usage_source == "metrics_server"


def test_container_rows_without_cost_components_split_the_total_by_usage():
    maps = PodResourceMaps(container_cpu_usage_map={("shop", "web-0", "app"): 75, ("shop", "web-0", "proxy"): 25})
    aggregate = _aggregate(cost_cpu=None, cost_ram=None, cost_storage=None, cost_network=None, cost_lb=None)

    rows = MetricAssembler.assemble_container_metrics([aggregate], _containers(), maps)

    by_name = {row.container_name: row for row in rows}
    assert by_name["app"].total_cost == pytest.approx(0.75)
    assert by_name["app"].cost_cpu is None


def test_power_source_for_profile():
    estimator = MagicMock()
    estimator.DEFAULT_INSTANCE_PROFILE = {"vcores": 1}
    profile = {"vcores": 2}
    estimator.instance_profiles = {"m5.large": profile}

    assert power_source_for_profile(estimator, estimator.DEFAULT_INSTANCE_PROFILE) == "default_profile"
    assert power_source_for_profile(estimator, profile) == "instance_profile"
    assert power_source_for_profile(estimator, {"vcores": 8}) == "node_capacity_derived"


def test_default_cost_placeholder_is_not_tagged_as_a_cost_rate():
    """DEFAULT_COST is a flat placeholder, not a per core-hour rate: rows priced with it carry ``none``,
    the value migration 0039 backfills for legacy rows priced the same way."""
    fields = cost_fields(None, 1.0, default_cost=0.05)

    assert fields["total_cost"] == pytest.approx(0.05)
    assert fields["cost_source"] == "none"
    assert not is_cost_trusted(fields)
