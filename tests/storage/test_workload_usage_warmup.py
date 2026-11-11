"""Warm-up exclusion of the ``workload_usage_hourly`` rollup on SQLite and PostgreSQL (USER BUG 2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from greenkube.core.config import Config
from greenkube.core.metrics_compressor import MetricsCompressor
from greenkube.core.workload_usage import WorkloadUsageRollup, floor_hour
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


async def test_hours_rerolled_from_compressed_tables_exclude_the_warmup(db, monkeypatch):  # noqa: F811
    """Once raw rows are pruned, a restart seen in the hourly tables still keeps its spike out."""
    start = floor_hour(datetime.now(timezone.utc) - timedelta(days=10))
    rows = []
    for step in range(24):
        ts = start + timedelta(minutes=5 * step)
        for pod, node in (("api-7d9f8b6c5d-abcde", "node-a"), ("api-7d9f8b6c5d-fghij", "node-a")):
            restarted = pod.endswith("abcde") and step >= 15
            spike = restarted and step in (15, 16)
            rows.append(
                _row(
                    ts,
                    pod_name=pod,
                    node=node,
                    restart_count=6 if restarted else 5,
                    memory_usage_bytes=(1100 if spike else 300) * MIB,
                )
            )
    await _metrics_repo(db).write_combined_metrics(rows)
    monkeypatch.setenv("METRICS_RAW_RETENTION_DAYS", "1")
    await MetricsCompressor(db, Config(), rollup_enabled=False).run(now=datetime.now(timezone.utc))

    repository = create_workload_usage_repository(db, db.db_type)
    second_hour = start + timedelta(hours=1)
    assert (await repository.read_hour_inputs(second_hour))[2] == "hourly"
    context = await repository.read_hourly_restart_context(second_hour)
    assert {(row.pod_name, row.restart_count, row.timestamp) for row in context} == {
        ("api-7d9f8b6c5d-abcde", 5, start),
        ("api-7d9f8b6c5d-fghij", 5, start),
    }

    await WorkloadUsageRollup(repository, warmup_seconds=600, backfill_hours=14 * 24).refresh(
        now=datetime.now(timezone.utc)
    )
    (hour,) = [
        item
        for item in await repository.read_hours(second_hour, second_hour + timedelta(hours=1))
        if not item.container_name
    ]
    assert hour.source == "hourly"
    assert hour.memory_usage_p99 == 300 * MIB
    assert hour.memory_usage_max == 1100 * MIB
