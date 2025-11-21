# tests/integration/test_prometheus_k8s_integration.py

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from greenkube.collectors.prometheus_collector import PrometheusCollector
from greenkube.core.calculator import CarbonCalculator
from greenkube.core.config import config
from greenkube.core.processor import DataProcessor
from greenkube.models.prometheus_metrics import PrometheusMetric


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
        "metric": {
            "namespace": "default",
            "pod": "p1",
            "container": "c1",
            "node": "node-1",
        },
        "value": [
            int(sample_ts.replace("-", "").replace(":", "").split("T")[0]),
            "0.5",
        ],
    }

    prom_metric = PrometheusMetric()
    prom_metric.pod_cpu_usage = []
    prom_metric.node_instance_types = []

    # Patch the collector methods
    collector = PrometheusCollector(dummy_config)
    monkeypatch.setattr(
        collector,
        "_query_prometheus",
        lambda q: [pod_series] if "container_cpu_usage_seconds_total" in q else [],
    )

    # Provide a dummy NodeCollector-like object so tests don't require a live cluster
    class DummyNodeCollector:
        def collect_instance_types(self):
            return {"node-1": "m5.large"}

        def collect(self):
            return {"node-1": "gcp-us-east1-a"}

    node_collector = DummyNodeCollector()

    # Mock repository to return a known intensity for the normalized hour
    class DummyRepo:
        def get_for_zone_at_time(self, zone, timestamp):
            # We expect timestamp normalized to the hour
            assert timestamp.endswith("00:00+00:00")
            return 120.0

    repository = DummyRepo()

    # Mock calculator but use real CarbonCalculator to exercise normalization
    calc = CarbonCalculator(repository=repository, pue=config.DEFAULT_PUE)

    # Instead of using the full estimator pipeline, mock the estimator to return
    # a single EnergyMetric with a known joules value and timestamp so we can
    # assert final CombinedMetric values.
    from greenkube.models.metrics import EnergyMetric

    est = MagicMock()
    sample_dt = datetime.now(timezone.utc).replace(minute=23, second=12, microsecond=0)
    # Use 3.6e6 Joules == 1 kWh, simplifies math
    _energy_metric = EnergyMetric(
        pod_name="p1",
        namespace="default",
        joules=3.6e6,
        timestamp=sample_dt.isoformat(),
        node="node-1",
    )
    est.estimate.return_value = [_energy_metric]

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

    # Ensure a CombinedMetric was produced and contains expected intensity and co2e
    assert len(combined) >= 1, f"Expected at least 1 combined metric, got {len(combined)}"

    cm = combined[0]
    # grid_intensity should come from repository (120.0)
    assert cm.grid_intensity == 120.0
    # joules -> kWh = 1, * pue (DEFAULT_PUE) * intensity 120 = expected g CO2e
    expected_co2e = (3.6e6 / config.JOULES_PER_KWH) * config.DEFAULT_PUE * 120.0
    assert pytest.approx(cm.co2e_grams, rel=1e-6) == expected_co2e


def test_normalization_day_and_none(monkeypatch, dummy_config):
    """Test normalization granularity 'day' and 'none' behaviors."""
    from greenkube.core.config import config as core_config

    # Prepare a single energy metric
    from greenkube.models.metrics import EnergyMetric

    sample_dt = datetime.now(timezone.utc).replace(hour=10, minute=23, second=12, microsecond=0)
    _energy_metric = EnergyMetric(
        pod_name="p1",
        namespace="default",
        joules=3.6e6,
        timestamp=sample_dt.isoformat(),
        node="node-1",
    )

    class DummyRepoDay:
        def get_for_zone_at_time(self, zone, timestamp):
            # For 'day' we expect midnight normalization
            assert timestamp.endswith("00:00:00+00:00") or "T00:00:00" in timestamp
            return 200.0

    # Day granularity (use monkeypatch to avoid global state leakage)
    monkeypatch.setattr(core_config, "NORMALIZATION_GRANULARITY", "day")
    # Also ensure the config object referenced inside the calculator module
    # uses the same granularity (some modules may hold a reference).
    monkeypatch.setattr(
        "greenkube.core.calculator.config.NORMALIZATION_GRANULARITY",
        "day",
        raising=False,
    )
    calc_day = CarbonCalculator(repository=DummyRepoDay(), pue=config.DEFAULT_PUE)
    # Directly call calculate_emissions
    res = calc_day.calculate_emissions(3.6e6, "FR", sample_dt.isoformat())
    assert res.grid_intensity == 200.0

    # None granularity (no normalization) should call repository with exact timestamp
    class DummyRepoNone:
        def get_for_zone_at_time(self, zone, timestamp):
            # Expect same hour minute second as sample_dt
            assert timestamp.startswith(sample_dt.replace(tzinfo=timezone.utc).isoformat()[:13])
            return 250.0

    monkeypatch.setattr(core_config, "NORMALIZATION_GRANULARITY", "none")
    monkeypatch.setattr(
        "greenkube.core.calculator.config.NORMALIZATION_GRANULARITY",
        "none",
        raising=False,
    )
    calc_none = CarbonCalculator(repository=DummyRepoNone(), pue=config.DEFAULT_PUE)
    res2 = calc_none.calculate_emissions(3.6e6, "FR", sample_dt.isoformat())
    assert res2.grid_intensity == 250.0
