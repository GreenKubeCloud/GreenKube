# tests/test_aggregator.py
from datetime import datetime, timezone

import pytest

from greenkube.core.aggregator import aggregate_metrics
from greenkube.models.metrics import CombinedMetric


def test_aggregate_metrics_daily():
    """Verify that metrics are correctly aggregated on a daily basis."""
    metrics = [
        CombinedMetric(
            pod_name="pod-1",
            namespace="ns-1",
            timestamp=datetime(2023, 10, 27, 10, 0, 0, tzinfo=timezone.utc),
            duration_seconds=300,
            joules=100,
            co2e_grams=10,
            total_cost=1,
            grid_intensity=100,
            pue=1.5,
            cpu_request=500,
            memory_request=1024,
            embodied_co2e_grams=5,
        ),
        CombinedMetric(
            pod_name="pod-1",
            namespace="ns-1",
            timestamp=datetime(2023, 10, 27, 11, 0, 0, tzinfo=timezone.utc),
            duration_seconds=300,
            joules=200,
            co2e_grams=20,
            total_cost=2,
            grid_intensity=120,
            pue=1.6,
            cpu_request=500,
            memory_request=1024,
            embodied_co2e_grams=7,
        ),
        # Metric for another pod, should not be aggregated with the first two
        CombinedMetric(
            pod_name="pod-2",
            namespace="ns-1",
            timestamp=datetime(2023, 10, 27, 10, 0, 0, tzinfo=timezone.utc),
            duration_seconds=300,
            joules=50,
            co2e_grams=5,
            total_cost=0.5,
            grid_intensity=100,
            pue=1.5,
        ),
    ]

    # Aggregate daily
    result = aggregate_metrics(metrics, daily=True)

    # We expect two results: one for pod-1, one for pod-2
    assert len(result) == 2

    # Find the aggregated metric for pod-1
    agg_pod1 = next((m for m in result if m.pod_name == "pod-1"), None)
    assert agg_pod1 is not None

    # Verify summed values
    assert agg_pod1.joules == 300  # 100 + 200
    assert agg_pod1.co2e_grams == 30  # 10 + 20
    assert agg_pod1.embodied_co2e_grams == 12  # 5 + 7
    assert agg_pod1.total_cost == 3  # 1 + 2
    assert agg_pod1.duration_seconds == 600  # 300 + 300

    # Verify time-weighted average for grid intensity (equal duration)
    assert agg_pod1.grid_intensity == pytest.approx(110.0)  # (100*300 + 120*300) / 600

    # Verify max value for requests
    assert agg_pod1.cpu_request == 500
    assert agg_pod1.memory_request == 1024

    # Verify period string
    assert agg_pod1.period == "2023-10-27"
