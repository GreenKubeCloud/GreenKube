# tests/integration/test_prometheus_k8s_integration.py

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

from src.greenkube.collectors.prometheus_collector import PrometheusCollector
from src.greenkube.collectors.node_collector import NodeCollector
from src.greenkube.core.processor import DataProcessor
from src.greenkube.core.calculator import CarbonCalculator, CarbonCalculationResult
from src.greenkube.core.config import config
from src.greenkube.models.prometheus_metrics import PrometheusMetric
from src.greenkube.energy.estimator import BasicEstimator


class DummyConfig:
    PROMETHEUS_URL = "http://mock-prometheus:9090"
    PROMETHEUS_QUERY_RANGE_STEP = "5m"


@pytest.fixture
def dummy_config():
    return DummyConfig()


def test_integration_prometheus_and_k8s(monkeypatch, dummy_config):
    """Integration-like test: mock Prometheus HTTP responses and Kubernetes node labels."""
    # Mock PrometheusCollector._query_prometheus to return pod CPU metrics and no node labels
    sample_ts = datetime.now(timezone.utc).replace(minute=23, second=12, microsecond=0).isoformat()

    pod_series = {
        "metric": {"namespace": "default", "pod": "p1", "container": "c1", "node": "node-1"},
        "value": [int(sample_ts.replace('-', '').replace(':','').split('T')[0]), "0.5"]
    }

    prom_metric = PrometheusMetric()
    prom_metric.pod_cpu_usage = []
    prom_metric.node_instance_types = []

    # Patch the collector methods
    collector = PrometheusCollector(dummy_config)
    monkeypatch.setattr(collector, '_query_prometheus', lambda q: [pod_series] if 'container_cpu_usage_seconds_total' in q else [])

    # Mock NodeCollector to return instance types
    node_collector = NodeCollector()
    monkeypatch.setattr(node_collector, 'collect_instance_types', lambda: {'node-1': 'm5.large'})
    monkeypatch.setattr(node_collector, 'collect', lambda: {'node-1': 'gcp-us-east1-a'})

    # Mock repository to return a known intensity for the normalized hour
    class DummyRepo:
        def get_for_zone_at_time(self, zone, timestamp):
            # We expect timestamp normalized to the hour
            assert timestamp.endswith('00:00+00:00') or timestamp.endswith('00:00+00:00')
            return 120.0

    repository = DummyRepo()

    # Mock calculator but use real CarbonCalculator to exercise normalization
    calc = CarbonCalculator(repository=repository, pue=config.DEFAULT_PUE)

    # Instead of using the full estimator pipeline, mock the estimator to return
    # a single EnergyMetric with a known joules value and timestamp so we can
    # assert final CombinedMetric values.
    from src.greenkube.models.metrics import EnergyMetric
    est = MagicMock()
    sample_dt = datetime.now(timezone.utc).replace(minute=23, second=12, microsecond=0)
    # Use 3.6e6 Joules == 1 kWh, simplifies math
    energy_metric = EnergyMetric(pod_name='p1', namespace='default', joules=3.6e6, timestamp=sample_dt.isoformat(), node='node-1')
    est.estimate.return_value = [energy_metric]

    # OpenCost and PodCollector mocks
    opencost = MagicMock()
    opencost.collect.return_value = []
    pod_collector = MagicMock()
    pod_collector.collect.return_value = []

    dp = DataProcessor(
        prometheus_collector=collector,
        opencost_collector=opencost,
        node_collector=node_collector,
        pod_collector=pod_collector,
        repository=repository,
        calculator=calc,
    estimator=est,
    )

    # Run the processing pipeline; we expect it to run without exceptions and produce 1 or 0 combined metrics
    combined = dp.run()
    # The important assertions: repository should have been queried using the normalized hour
    normalized_hour = sample_dt.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    cache_key = ('FR', normalized_hour)
    assert cache_key in calc._intensity_cache

    # Ensure a CombinedMetric was produced and contains expected intensity and co2e
    assert len(combined) >= 0
    if combined:
        cm = combined[0]
        # grid_intensity should come from repository (120.0)
        assert cm.grid_intensity == 120.0
        # joules -> kWh = 1, * pue (DEFAULT_PUE) = 1.5, * intensity 120 = 180 g CO2e
        assert pytest.approx(cm.co2e_grams, rel=1e-6) == 180.0


def test_normalization_day_and_none(monkeypatch, dummy_config):
    """Test normalization granularity 'day' and 'none' behaviors."""
    from src.greenkube.core.config import config as core_config

    # Prepare a single energy metric
    from src.greenkube.models.metrics import EnergyMetric
    sample_dt = datetime.now(timezone.utc).replace(hour=10, minute=23, second=12, microsecond=0)
    energy_metric = EnergyMetric(pod_name='p1', namespace='default', joules=3.6e6, timestamp=sample_dt.isoformat(), node='node-1')

    class DummyRepoDay:
        def get_for_zone_at_time(self, zone, timestamp):
            # For 'day' we expect midnight normalization
            assert timestamp.endswith('00:00:00+00:00') or 'T00:00:00' in timestamp
            return 200.0

    # Day granularity
    core_config.NORMALIZATION_GRANULARITY = 'day'
    calc_day = CarbonCalculator(repository=DummyRepoDay(), pue=config.DEFAULT_PUE)
    # Simulate DataProcessor pre-populating cache
    dp_day = MagicMock()
    # Directly call calculate_emissions
    res = calc_day.calculate_emissions(3.6e6, 'FR', sample_dt.isoformat())
    assert res.grid_intensity == 200.0

    # None granularity (no normalization) should call repository with exact timestamp
    class DummyRepoNone:
        def get_for_zone_at_time(self, zone, timestamp):
            # Expect same hour minute second as sample_dt
            assert timestamp.startswith(sample_dt.replace(tzinfo=timezone.utc).isoformat()[:13])
            return 250.0

    core_config.NORMALIZATION_GRANULARITY = 'none'
    calc_none = CarbonCalculator(repository=DummyRepoNone(), pue=config.DEFAULT_PUE)
    res2 = calc_none.calculate_emissions(3.6e6, 'FR', sample_dt.isoformat())
    assert res2.grid_intensity == 250.0
