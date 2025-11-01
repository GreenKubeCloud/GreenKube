# tests/energy/test_default_profile.py
import pytest
from greenkube.core.config import Config
from greenkube.energy.estimator import BasicEstimator
from greenkube.models.prometheus_metrics import PrometheusMetric, PodCPUUsage

@pytest.fixture
def mock_config():
    # Use the global config object and temporarily override defaults
    from greenkube.core.config import config as global_config
    old_vcores = global_config.DEFAULT_INSTANCE_VCORES
    old_min = global_config.DEFAULT_INSTANCE_MIN_WATTS
    old_max = global_config.DEFAULT_INSTANCE_MAX_WATTS
    try:
        global_config.DEFAULT_INSTANCE_VCORES = 2
        global_config.DEFAULT_INSTANCE_MIN_WATTS = 2.0
        global_config.DEFAULT_INSTANCE_MAX_WATTS = 12.0
        # Return the global_config as the settings object for the estimator
        yield global_config
    finally:
        global_config.DEFAULT_INSTANCE_VCORES = old_vcores
        global_config.DEFAULT_INSTANCE_MIN_WATTS = old_min
        global_config.DEFAULT_INSTANCE_MAX_WATTS = old_max

def test_default_profile_joules_used(mock_config):
    estimator = BasicEstimator(settings=mock_config)

    # Pod with 0.5 cores on node without instance label
    pod = PodCPUUsage(namespace='test', pod='p1', container='c', node='node-x', cpu_usage_cores=0.5)
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
    from greenkube.core.config import config as global_config
    old_vcores = global_config.DEFAULT_INSTANCE_VCORES
    old_min = global_config.DEFAULT_INSTANCE_MIN_WATTS
    old_max = global_config.DEFAULT_INSTANCE_MAX_WATTS

    try:
        global_config.DEFAULT_INSTANCE_VCORES = 1
        global_config.DEFAULT_INSTANCE_MIN_WATTS = 1.0
        global_config.DEFAULT_INSTANCE_MAX_WATTS = 5.0

        estimator = BasicEstimator(settings=global_config)
        pod = PodCPUUsage(namespace='test', pod='p2', container='c', node='node-y', cpu_usage_cores=0.5)
        metrics = PrometheusMetric(pod_cpu_usage=[pod], node_instance_types=[])

        results = estimator.estimate(metrics)
        assert len(results) == 1

        # With vcores=1 and cpu_usage_cores=0.5, utilization = 0.5, power = 1.0 + 0.5*(5.0-1.0)=3.0W
        expected_power = 1.0 + 0.5 * (5.0 - 1.0)
        expected_joules = expected_power * 300
        assert results[0].joules == pytest.approx(expected_joules)
    finally:
        global_config.DEFAULT_INSTANCE_VCORES = old_vcores
        global_config.DEFAULT_INSTANCE_MIN_WATTS = old_min
        global_config.DEFAULT_INSTANCE_MAX_WATTS = old_max
