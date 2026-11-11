"""Warm-up exclusion of the ``workload_usage_hourly`` rollup on SQLite and PostgreSQL (USER BUG 2)."""

from __future__ import annotations

from datetime import timedelta

from greenkube.core.workload_usage import WorkloadUsageRollup
from greenkube.storage.base_workload_usage_repository import create_workload_usage_repository
from tests.storage.test_workload_usage_repository import (  # pyrefly: ignore[missing-import]
    MIB,
    _base_hour,
    _metrics_repo,
    _row,
    db,  # noqa: F401 - pytest fixture
)


async def test_a_restart_at_the_end_of_an_hour_excludes_the_next_hours_first_points(db):  # noqa: F811
    start = _base_hour()
    rows = []
    for step in range(24):
        ts = start + timedelta(minutes=5 * step)
        # The restart is seen at the hour's last step; the replay spike spans the hour boundary.
        rows.append(
            _row(ts, restart_count=6 if step >= 11 else 5, memory_usage_bytes=(1100 if step in (11, 12) else 300) * MIB)
        )
    await _metrics_repo(db).write_combined_metrics(rows)
    repository = create_workload_usage_repository(db, db.db_type)

    second_hour = start + timedelta(hours=1)
    context = await repository.read_restart_context(second_hour, 900)
    assert ("api-7d9f8b6c5d-abcde", 6) in {(row.pod_name, row.restart_count) for row in context}
    assert all(row.timestamp is not None and row.timestamp < second_hour for row in context)
    assert await repository.read_restart_context(start, 900) == []

    await WorkloadUsageRollup(repository, warmup_seconds=600).refresh(now=start + timedelta(hours=2, minutes=30))
    hours = sorted(
        (item for item in await repository.read_hours(start, start + timedelta(hours=2)) if not item.container_name),
        key=lambda item: item.hour_bucket,
    )
    assert [item.memory_usage_p99 for item in hours] == [300 * MIB, 300 * MIB]
    assert [item.memory_usage_max for item in hours] == [1100 * MIB, 1100 * MIB]


async def test_without_warmup_the_spike_drives_the_percentiles(db):  # noqa: F811
    start = _base_hour()
    rows = [
        _row(
            start + timedelta(minutes=5 * step), restart_count=5, memory_usage_bytes=(1100 if step == 11 else 300) * MIB
        )
        for step in range(12)
    ]
    await _metrics_repo(db).write_combined_metrics(rows)
    repository = create_workload_usage_repository(db, db.db_type)
    await WorkloadUsageRollup(repository).refresh(now=start + timedelta(hours=1, minutes=30))
    (hour,) = [
        item for item in await repository.read_hours(start, start + timedelta(hours=1)) if not item.container_name
    ]
    assert hour.memory_usage_p99 == 1100 * MIB
