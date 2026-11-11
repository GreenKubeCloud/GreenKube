"""Startup spikes after a container (re)start stay out of usage percentiles (USER BUG 2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from greenkube.core.workload_usage import rollup_rows, summarize_rows, warmup_windows
from greenkube.models.metrics import CombinedMetric

MIB = 1024 * 1024


def _hour() -> datetime:
    return (datetime.now(timezone.utc) - timedelta(days=1)).replace(minute=0, second=0, microsecond=0)


def _metric(ts: datetime, **overrides: Any) -> CombinedMetric:
    values: dict[str, Any] = {
        "pod_name": "api-7d9f8b6c5d-abcde",
        "namespace": "shop",
        "owner_kind": "Deployment",
        "owner_name": "api",
        "timestamp": ts,
        "duration_seconds": 300,
        "node": "node-a",
        "cpu_request": 500,
        "memory_request": 512 * MIB,
        "cpu_usage_millicores": 100,
        "memory_usage_bytes": 300 * MIB,
        "total_cost": 0.01,
    }
    values.update(overrides)
    return CombinedMetric(**values)


def _hour_with_restart(restart_at: int, spike: int) -> list[CombinedMetric]:
    """Twelve 5-minute steps; the restart is seen at ``restart_at`` and spikes for two steps."""
    hour = _hour()
    rows = []
    for i in range(12):
        memory = spike * MIB if restart_at <= i < restart_at + 2 else 300 * MIB
        rows.append(
            _metric(
                hour + timedelta(minutes=5 * i), memory_usage_bytes=memory, restart_count=4 if i >= restart_at else 3
            )
        )
    return rows


def test_restart_warmup_is_excluded_from_percentiles_but_not_from_the_maximum():
    rows = _hour_with_restart(restart_at=6, spike=1100)
    (plain,) = rollup_rows(rows)
    (warm,) = rollup_rows(rows, warmup_seconds=600)
    assert plain.memory_usage_p99 == 1100 * MIB
    assert warm.memory_usage_p99 == 300 * MIB and warm.memory_usage_p50 == 300 * MIB
    assert warm.memory_usage_max == 1100 * MIB
    # Averages and sample counts still cover every point.
    assert warm.memory_usage_avg == plain.memory_usage_avg
    assert warm.sample_count == plain.sample_count == 12


def test_a_restart_just_before_the_hour_is_seen_through_context_rows():
    hour = _hour()
    context = [_metric(hour - timedelta(minutes=5), restart_count=2)]
    rows = [
        _metric(hour + timedelta(minutes=5 * i), memory_usage_bytes=(900 if i == 0 else 300) * MIB, restart_count=3)
        for i in range(12)
    ]
    (without,) = rollup_rows(rows, warmup_seconds=600)
    (with_context,) = rollup_rows(rows, warmup_seconds=600, context_rows=context)
    assert without.memory_usage_p99 == 900 * MIB
    assert with_context.memory_usage_p99 == 300 * MIB
    assert with_context.memory_usage_max == 900 * MIB


def test_a_new_replica_starts_with_a_warmup():
    hour = _hour()
    rows = [_metric(hour + timedelta(minutes=5 * i), cpu_usage_millicores=100) for i in range(12)]
    rows += [
        _metric(hour + timedelta(minutes=5 * i), pod_name="api-7d9f8b6c5d-zzzzz", cpu_usage_millicores=900)
        for i in (6, 7)
    ]
    rows += [
        _metric(hour + timedelta(minutes=5 * i), pod_name="api-7d9f8b6c5d-zzzzz", cpu_usage_millicores=100)
        for i in range(8, 12)
    ]
    (row,) = rollup_rows(rows, warmup_seconds=600)
    # The new replica's first two steps (900m) are its warm-up.
    assert row.cpu_usage_p99 == 100
    assert row.cpu_usage_max == 900
    assert warmup_windows(rows, 600) == {
        ("shop", "api-7d9f8b6c5d-zzzzz"): [(hour + timedelta(minutes=30), hour + timedelta(minutes=40))]
    }


def test_pods_present_from_the_first_sample_are_not_new():
    rows = [_metric(_hour() + timedelta(minutes=5 * i)) for i in range(12)]
    assert warmup_windows(rows, 600) == {}
    assert warmup_windows(_hour_with_restart(restart_at=6, spike=1100), 0) == {}


def test_an_hour_that_is_all_warmup_keeps_its_percentiles():
    rows = _hour_with_restart(restart_at=0, spike=700)
    context = [_metric(_hour() - timedelta(minutes=5), restart_count=3)]
    (row,) = rollup_rows(rows, warmup_seconds=3600, context_rows=context)
    assert row.memory_usage_p99 == 700 * MIB


def _hourly(ts: datetime, pod: str, restarts: int, avg: int, peak: int) -> CombinedMetric:
    """A compressed hourly pod row: its hour's average, peak and highest restart count."""
    return _metric(
        ts,
        pod_name=pod,
        sample_count=12,
        restart_count=restarts,
        memory_usage_bytes=avg * MIB,
        memory_usage_max_bytes=peak * MIB,
    )


def test_hours_rerolled_from_compressed_tables_apply_the_warmup():
    hour = _hour()
    previous = [_hourly(hour - timedelta(hours=1), pod, 3, 300, 320) for pod in ("api-a", "api-b")]
    rows = [
        _hourly(hour, "api-a", 4, 380, 1100),  # restarted during the hour: replay spike
        _hourly(hour, "api-b", 3, 300, 320),
    ]
    (plain,) = rollup_rows(rows, source="hourly")
    assert plain.memory_usage_p99 == 1100 * MIB
    (warm,) = rollup_rows(rows, warmup_seconds=600, source="hourly", context_rows=previous)
    # The restarted replica's hour leaves the percentiles; its peak stays the maximum.
    assert warm.memory_usage_p99 == 320 * MIB
    assert warm.memory_usage_max == 1100 * MIB


def test_a_single_replica_warm_hour_counts_with_its_average():
    hour = _hour()
    previous = [_hourly(hour - timedelta(hours=1), "api-a", 3, 300, 320)]
    (row,) = rollup_rows(
        [_hourly(hour, "api-a", 4, 380, 1100)], warmup_seconds=600, source="hourly", context_rows=previous
    )
    assert row.memory_usage_p99 == 380 * MIB
    assert row.memory_usage_max == 1100 * MIB


def test_a_replica_new_in_a_compressed_hour_warms_up():
    hour = _hour()
    previous = [_hourly(hour - timedelta(hours=1), "api-a", 3, 300, 320)]
    rows = [_hourly(hour, "api-a", 3, 300, 320), _hourly(hour, "api-new", 0, 400, 900)]
    (row,) = rollup_rows(rows, warmup_seconds=600, source="hourly", context_rows=previous)
    assert row.memory_usage_p99 == 320 * MIB and row.memory_usage_max == 900 * MIB
    # Without the previous hour, nothing is known to be new or restarted.
    (alone,) = rollup_rows(rows, warmup_seconds=600, source="hourly")
    assert alone.memory_usage_p99 == 900 * MIB


def test_summaries_from_rows_apply_the_warmup():
    rows = _hour_with_restart(restart_at=6, spike=1100)
    snapshot = summarize_rows(rows, warmup_seconds=600)
    (summary,) = snapshot.summaries[("shop", "Deployment", "api")].values()
    assert summary.memory is not None
    assert summary.memory.p99 == 300 * MIB and summary.memory.max == 1100 * MIB
