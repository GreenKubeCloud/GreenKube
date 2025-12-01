# tests/energy/test_estimator.py
"""
Tests for the BasicEstimator.

These tests verify that the estimation engine correctly converts
Prometheus metrics into energy estimations (Joules) using
the instance profiles.
"""

import pytest

from greenkube.core.config import Config
from greenkube.data.instance_profiles import INSTANCE_PROFILES
from greenkube.energy.estimator import BasicEstimator
from greenkube.models.prometheus_metrics import (
    NodeInstanceType,
    PodCPUUsage,
    PrometheusMetric,
)


@pytest.fixture
def mock_config():
    """Fixture for a Config with a 5-minute step (300 seconds)."""
    config = Config()
    config.PROMETHEUS_QUERY_RANGE_STEP = "5m"
    return config


@pytest.fixture
def estimator(mock_config):
    """Fixture for the BasicEstimator."""
    return BasicEstimator(settings=mock_config)


@pytest.fixture
def sample_prometheus_metrics():
    """Simulated test data coming from the PrometheusCollector."""

    # Pod 1: 1 container on node-1 (m5.large)
    pod1_cpu = PodCPUUsage(
        namespace="prod",
        pod="api-pod",
        container="app",
        node="node-1",
        cpu_usage_cores=0.5,
    )

    # Pod 2: 2 containers on node-2 (t3.medium)
    pod2_cpu1 = PodCPUUsage(
        namespace="dev",
        pod="db-pod",
        container="db",
        node="node-2",
        cpu_usage_cores=1.0,
    )
    pod2_cpu2 = PodCPUUsage(
        namespace="dev",
        pod="db-pod",
        container="sidecar",
        node="node-2",
        cpu_usage_cores=0.2,
    )

    # Pod 3: 1 container on a node with an unknown instance type (will be skipped)
    pod3_cpu = PodCPUUsage(
        namespace="staging",
        pod="web-pod",
        container="web",
        node="node-3",
        cpu_usage_cores=0.1,
    )

    # Pod 4: 1 container on a node with no instance label (will be skipped)
    pod4_cpu = PodCPUUsage(
        namespace="staging",
        pod="cache-pod",
        container="cache",
        node="node-4",
        cpu_usage_cores=0.3,
    )

    # Node Info
    node1_type = NodeInstanceType(node="node-1", instance_type="m5.large")
    node2_type = NodeInstanceType(node="node-2", instance_type="t3.medium")
    node3_type = NodeInstanceType(node="node-3", instance_type="custom-type")  # Not in INSTANCE_PROFILES
    # node-4 is not in this list (simulates a node without the label)

    return PrometheusMetric(
        pod_cpu_usage=[pod1_cpu, pod2_cpu1, pod2_cpu2, pod3_cpu, pod4_cpu],
        node_instance_types=[node1_type, node2_type, node3_type],
    )


def test_estimator_calculates_energy_correctly(estimator, sample_prometheus_metrics):
    """
    Tests the "happy path": calculating energy for valid pods.
    """

    # Act
    energy_results = estimator.estimate(sample_prometheus_metrics)

    # Assert
    # Now estimator uses a DEFAULT_INSTANCE_PROFILE for unknown or missing instance types,
    # so all 4 pods should be processed.
    assert len(energy_results) == 4

    # 1. Check Pod 1 ("api-pod" on "m5.large")
    pod1_result = next(m for m in energy_results if m.pod_name == "api-pod")
    assert pod1_result.namespace == "prod"

    profile = INSTANCE_PROFILES["m5.large"]
    # New values based on AWS averages:
    # Min: 0.74 * 2 = 1.48
    # Max: 3.5 * 2 = 7.0

    cpu_cores = 0.5
    vcores = profile["vcores"]

    # Utilization = 0.5 / 2 = 0.25
    cpu_util = cpu_cores / vcores

    # Power = 1.48 + 0.25 * (7.0 - 1.48) = 1.48 + 1.38 = 2.86 Watts
    expected_power_watts = profile["minWatts"] + (cpu_util * (profile["maxWatts"] - profile["minWatts"]))
    assert expected_power_watts == pytest.approx(2.86)

    # Energy = 2.86 Watts * 300 Seconds = 858 Joules
    expected_joules = expected_power_watts * 300  # 300s = 5m
    assert pod1_result.joules == pytest.approx(expected_joules)
    assert pod1_result.node == "node-1"

    # 2. Check Pod 2 ("db-pod" on "t3.medium")
    pod2_result = next(m for m in energy_results if m.pod_name == "db-pod")
    assert pod2_result.namespace == "dev"

    profile2 = INSTANCE_PROFILES["t3.medium"]
    # New values based on AWS averages:
    # Min: 0.74 * 2 = 1.48
    # Max: 3.5 * 2 = 7.0

    # Aggregated CPU usage = 1.0 + 0.2 = 1.2 cores
    cpu_cores_2 = 1.2
    vcores_2 = profile2["vcores"]

    # Utilization = 1.2 / 2 = 0.6
    cpu_util_2 = cpu_cores_2 / vcores_2

    # Power = 1.48 + 0.6 * (7.0 - 1.48) = 1.48 + 3.312 = 4.792 Watts
    expected_power_watts_2 = profile2["minWatts"] + (cpu_util_2 * (profile2["maxWatts"] - profile2["minWatts"]))
    assert expected_power_watts_2 == pytest.approx(4.792)

    # Energy = 4.792 Watts * 300 Seconds = 1437.6 Joules
    expected_joules_2 = expected_power_watts_2 * 300
    assert pod2_result.joules == pytest.approx(expected_joules_2)
    assert pod2_result.node == "node-2"


def test_estimator_handles_missing_profiles_and_nodes(estimator):
    """
    Tests that the estimator skips pods on unknown nodes
    or unlisted instance types.
    """

    # Metric with a pod on a node whose instance type is unknown
    pod_unknown_instance = PodCPUUsage(
        namespace="test",
        pod="pod-1",
        container="c1",
        node="node-1",
        cpu_usage_cores=0.5,
    )
    node_unknown_instance = NodeInstanceType(node="node-1", instance_type="g1-small")  # g1-small is not in our profiles

    metrics_1 = PrometheusMetric(
        pod_cpu_usage=[pod_unknown_instance],
        node_instance_types=[node_unknown_instance],
    )

    # Metric with a pod on a node that has no instance label
    pod_no_label_node = PodCPUUsage(
        namespace="test",
        pod="pod-2",
        container="c1",
        node="node-2",
        cpu_usage_cores=0.5,
    )
    # Note: node_instance_types is empty for node-2

    metrics_2 = PrometheusMetric(pod_cpu_usage=[pod_no_label_node], node_instance_types=[])

    # Act & Assert
    # Estimator now falls back to the default instance profile when a profile is missing,
    # so both cases should produce a result.
    results_1 = estimator.estimate(metrics_1)
    assert len(results_1) == 1

    results_2 = estimator.estimate(metrics_2)
    assert len(results_2) == 1


def test_estimator_handles_cpu_utilization_over_100(estimator):
    """
    Tests that if CPU utilization goes over 100% (e.g., burstable),
    it is capped at 100% for the calculation.
    """

    # Pod using 3.0 cores on a 2-vcore instance (m5.large)
    pod_burst = PodCPUUsage(
        namespace="prod",
        pod="burst-pod",
        container="app",
        node="node-1",
        cpu_usage_cores=3.0,
    )
    node1_type = NodeInstanceType(node="node-1", instance_type="m5.large")

    metrics = PrometheusMetric(pod_cpu_usage=[pod_burst], node_instance_types=[node1_type])

    # Act
    energy_results = estimator.estimate(metrics)

    # Assert
    assert len(energy_results) == 1

    # The calculation must be capped at 100% utilization,
    # so the energy should equal maxWatts * time.
    profile = INSTANCE_PROFILES["m5.large"]

    # Power = Max Watts = 7.0 Watts (AWS Average Max 3.5 * 2 vCPUs)
    expected_power_watts = profile["maxWatts"]
    assert expected_power_watts == pytest.approx(7.0)

    # Energy = 7.0 Watts * 300 Seconds = 2100 Joules
    expected_joules = expected_power_watts * 300
    assert energy_results[0].joules == pytest.approx(expected_joules)
