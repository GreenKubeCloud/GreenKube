# tests/energy/test_default_profile.py
from unittest.mock import patch

import pytest

from greenkube.core.config import config as global_config
from greenkube.energy.estimator import BasicEstimator
from greenkube.models.prometheus_metrics import PodCPUUsage, PrometheusMetric


@pytest.fixture
def mock_config():
    # Use patch.object to safely override attributes on the global config object
    with (
        patch.object(global_config, "DEFAULT_INSTANCE_VCORES", 2),
        patch.object(global_config, "DEFAULT_INSTANCE_MIN_WATTS", 2.0),
        patch.object(global_config, "DEFAULT_INSTANCE_MAX_WATTS", 12.0),
    ):
        yield global_config


def test_default_profile_joules_used(mock_config):
    estimator = BasicEstimator(settings=mock_config)

    # Pod with 0.5 cores on node without instance label
    pod = PodCPUUsage(namespace="test", pod="p1", container="c", node="node-x", cpu_usage_cores=0.5)
    metrics = PrometheusMetric(pod_cpu_usage=[pod], node_instance_types=[])

    results = estimator.estimate(metrics)
    assert len(results) == 1

    # Using default profile vcores=2, min=2.0, max=12.0
    vcores = 2
    cpu_util = 0.5 / vcores
    expected_power = 2.0 + cpu_util * (12.0 - 2.0)  # 2.0 + 0.25 * 10 = 4.5 W
    expected_joules = expected_power * 300  # 5m

    assert results[0].joules == pytest.approx(expected_joules)


def test_configurable_defaults_affect_estimate():
    # Use patch.object as a context manager for this specific test
    with (
        patch.object(global_config, "DEFAULT_INSTANCE_VCORES", 1),
        patch.object(global_config, "DEFAULT_INSTANCE_MIN_WATTS", 1.0),
        patch.object(global_config, "DEFAULT_INSTANCE_MAX_WATTS", 5.0),
    ):
        estimator = BasicEstimator(settings=global_config)
        pod = PodCPUUsage(
            namespace="test",
            pod="p2",
            container="c",
            node="node-y",
            cpu_usage_cores=0.5,
        )
        metrics = PrometheusMetric(pod_cpu_usage=[pod], node_instance_types=[])

        results = estimator.estimate(metrics)
        assert len(results) == 1

        # With vcores=1 and cpu_usage_cores=0.5, utilization = 0.5, power = 1.0 + 0.5*(5.0-1.0)=3.0W
        expected_power = 1.0 + 0.5 * (5.0 - 1.0)
        expected_joules = expected_power * 300
        assert results[0].joules == pytest.approx(expected_joules)
