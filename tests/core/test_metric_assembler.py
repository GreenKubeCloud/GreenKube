# tests/core/test_metric_assembler.py
"""Tests for the MetricAssembler helpers used by the range collection path."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from greenkube.core.config import get_config
from greenkube.core.metric_assembler import MetricAssembler
from greenkube.core.prometheus_resource_mapper import PodResourceMaps
from greenkube.models.metrics import CombinedMetric, CostMetric, EnergyMetric, PodMetric
from greenkube.models.node import NodeZoneContext


def _energy(*, is_estimated: bool = False, reasons: list[str] | None = None) -> EnergyMetric:
    return EnergyMetric(
        pod_name="pod-a",
        namespace="ns-1",
        joules=1_000_000.0,
        timestamp=datetime.now(timezone.utc),
        node="node-1",
        is_estimated=is_estimated,
        estimation_reasons=reasons or [],
    )


def _known_provider() -> str:
    profiles = get_config().DATACENTER_PUE_PROFILES
    return next(key.removeprefix("default_") for key in profiles if key.startswith("default_"))


@pytest.fixture
def assembler() -> MetricAssembler:
    return MetricAssembler(
        calculator=MagicMock(),
        estimator=MagicMock(),
        repository=MagicMock(),
        electricity_provider=MagicMock(),
        zone_mapper=MagicMock(),
        embodied_service=MagicMock(),
    )


def _flags(assembler: MetricAssembler, **overrides):
    arguments = {
        "energy_metric": _energy(),
        "node_context": NodeZoneContext(node="node-1", emaps_zone="FR", is_estimated=False),
        "cost_metric": CostMetric(pod_name="pod-a", namespace="ns-1", cpu_cost=0.1, ram_cost=0.1, total_cost=0.2),
        "provider": _known_provider(),
        "pue": 1.2,
        "node_name": "node-1",
        "cpu_adjusted_nodes": set(),
    }
    arguments.update(overrides)
    return assembler.build_estimation_flags(**arguments)


class TestBuildEstimationFlags:
    def test_fully_resolved_row_is_not_estimated(self, assembler):
        assert _flags(assembler) == (False, [])

    def test_missing_node_context_names_the_default_zone(self, assembler):
        is_estimated, reasons = _flags(assembler, node_context=None, node_name="orphan-node")

        assert is_estimated is True
        assert any("orphan-node" in reason for reason in reasons)

    def test_missing_cost_is_estimated(self, assembler):
        is_estimated, reasons = _flags(assembler, cost_metric=None)

        assert is_estimated is True
        assert any("cost" in reason.lower() for reason in reasons)

    def test_negative_opencost_is_marked_untrusted(self, assembler):
        cost = CostMetric(pod_name="pod-a", namespace="ns-1", cpu_cost=-0.1, ram_cost=0.0, total_cost=-0.1)

        is_estimated, reasons = _flags(assembler, cost_metric=cost)

        assert is_estimated is True
        assert any("negative" in reason.lower() for reason in reasons)

    @pytest.mark.parametrize("provider", [None, "unknown-cloud"])
    def test_unknown_provider_uses_the_default_pue(self, assembler, provider):
        is_estimated, reasons = _flags(assembler, provider=provider)

        assert is_estimated is True
        assert any("PUE" in reason for reason in reasons)

    def test_energy_and_zone_reasons_are_kept_once(self, assembler):
        context = NodeZoneContext(node="node-1", emaps_zone="FR", is_estimated=True, estimation_reasons=["zone"])

        is_estimated, reasons = _flags(
            assembler,
            energy_metric=_energy(is_estimated=True, reasons=["zone", "profile"]),
            node_context=context,
        )

        assert is_estimated is True
        assert reasons == ["zone", "profile"]


def test_container_rows_keep_request_identity_and_apportion_pod_values():
    pod_metrics = [
        PodMetric(
            pod_name="pod-a",
            namespace="ns-1",
            container_name="web",
            cpu_request=500,
            memory_request=256 * 1024**2,
            owner_kind="Deployment",
            owner_name="api",
        ),
        PodMetric(
            pod_name="pod-a",
            namespace="ns-1",
            container_name="telemetry-sidecar",
            cpu_request=100,
            memory_request=64 * 1024**2,
            owner_kind="Deployment",
            owner_name="api",
        ),
    ]
    resource_maps = PodResourceMaps(
        container_cpu_usage_map={("ns-1", "pod-a", "web"): 20, ("ns-1", "pod-a", "telemetry-sidecar"): 10},
        container_memory_usage_map={
            ("ns-1", "pod-a", "web"): 120 * 1024**2,
            ("ns-1", "pod-a", "telemetry-sidecar"): 40 * 1024**2,
        },
    )
    aggregate = CombinedMetric(
        pod_name="pod-a",
        namespace="ns-1",
        node="node-1",
        timestamp=datetime.now(timezone.utc),
        total_cost=0.3,
        cost_cpu=0.1,
        cost_ram=0.2,
        co2e_grams=50.0,
        joules=1_000_000.0,
        cpu_request=600,
        memory_request=320 * 1024**2,
        cost_source="opencost",
        usage_source="prometheus",
    )

    result = MetricAssembler.assemble_container_metrics([aggregate], pod_metrics, resource_maps)

    assert {metric.container_name: metric.cpu_request for metric in result} == {"web": 500, "telemetry-sidecar": 100}
    assert {metric.container_name: metric.cpu_usage_millicores for metric in result} == {
        "web": 20,
        "telemetry-sidecar": 10,
    }
    # BUG-29: container rows inherit the pod's provenance instead of being blanket-estimated.
    assert all(metric.is_estimated == aggregate.is_estimated for metric in result)
    assert all(metric.cost_source == "opencost" for metric in result)
    assert all(metric.usage_source == "prometheus" for metric in result)
    assert sum(metric.total_cost for metric in result) == pytest.approx(aggregate.total_cost)
    assert sum(metric.co2e_grams for metric in result) == pytest.approx(aggregate.co2e_grams)
