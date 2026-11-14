"""A missing or NaN OpenCost cost is missing, never a trusted zero (production readiness, A11/C0.3).

Before the fix ``_number`` turned ``None``/NaN/inf into ``0.0`` and the row
was stamped ``cost_source = opencost``: an hour of NaN cost after a change
looked like the whole allocated cost saved.
"""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.collectors.opencost_collector import _allocation_to_cost_metric, untrusted_cost_reason
from greenkube.core.metric_assembler import cost_fields
from greenkube.models.provenance import is_cost_trusted
from greenkube.verify.model import TimeWindow
from greenkube.verify.series import hourly_buckets, quantity_series
from tests.verify.fixtures import workload_rows  # pyrefly: ignore[missing-import]

NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _allocation(**values):
    item = {
        "properties": {"namespace": "prod", "pod": "api-a"},
        "cpuCost": 0.08,
        "ramCost": 0.02,
        "totalCost": 0.10,
        "minutes": 60,
        "cpuCoreRequestAverage": 1.0,
        "ramByteRequestAverage": 2 * 1024**3,
    }
    item.update(values)
    return item


def _row(item):
    metric = _allocation_to_cost_metric("prod/api-a", item, NOW)
    assert metric is not None
    return {**cost_fields(metric, 1.0, 0.0), "cpu_request": 1000, "memory_request": 2 * 1024**3}


def test_a_complete_allocation_is_trusted() -> None:
    row = _row(_allocation())
    assert row["cost_source"] == "opencost" and row["total_cost"] == pytest.approx(0.10)
    assert is_cost_trusted(row)


@pytest.mark.parametrize(
    "values",
    [
        {"totalCost": float("nan")},
        {"totalCost": "NaN"},
        {"cpuCost": float("nan")},
        {"ramCost": float("inf")},
        {"totalCost": None, "cpuCost": None},
        {"totalCost": 0.0, "cpuCost": 0.0, "ramCost": 0.0},
    ],
)
def test_a_missing_nan_or_zero_cost_of_a_running_pod_is_not_trusted(values) -> None:
    item = _allocation(**values)
    assert untrusted_cost_reason(item) is not None
    row = _row(item)
    assert row["cost_source"] == "none"
    assert row["cost_cpu"] is None and row["cost_ram"] is None
    assert not is_cost_trusted(row)


def test_a_zero_cost_of_a_pod_without_requests_or_runtime_stays_trusted() -> None:
    assert untrusted_cost_reason(_allocation(totalCost=0.0, cpuCost=0.0, ramCost=0.0, minutes=0)) is None
    no_requests = _allocation(
        totalCost=0.0, cpuCost=0.0, ramCost=0.0, cpuCoreRequestAverage=0.0, ramByteRequestAverage=0.0
    )
    assert untrusted_cost_reason(no_requests) is None


def test_a_stored_zero_total_with_requests_is_not_trusted() -> None:
    assert not is_cost_trusted({"cost_source": "opencost", "total_cost": 0.0, "cpu_request": 500})
    assert is_cost_trusted({"cost_source": "opencost", "total_cost": 0.0, "cpu_request": 0, "memory_request": 0})
    assert is_cost_trusted({"cost_source": "opencost", "total_cost": 0.12, "cpu_request": 500})


def test_nan_opencost_hours_are_not_a_saving() -> None:
    """Two pods at $0.10/h each; the post-change hours have NaN OpenCost cost: no trusted cost, no saving."""
    window = TimeWindow(start=NOW - timedelta(hours=4), end=NOW)
    rows = workload_rows(window, cost=0.10)
    nan_item = _allocation(totalCost=float("nan"), cpuCost=float("nan"), ramCost=float("nan"))
    nan_fields = cost_fields(_allocation_to_cost_metric("prod/api-a", nan_item, NOW), 1.0, 0.0)
    post = [row.model_copy(update=nan_fields) for row in rows[4:]]
    pre = [row.model_copy(update={"cost_source": "opencost"}) for row in rows[:4]]
    buckets = hourly_buckets(pre + post, window, 300)
    series = quantity_series(buckets, "allocated_cost")
    # Only the two trusted hours count; the NaN hours are missing, not $0.
    assert sorted(series.values()) == [pytest.approx(0.20), pytest.approx(0.20)]
