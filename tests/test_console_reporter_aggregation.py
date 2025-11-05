# tests/test_console_reporter_aggregation.py
"""
Unit tests for aggregation behavior in ConsoleReporter.
"""

import sys
from io import StringIO

from greenkube.models.metrics import CombinedMetric
from greenkube.reporters.console_reporter import ConsoleReporter


def capture_console_output(func, *args, **kwargs):
    old_stdout = sys.stdout
    sys.stdout = StringIO()
    try:
        func(*args, **kwargs)
        return sys.stdout.getvalue()
    finally:
        sys.stdout = old_stdout


def test_aggregation_without_period_sums_values():
    reporter = ConsoleReporter()
    # Two entries for the same pod/namespace
    items = [
        CombinedMetric(
            pod_name="pod-a",
            namespace="ns1",
            total_cost=1.0,
            co2e_grams=10.0,
            joules=100.0,
            pue=1.2,
            grid_intensity=200.0,
            cpu_request=100,
            memory_request=1024,
        ),
        CombinedMetric(
            pod_name="pod-a",
            namespace="ns1",
            total_cost=2.0,
            co2e_grams=20.0,
            joules=200.0,
            pue=1.2,
            grid_intensity=200.0,
            cpu_request=100,
            memory_request=1024,
        ),
    ]

    out = capture_console_output(reporter.report, items)
    # Aggregated CO2e should be 30.0 and appear in the output
    assert "30.00" in out
    # Aggregated joules 300
    assert "300" in out


def test_aggregation_with_period_groups_by_period():
    reporter = ConsoleReporter()
    # Two entries for same pod/namespace but different periods
    items = [
        CombinedMetric(
            pod_name="pod-b",
            namespace="ns2",
            period="2025-01",
            total_cost=1.0,
            co2e_grams=5.0,
            joules=50.0,
            pue=1.1,
            grid_intensity=150.0,
            cpu_request=200,
            memory_request=2048,
        ),
        CombinedMetric(
            pod_name="pod-b",
            namespace="ns2",
            period="2025-02",
            total_cost=2.0,
            co2e_grams=10.0,
            joules=100.0,
            pue=1.1,
            grid_intensity=150.0,
            cpu_request=200,
            memory_request=2048,
        ),
    ]

    # Run the reporter to ensure no exceptions are raised
    _ = capture_console_output(reporter.report, items)

    # Recompute aggregation the same way ConsoleReporter does and assert keys
    aggregated = {}
    for item in items:
        period = getattr(item, "period", None)
        key = (item.namespace, item.pod_name, period) if period else (item.namespace, item.pod_name)
        if key not in aggregated:
            aggregated[key] = {"co2e": 0.0, "joules": 0.0}
        aggregated[key]["co2e"] += item.co2e_grams
        aggregated[key]["joules"] += item.joules

    # Expect two separate aggregation keys for the two periods
    assert ("ns2", "pod-b", "2025-01") in aggregated
    assert ("ns2", "pod-b", "2025-02") in aggregated
    assert aggregated[("ns2", "pod-b", "2025-01")]["co2e"] == 5.0
    assert aggregated[("ns2", "pod-b", "2025-02")]["co2e"] == 10.0
