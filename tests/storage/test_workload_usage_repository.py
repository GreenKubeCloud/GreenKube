"""``workload_usage_hourly`` rollup and SQL summaries on SQLite and PostgreSQL (roadmap E3, BUG-12).

The Python reference implementation (:mod:`greenkube.core.workload_usage`)
and the SQL repositories must agree: the rollup job stores what
``rollup_rows`` computes, and ``read_usage_snapshot`` returns what
``summarize`` computes from the stored hours.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.metrics_compressor import MetricsCompressor
from greenkube.core.workload_usage import WorkloadUsageRollup, floor_hour, rollup_rows, summarize
from greenkube.models.metrics import CombinedMetric
from greenkube.models.workload_usage import BaselineMetric, UsageSnapshot
from greenkube.storage.base_workload_usage_repository import create_workload_usage_repository
from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository
from greenkube.utils.time import to_iso_z

MIB = 1024 * 1024


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def db(request, tmp_path, monkeypatch):
    backend = request.param
    schema = None
    dsn = None
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL storage tests.")
        schema = f"gk_usage_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "usage.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield manager
    finally:
        await manager.close()
        if backend == "postgres" and dsn and schema:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


def _metrics_repo(manager: DatabaseManager):
    if manager.db_type == "postgres":
        return PostgresCombinedMetricsRepository(manager)
    return SQLiteCombinedMetricsRepository(manager)


def _base_hour() -> datetime:
    # Three days ago, hour aligned: always inside the default backfill window.
    return floor_hour(datetime.now(timezone.utc) - timedelta(days=3))


def _row(ts: datetime, **overrides: Any) -> CombinedMetric:
    values: dict[str, Any] = {
        "pod_name": "api-7d9f8b6c5d-abcde",
        "namespace": "shop",
        "owner_kind": "Deployment",
        "owner_name": "api",
        "timestamp": ts,
        "duration_seconds": 300,
        "node": "node-a",
        "node_instance_type": "m5.large",
        "cpu_request": 500,
        "memory_request": 512 * MIB,
        "cpu_usage_millicores": 100,
        "memory_usage_bytes": 200 * MIB,
        "total_cost": 0.01,
        "cost_cpu": 0.006,
        "cost_ram": 0.003,
        "cost_source": "opencost",
        "co2e_grams": 0.5,
        "joules": 1000.0,
        "pue": 1.2,
        "grid_intensity": 50.0,
        "emaps_zone": "FR",
    }
    values.update(overrides)
    return CombinedMetric(**values)


def _workload_rows(start: datetime, hours: int) -> list[CombinedMetric]:
    """Two replicas of ``api`` (one container ``web``), a bare pod, a CronJob, over ``hours`` hours."""
    rows: list[CombinedMetric] = []
    for hour in range(hours):
        for step in range(12):
            ts = start + timedelta(hours=hour, minutes=5 * step)
            usage = 100 + 40 * ((hour * 12 + step) % 7)
            for replica, node in (("api-7d9f8b6c5d-abcde", "node-a"), ("api-7d9f8b6c5d-fghij", "node-b")):
                rows.append(_row(ts, pod_name=replica, node=node, cpu_usage_millicores=usage))
                rows.append(
                    _row(
                        ts,
                        pod_name=replica,
                        node=None,
                        container_name="web",
                        cpu_usage_millicores=usage - 20,
                        cpu_request=400,
                        cpu_limit=1000,
                        memory_limit=1024 * MIB,
                        memory_usage_bytes=150 * MIB + step * MIB,
                    )
                )
            rows.append(
                _row(
                    ts,
                    pod_name="debug-shell",
                    owner_kind=None,
                    owner_name=None,
                    cpu_usage_millicores=5,
                    total_cost=0.002,
                    namespace="tools",
                )
            )
    return rows


async def _rolled(db, rows: list[CombinedMetric], start: datetime, hours: int):
    await _metrics_repo(db).write_combined_metrics(rows)
    repository = create_workload_usage_repository(db, db.db_type)
    result = await WorkloadUsageRollup(repository).refresh(now=start + timedelta(hours=hours, minutes=30))
    return repository, result


async def test_rollup_job_stores_what_the_reference_rollup_computes(db):
    start = _base_hour()
    rows = _workload_rows(start, hours=3)
    repository, result = await _rolled(db, rows, start, hours=3)

    stored = await repository.read_hours(start, start + timedelta(hours=3))
    expected = rollup_rows(
        [row for row in rows if not row.container_name],
        [row for row in rows if row.container_name],
    )

    assert result.rows == len(stored) == len(expected)

    def _key(item):
        return (item.hour_bucket, item.namespace, item.target_kind, item.target_name, item.container_name, item.node)

    by_key = {_key(item): item for item in expected}
    for item in stored:
        reference = by_key[_key(item)]
        assert item.sample_count == reference.sample_count
        assert item.step_count == reference.step_count
        assert item.pod_count == reference.pod_count
        assert item.cpu_usage_p95 == pytest.approx(reference.cpu_usage_p95)
        assert item.cpu_usage_avg == pytest.approx(reference.cpu_usage_avg)
        assert item.memory_usage_max == pytest.approx(reference.memory_usage_max)
        assert item.cpu_request == reference.cpu_request
        assert item.cpu_limit == reference.cpu_limit
        assert item.total_cost == pytest.approx(reference.total_cost, rel=1e-5)
        assert item.cost_cpu == pytest.approx(reference.cost_cpu, rel=1e-5)
        assert item.cost_source == "opencost"
        assert item.source == "raw"

    # Container rows inherit the pod's node; bare pods keep kind Pod.
    containers = [item for item in stored if item.container_name == "web"]
    assert {item.node for item in containers} == {"node-a", "node-b"}
    assert any(item.target == ("tools", "Pod", "debug-shell") for item in stored)


async def test_sql_snapshot_matches_the_reference_summary(db):
    start = _base_hour()
    rows = _workload_rows(start, hours=5)
    repository, _ = await _rolled(db, rows, start, hours=5)

    window = (start - timedelta(hours=1), start + timedelta(hours=6))
    snapshot = await repository.read_usage_snapshot(*window)
    reference = summarize(await repository.read_hours(*window))

    assert snapshot.source == "rollup"
    assert set(snapshot.summaries) == set(reference.summaries)
    for target, containers in reference.summaries.items():
        assert set(snapshot.summaries[target]) == set(containers)
        for container, expected in containers.items():
            actual = snapshot.summaries[target][container]
            assert actual.hours_observed == expected.hours_observed == 5
            assert actual.first_hour == expected.first_hour
            assert actual.last_hour == expected.last_hour
            assert actual.sample_count == expected.sample_count
            assert actual.step_count == expected.step_count
            assert actual.replicas_max == expected.replicas_max
            assert actual.cpu_request == expected.cpu_request
            assert actual.cpu_limit == expected.cpu_limit
            assert actual.total_cost == pytest.approx(expected.total_cost, rel=1e-5)
            assert actual.grid_intensity == pytest.approx(expected.grid_intensity)
            for resource in ("cpu", "memory"):
                got, want = getattr(actual, resource), getattr(expected, resource)
                assert got is not None and want is not None
                for name in ("avg", "p50", "p90", "p95", "p99", "max"):
                    assert getattr(got, name) == pytest.approx(getattr(want, name)), (target, container, name)
            assert actual.cpu is not None and expected.cpu is not None
            assert actual.cpu.stddev == pytest.approx(expected.cpu.stddev, abs=1e-6)

    api = snapshot.summaries[("shop", "Deployment", "api")]
    assert api[""].replicas_avg == pytest.approx(2.0)
    assert api["web"].cpu_limit == 1000
    assert _profiles_equal(snapshot, reference)
    assert set(snapshot.nodes) == {"node-a", "node-b"}
    for node, expected_node in reference.nodes.items():
        assert snapshot.nodes[node].cpu_usage_avg == pytest.approx(expected_node.cpu_usage_avg)
        assert snapshot.nodes[node].cpu_request_avg == pytest.approx(expected_node.cpu_request_avg)


async def test_sql_snapshot_derives_restarts_in_the_window_from_the_rollup(db):
    """REC-04: the rollup restart history is the fallback when the scan signals are unknown."""
    start = _base_hour()
    # Hourly restart counts 2, 5 (+3), 1 (new pod: +1), 1, and a target without any count.
    counts = [2, 5, 1, 1]
    # Restart counts are pod-level (combined_metrics): container rows carry none.
    rows = [
        _row(start + timedelta(hours=hour, minutes=5 * step), restart_count=counts[hour])
        for hour in range(4)
        for step in range(12)
    ]
    rows += [
        _row(start + timedelta(hours=hour, minutes=5 * step), container_name="web", node=None)
        for hour in range(4)
        for step in range(12)
    ]
    rows += [
        _row(
            start + timedelta(hours=hour, minutes=5 * step), pod_name="db-0", owner_kind="StatefulSet", owner_name="db"
        )
        for hour in range(4)
        for step in range(12)
    ]
    repository, _ = await _rolled(db, rows, start, hours=4)

    window = (start - timedelta(hours=1), start + timedelta(hours=5))
    snapshot = await repository.read_usage_snapshot(*window)
    reference = summarize(await repository.read_hours(*window))

    api = ("shop", "Deployment", "api")
    assert snapshot.summaries[api][""].restart_increase == reference.summaries[api][""].restart_increase == 4
    assert snapshot.summaries[api]["web"].restart_increase is None
    db_summary = snapshot.summaries[("shop", "StatefulSet", "db")][""]
    assert db_summary.restart_increase is None
    assert reference.summaries[("shop", "StatefulSet", "db")][""].restart_increase is None


def _profiles_equal(left: UsageSnapshot, right: UsageSnapshot) -> bool:
    for target, profile in right.hour_profiles.items():
        for got, want in zip(left.hour_profiles[target], profile):
            if (got is None) != (want is None):
                return False
            if got is not None and want is not None and abs(got - want) > 1e-6:
                return False
    return True


async def test_namespace_filter_is_applied_in_sql(db):
    start = _base_hour()
    repository, _ = await _rolled(db, _workload_rows(start, hours=2), start, hours=2)

    snapshot = await repository.read_usage_snapshot(start, start + timedelta(hours=2), namespace="tools")

    assert set(snapshot.summaries) == {("tools", "Pod", "debug-shell")}


async def test_baseline_rows_keep_current_requests_on_the_newest_rows(db):
    start = _base_hour()
    rows = _workload_rows(start, hours=2)
    # The request was cut in the third hour: the newest rows carry the new request.
    rows += [
        _row(start + timedelta(hours=2, minutes=5 * step), cpu_request=250, cpu_usage_millicores=90)
        for step in range(12)
    ]
    repository, _ = await _rolled(db, rows, start, hours=3)

    window = (start, start + timedelta(hours=3))
    snapshot = await repository.read_usage_snapshot(*window)
    pods, containers = await repository.read_baseline_rows(*window, snapshot=snapshot)

    assert all(isinstance(row, BaselineMetric) for row in (*pods, *containers))
    api_pods = [row for row in pods if row.owner_name == "api" and row.node == "node-a"]
    latest = max(row.timestamp for row in api_pods if row.timestamp is not None)
    assert {row.cpu_request for row in api_pods if row.timestamp == latest} == {250}
    assert snapshot.summaries[("shop", "Deployment", "api")][""].cpu_request == 250
    # Pod-level rows are split by UTC hour of day so schedules can be projected.
    assert {row.timestamp.hour for row in api_pods if row.timestamp is not None} == {
        (start + timedelta(hours=h)).hour for h in range(3)
    }
    # Baseline rows keep the totals of the hours they aggregate.
    assert sum(row.total_cost for row in pods if row.owner_name == "api") == pytest.approx(
        sum(r.total_cost for r in rows if r.owner_name == "api" and not r.container_name), rel=1e-4
    )
    web = [row for row in containers if row.container_name == "web"]
    assert web and all(row.cpu_usage_p95 is not None for row in web)


async def test_baseline_rows_carry_the_dcgm_gpu_power(db):
    """Bounded scans keep the GPU power (B5) the Impact Model prices on deletions and schedules."""
    start = _base_hour()
    rows = [
        _row(
            start + timedelta(minutes=5 * step),
            pod_name="trainer-0",
            owner_kind="StatefulSet",
            owner_name="trainer",
            gpu_power_watts=200.0 if step % 2 else 100.0,
        )
        for step in range(12)
    ]
    rows += _workload_rows(start, hours=1)
    repository, _ = await _rolled(db, rows, start, hours=1)

    window = (start, start + timedelta(hours=1))
    snapshot = await repository.read_usage_snapshot(*window)
    pods, _ = await repository.read_baseline_rows(*window, snapshot=snapshot)

    trainer = [row for row in pods if row.owner_name == "trainer"]
    assert trainer and all(row.gpu_power_watts == pytest.approx(150.0) for row in trainer)
    assert all(row.gpu_power_watts is None for row in pods if row.owner_name == "api")


async def test_rollup_falls_back_to_hourly_tables_after_raw_pruning(db, monkeypatch):
    start = floor_hour(datetime.now(timezone.utc) - timedelta(days=10))
    rows = _workload_rows(start, hours=2)
    await _metrics_repo(db).write_combined_metrics(rows)
    monkeypatch.setenv("METRICS_RAW_RETENTION_DAYS", "1")
    await MetricsCompressor(db, Config(), rollup_enabled=False).run(now=datetime.now(timezone.utc))

    repository = create_workload_usage_repository(db, db.db_type)
    pods, containers, source = await repository.read_hour_inputs(start)
    assert source == "hourly"
    assert pods and containers

    await WorkloadUsageRollup(repository, backfill_hours=14 * 24).refresh(now=datetime.now(timezone.utc))
    stored = await repository.read_hours(start, start + timedelta(hours=2))
    assert stored and {item.source for item in stored} == {"hourly"}
    api = [item for item in stored if item.target_name == "api" and item.container_name == ""]
    # An hourly row stands for its samples: 12 per replica and hour.
    assert sum(item.sample_count for item in api) == 2 * 2 * 12


async def test_refresh_rerolls_recent_hours_and_prunes(db):
    start = _base_hour()
    repository, _ = await _rolled(db, _workload_rows(start, hours=2), start, hours=2)
    before = await repository.read_hours(start, start + timedelta(hours=2))

    # A late row for the first hour is picked up by the next refresh.
    await _metrics_repo(db).write_combined_metrics(
        [_row(start + timedelta(minutes=2), pod_name="api-7d9f8b6c5d-zzzzz", node="node-c")]
    )
    await WorkloadUsageRollup(repository).refresh(now=start + timedelta(hours=2, minutes=30), refresh_hours=4)
    after = await repository.read_hours(start, start + timedelta(hours=2))
    assert len(after) == len(before) + 1

    assert await repository.prune_before(start + timedelta(hours=1)) > 0
    assert all(
        item.hour_bucket is not None and item.hour_bucket >= start + timedelta(hours=1)
        for item in await repository.read_hours(start, start + timedelta(days=1))
    )
    assert await repository.latest_rolled_hour() == start + timedelta(hours=1)


async def test_compression_rolls_hours_written_behind_the_rollup(db):
    """Catch-up after an outage (or ``start --last``) writes hours older than the refresh window."""
    now = datetime.now(timezone.utc)
    base = floor_hour(now)
    hole = range(30, 72)  # hours ago, all older than the 24h refresh window
    repo = _metrics_repo(db)
    await repo.write_combined_metrics([_row(base - timedelta(hours=h)) for h in range(96, 0, -1) if h not in hole])
    compressor = MetricsCompressor(db, Config())
    await compressor.refresh_usage_rollup(now)

    usage = create_workload_usage_repository(db, db.db_type)
    rolled = {item.hour_bucket for item in await usage.read_hours(base - timedelta(hours=100), base)}
    assert not any(base - timedelta(hours=h) in rolled for h in hole)

    await repo.write_combined_metrics([_row(base - timedelta(hours=h)) for h in hole])
    result = await compressor.refresh_usage_rollup(now + timedelta(minutes=10))

    stored = await usage.read_hours(base - timedelta(hours=100), base)
    pod_level = {item.hour_bucket for item in stored if item.container_name == ""}
    assert all(base - timedelta(hours=h) in pod_level for h in hole)
    assert result.late_hours == len(hole)
    # Only the late hours and the refresh window are re-rolled, not the whole gap since the oldest one.
    assert result.hours <= len(hole) + 25

    # Nothing new: the next run re-rolls the refresh window only.
    again = await compressor.refresh_usage_rollup(now + timedelta(minutes=20))
    assert again.late_hours == 0


async def test_rollup_marks_cover_container_rows_and_table_resets(db):
    start = _base_hour()
    repository, _ = await _rolled(db, _workload_rows(start, hours=1), start, hours=30)
    marks = await repository.read_rollup_marks()
    assert set(marks) == {"combined_metrics", "container_metrics"}
    assert marks == await repository.input_high_water_marks()
    assert all(value > 0 for value in marks.values())

    late = start - timedelta(hours=5)
    await _metrics_repo(db).write_combined_metrics(
        [_row(late + timedelta(minutes=5), pod_name="api-7d9f8b6c5d-abcde", node=None, container_name="web")]
    )
    upto = await repository.input_high_water_marks()
    assert upto["container_metrics"] > marks["container_metrics"]
    assert upto["combined_metrics"] == marks["combined_metrics"]
    assert await repository.hours_written_between(marks, upto) == [late]
    # Marks above the current ids (tables emptied and recreated) read from id 0.
    reset = {table: value + 10_000 for table, value in marks.items()}
    assert late in await repository.hours_written_between(reset, upto)


async def test_latest_request_comes_from_the_latest_sample_after_a_rollout_to_another_node(db):
    """A rollout that moves a pod to another node within the hour shows the new request at once.

    The hour holds one row per node: the replaced pod on node-a (500m until
    minute 10) and its successor on node-b (200m from minute 15). The latest
    request is the newest sample's (200m), not the hour's maximum (500m).
    """
    start = _base_hour()
    rows = [
        _row(start + timedelta(minutes=5 * step), pod_name="api-old", node="node-a", cpu_request=500)
        for step in range(3)
    ]
    rows += [
        _row(start + timedelta(minutes=5 * step), pod_name="api-new", node="node-b", cpu_request=200)
        for step in range(3, 12)
    ]
    rows += [
        _row(
            start + timedelta(minutes=5 * step),
            pod_name=pod,
            node=None,
            container_name="web",
            cpu_request=request,
            cpu_limit=limit,
        )
        for step in range(12)
        for pod, request, limit in [("api-old", 500, 1000) if step < 3 else ("api-new", 200, 400)]
    ]
    repository, _ = await _rolled(db, rows, start, hours=1)

    window = (start, start + timedelta(hours=1))
    snapshot = await repository.read_usage_snapshot(*window)
    reference = summarize(await repository.read_hours(*window))
    for summaries in (snapshot.summaries, reference.summaries):
        api = summaries[("shop", "Deployment", "api")]
        assert api[""].cpu_request == 200
        assert api["web"].cpu_request == 200
        assert api["web"].cpu_limit == 400


@pytest.mark.parametrize(
    ("second", "expected"),
    [("opencost_derived", "opencost_derived"), ("opencost", "opencost"), ("pricing_catalog", "mixed")],
)
async def test_cost_source_rollups_keep_opencost_and_derived_rows_trusted(db, second, expected):
    """An hour (and a baseline) mixing ``opencost`` and ``opencost_derived`` rows is an OpenCost cost, not ``mixed``."""
    start = _base_hour()
    rows = []
    for hour in range(2):
        for step in range(12):
            ts = start + timedelta(hours=hour, minutes=5 * step)
            # Hour 0 alternates labels within the hour; hour 1 uses only the second label.
            source = second if hour == 1 or step % 2 else "opencost"
            rows.append(_row(ts, cost_source=source))
            rows.append(_row(ts, container_name="web", cpu_request=400, cost_source=source))
    repository, _ = await _rolled(db, rows, start, hours=2)

    stored = await repository.read_hours(start, start + timedelta(hours=2))
    assert stored and {item.cost_source for item in stored if item.hour_bucket == start} == {expected}
    window = (start, start + timedelta(hours=2))
    snapshot = await repository.read_usage_snapshot(*window)
    _, containers = await repository.read_baseline_rows(*window, snapshot=snapshot)
    assert containers and {row.cost_source for row in containers} == {expected}


def _per_pod_rows(start: datetime, per_pod: dict[str, list[Any]]) -> list[CombinedMetric]:
    """Twelve 5-minute pod rows per hour for each pod, with the given hourly restart counts."""
    return [
        _row(start + timedelta(hours=hour, minutes=5 * step), pod_name=pod, restart_count=count)
        for pod, counts in per_pod.items()
        for hour, count in enumerate(counts)
        for step in range(12)
    ]


@pytest.mark.parametrize(
    ("per_pod", "expected"),
    [
        # One replica steady at 5 while another goes 0 -> 2 -> 4: 4 restarts, not 0 (max is no counter).
        ({"api-1": [5, 5, 5], "api-2": [0, 2, 4]}, 4),
        # A replaced pod whose count equals the old pod's: the new pod restarted 3 times.
        ({"api-1": [3, 3, None], "api-2": [None, None, 3]}, 3),
        # A pod recreated under the same name (StatefulSet): the reset adds the new count.
        ({"api-1": [4, 4, 1]}, 1),
        # Control: steady replicas never restarted in the window.
        ({"api-1": [0, 0, 0], "api-2": [3, 3, 3]}, 0),
    ],
    ids=["two-replicas", "replaced-pod", "reset", "control"],
)
async def test_restart_history_is_a_per_pod_counter_on_sql_and_python(db, per_pod, expected):
    """REC-04 review: the restart fallback sums per-pod increases, never the max across replicas."""
    start = _base_hour()
    hours = len(next(iter(per_pod.values())))
    repository, _ = await _rolled(db, _per_pod_rows(start, per_pod), start, hours=hours)

    window = (start - timedelta(hours=1), start + timedelta(hours=hours + 2))
    snapshot = await repository.read_usage_snapshot(*window)
    reference = summarize(await repository.read_hours(*window))

    api = ("shop", "Deployment", "api")
    assert snapshot.summaries[api][""].restart_increase == reference.summaries[api][""].restart_increase == expected


async def test_restart_history_skips_an_hour_without_count_alike_on_sql_and_python(db):
    """REC-04 review: an hour without restart count gives the same answer in SQL and in Python.

    Counts 2, unknown, 5: the pod's count before the 5 is unknown, so its whole
    count is restarts (fail closed, at least the 3 that really happened).
    """
    start = _base_hour()
    repository, _ = await _rolled(db, _per_pod_rows(start, {"api-1": [2, None, 5]}), start, hours=3)

    window = (start - timedelta(hours=1), start + timedelta(hours=5))
    snapshot = await repository.read_usage_snapshot(*window)
    reference = summarize(await repository.read_hours(*window))

    api = ("shop", "Deployment", "api")
    got = snapshot.summaries[api][""].restart_increase
    assert got == reference.summaries[api][""].restart_increase
    assert got is not None and got >= 3


async def test_hours_rolled_before_the_per_pod_increase_make_the_restart_history_unknown(db):
    """Rows rolled before migration 0130 carry a restart count without increase: unknown, never 0."""
    start = _base_hour()
    repository, _ = await _rolled(db, _per_pod_rows(start, {"api-1": [1, 1, 1]}), start, hours=3)
    table = "workload_usage_hourly"
    bucket = start if db.db_type == "postgres" else to_iso_z(start)
    async with db.connection_scope() as conn:
        if db.db_type == "postgres":
            await conn.execute(f"UPDATE {table} SET restart_increase = NULL WHERE hour_bucket = $1", bucket)
        else:
            await conn.execute(f"UPDATE {table} SET restart_increase = NULL WHERE hour_bucket = ?", (bucket,))
            await conn.commit()

    window = (start - timedelta(hours=1), start + timedelta(hours=5))
    snapshot = await repository.read_usage_snapshot(*window)
    reference = summarize(await repository.read_hours(*window))

    api = ("shop", "Deployment", "api")
    assert snapshot.summaries[api][""].restart_increase is None
    assert reference.summaries[api][""].restart_increase is None


def _timed_rows(start: datetime, samples: list[tuple[int, int, int]]) -> list[CombinedMetric]:
    """Pod rows of ``api-1`` at ``(hour, minute, restart_count)``."""
    return [
        _row(start + timedelta(hours=hour, minutes=minute), pod_name="api-1", restart_count=count)
        for hour, minute, count in samples
    ]


#: GreenKube down 0:50 -> 1:10 while the pod restarts twice (1 -> 3).
_BOUNDARY_GAP = (
    [(0, minute, 1) for minute in range(0, 50, 5)]
    + [(1, minute, 3) for minute in range(10, 60, 5)]
    + [(2, minute, 3) for minute in range(0, 60, 5)]
)
#: A whole hour missing while the pod restarts three times (1 -> 4).
_MISSING_HOUR = [(0, minute, 1) for minute in range(0, 60, 5)] + [(2, minute, 4) for minute in range(0, 60, 5)]


@pytest.mark.parametrize(
    ("samples", "expected"), [(_BOUNDARY_GAP, 2), (_MISSING_HOUR, 3)], ids=["boundary-gap", "missing-hour"]
)
async def test_restarts_across_a_collection_gap_are_counted_on_sql_and_python(db, samples, expected):
    """REC-04 review 2: a gap longer than the restart context never hides the restarts in it."""
    start = _base_hour()
    repository, _ = await _rolled(db, _timed_rows(start, samples), start, hours=3)

    window = (start - timedelta(hours=1), start + timedelta(hours=5))
    snapshot = await repository.read_usage_snapshot(*window)
    reference = summarize(await repository.read_hours(*window))

    api = ("shop", "Deployment", "api")
    assert snapshot.summaries[api][""].restart_increase == reference.summaries[api][""].restart_increase == expected


async def test_restarts_across_a_missing_hour_are_counted_from_the_hourly_tables(db, monkeypatch):
    """REC-04 review 2: the same on hours re-rolled from the compressed hourly tables."""
    start = floor_hour(datetime.now(timezone.utc) - timedelta(days=10))
    await _metrics_repo(db).write_combined_metrics(_timed_rows(start, _MISSING_HOUR))
    monkeypatch.setenv("METRICS_RAW_RETENTION_DAYS", "1")
    await MetricsCompressor(db, Config(), rollup_enabled=False).run(now=datetime.now(timezone.utc))
    repository = create_workload_usage_repository(db, db.db_type)
    assert (await repository.read_hour_inputs(start + timedelta(hours=2)))[2] == "hourly"

    await WorkloadUsageRollup(repository, backfill_hours=14 * 24).refresh(now=datetime.now(timezone.utc))

    window = (start - timedelta(hours=1), start + timedelta(hours=4))
    snapshot = await repository.read_usage_snapshot(*window)
    reference = summarize(await repository.read_hours(*window))
    api = ("shop", "Deployment", "api")
    assert snapshot.summaries[api][""].restart_increase == reference.summaries[api][""].restart_increase == 3


async def _null_increase(db, hour: datetime) -> None:
    """Make ``hour`` look rolled before migration 0130 (restart count, no increase)."""
    bucket = hour if db.db_type == "postgres" else to_iso_z(hour)
    async with db.connection_scope() as conn:
        if db.db_type == "postgres":
            await conn.execute(
                "UPDATE workload_usage_hourly SET restart_increase = NULL WHERE hour_bucket = $1", bucket
            )
        else:
            await conn.execute(
                "UPDATE workload_usage_hourly SET restart_increase = NULL WHERE hour_bucket = ?", (bucket,)
            )
            await conn.commit()


async def test_refresh_rerolls_hours_rolled_before_the_per_pod_increase(db):
    """REC-04 review 2: pre-0130 hours that stored rows can rebuild are re-rolled on upgrade."""
    start = _base_hour()
    repository, _ = await _rolled(db, _per_pod_rows(start, {"api-1": [1, 1, 2]}), start, hours=3)
    await _null_increase(db, start)
    window = (start - timedelta(hours=1), start + timedelta(hours=5))
    assert (await repository.read_usage_snapshot(*window)).summaries[("shop", "Deployment", "api")][
        ""
    ].restart_increase is None

    await WorkloadUsageRollup(repository).refresh(now=start + timedelta(hours=3, minutes=30), refresh_hours=1)

    summary = (await repository.read_usage_snapshot(*window)).summaries[("shop", "Deployment", "api")][""]
    assert summary.restart_increase == 1
    assert getattr(summary, "restart_increase_legacy", None) is False


async def test_pre_0130_hours_without_stored_rows_stay_unknown_and_are_flagged(db):
    start = _base_hour()
    repository, _ = await _rolled(db, _per_pod_rows(start, {"api-1": [1, 1, 2]}), start, hours=3)
    await _null_increase(db, start)
    async with db.connection_scope() as conn:
        if db.db_type == "postgres":
            await conn.execute("DELETE FROM combined_metrics WHERE timestamp < $1", start + timedelta(hours=1))
        else:
            await conn.execute(
                'DELETE FROM combined_metrics WHERE "timestamp" < ?', (to_iso_z(start + timedelta(hours=1)),)
            )
            await conn.commit()

    await WorkloadUsageRollup(repository).refresh(now=start + timedelta(hours=3, minutes=30), refresh_hours=1)

    window = (start - timedelta(hours=1), start + timedelta(hours=5))
    summary = (await repository.read_usage_snapshot(*window)).summaries[("shop", "Deployment", "api")][""]
    reference = summarize(await repository.read_hours(*window)).summaries[("shop", "Deployment", "api")][""]
    assert summary.restart_increase is None and reference.restart_increase is None
    assert getattr(summary, "restart_increase_legacy", None) is True
    assert getattr(reference, "restart_increase_legacy", None) is True
    # The rolled hour is kept, not deleted, when its inputs are gone.
    assert [
        item for item in await repository.read_hours(start, start + timedelta(hours=1)) if item.container_name == ""
    ]


async def test_legacy_reroll_from_hourly_tables_keeps_the_raw_usage(db, monkeypatch):
    """REC-04 review 3: a pre-0130 hour rebuilt from compressed rows only gains its restart increase."""
    now = datetime.now(timezone.utc)
    start = floor_hour(now - timedelta(days=10))
    rows = [
        _row(
            start + timedelta(hours=hour, minutes=minute),
            pod_name="api-1",
            restart_count=1 if hour < 1 else 3,
            cpu_usage_millicores=500 if minute == 30 else 100,
        )
        for hour in range(3)
        for minute in range(0, 60, 5)
    ]
    await _metrics_repo(db).write_combined_metrics(rows)
    repository = create_workload_usage_repository(db, db.db_type)
    await WorkloadUsageRollup(repository, backfill_hours=14 * 24).refresh(now=start + timedelta(hours=3, minutes=5))
    # The middle hour: the next refresh re-rolls the latest rolled hour anyway.
    target = start + timedelta(hours=1)

    def _pod_hour(hours):
        return next(item for item in hours if item.container_name == "" and item.hour_bucket == target)

    before = _pod_hour(await repository.read_hours(start, start + timedelta(hours=3)))
    assert before.source == "raw" and before.restart_increase == 2
    await _null_increase(db, target)
    monkeypatch.setenv("METRICS_RAW_RETENTION_DAYS", "1")
    await MetricsCompressor(db, Config(), rollup_enabled=False).run(now=now)
    assert (await repository.read_hour_inputs(target))[2] == "hourly"

    await WorkloadUsageRollup(repository, backfill_hours=14 * 24).refresh(now=now)

    after = _pod_hour(await repository.read_hours(start, start + timedelta(hours=3)))
    assert after.restart_increase == 2
    assert after.source == "raw"
    assert after.cpu_usage_p50 == pytest.approx(before.cpu_usage_p50)
    assert after.sample_count == before.sample_count


async def test_previous_restart_counts_are_looked_up_in_bounded_chunks(db, monkeypatch):
    """REC-04 review 3: pods without restart counts never trigger the lookup; pods are queried in chunks."""
    start = _base_hour()
    rows: list[CombinedMetric] = []
    for index in range(40):
        for hour in range(3):
            # Pods without restart count (kube-state-metrics not scraped) for the whole window.
            rows.append(_row(start + timedelta(hours=hour), pod_name=f"api-null-{index}", restart_count=None))
        # Pods that appear in hour 2 with a count: their previous count must be looked up.
        rows.append(_row(start + timedelta(hours=2), pod_name=f"api-new-{index}", restart_count=0))
    await _metrics_repo(db).write_combined_metrics(rows)
    repository = create_workload_usage_repository(db, db.db_type)

    lookups: list[list[tuple[str, str]]] = []
    original = repository.read_last_restart_counts

    async def spy(hour, pods):
        lookups.append(list(pods))
        return await original(hour, pods)

    queries: list[str] = []
    fetch = getattr(repository, "_fetch")

    async def counting_fetch(sql, params):
        queries.append(sql)
        return await fetch(sql, params)

    monkeypatch.setattr(repository, "read_last_restart_counts", spy)
    monkeypatch.setattr(repository, "_fetch", counting_fetch)
    await WorkloadUsageRollup(repository).refresh(now=start + timedelta(hours=3, minutes=30))

    looked_up = {pod for call in lookups for _namespace, pod in call}
    assert not any(pod.startswith("api-null-") for pod in looked_up)
    assert {f"api-new-{index}" for index in range(40)} <= looked_up
    # One query per table and chunk of pods, never one per pod.
    lookup_queries = [sql for sql in queries if "restart_count IS NOT NULL" in sql]
    assert len([sql for sql in lookup_queries if "pod_name IN" in sql or "ANY(" in sql]) <= 2 * len(lookups)
    assert len(lookup_queries) <= 2 * len(lookups) + 4


async def test_config_since_is_the_start_of_the_last_hour_with_other_requests_on_sql_and_python(db):
    """REC-02: since when the container runs its current memory request and limit (and any request)."""
    start = _base_hour()
    rows = []
    for hour in range(6):
        for step in range(12):
            ts = start + timedelta(hours=hour, minutes=5 * step)
            memory_limit = 512 * MIB if hour < 2 else 1024 * MIB
            cpu_request = 400 if hour < 4 else 300
            rows.append(_row(ts, pod_name="api-7d9f8b6c5d-abcde", node="node-a"))
            rows.append(
                _row(
                    ts,
                    pod_name="api-7d9f8b6c5d-abcde",
                    node=None,
                    container_name="web",
                    cpu_request=cpu_request,
                    memory_limit=memory_limit,
                )
            )
    repository, _ = await _rolled(db, rows, start, hours=6)

    window = (start, start + timedelta(hours=6))
    snapshot = await repository.read_usage_snapshot(*window)
    reference = summarize(await repository.read_hours(*window))
    for summaries in (snapshot.summaries, reference.summaries):
        web = summaries[("shop", "Deployment", "api")]["web"]
        assert web.memory_config_since == start + timedelta(hours=1)
        assert web.resources_config_since == start + timedelta(hours=3)


def _unit_prices(rows: list[BaselineMetric]) -> tuple[float, float]:
    """Reserved CPU and RAM unit prices the Impact Model derives from baseline rows."""
    from greenkube.impact.model import sample_prices
    from greenkube.impact.projector import sample_from_metric
    from greenkube.impact.types import NodeImpactProfile, UnitPrices, WorkloadRef
    from greenkube.models.provenance import CostSource

    # The node's CPU/RAM price ratio matches the observed split (2:1 per core-hour and GiB-hour).
    node = NodeImpactProfile(
        name="node-a",
        cpu_capacity_cores=4,
        mem_capacity_gib=16,
        idle_watts=10,
        max_watts=50,
        embodied_g_per_hour=0,
        prices=UnitPrices(0.02, 0.01, "USD", CostSource.PRICING_CATALOG),
        pue=1.2,
        grid_intensity_g_per_kwh=100,
    )
    ref = WorkloadRef("c", "shop", "Deployment", "api", "")
    cpu_cost = cpu_hours = ram_cost = ram_hours = 0.0
    for row in rows:
        sample = sample_from_metric(row, ref, 1.0)
        prices = sample_prices(sample, node, "USD")
        assert prices.observed
        cpu_cost += prices.cpu_reserved_core_hour * sample.cpu_reserved_cores * sample.hours
        cpu_hours += sample.cpu_reserved_cores * sample.hours
        ram_cost += prices.ram_reserved_gib_hour * sample.mem_reserved_gib * sample.hours
        ram_hours += sample.mem_reserved_gib * sample.hours
    return cpu_cost / cpu_hours, ram_cost / ram_hours


async def _mixed_split_baselines(db) -> tuple[list[BaselineMetric], list[BaselineMetric]]:
    """Pod baselines of one split hour alone, and of that hour plus an unsplit (0.3.x) hour the day before."""
    start = _base_hour() - timedelta(days=1)
    rows = []
    for day in range(2):
        for step in range(12):
            ts = start + timedelta(days=day, minutes=5 * step)
            if day == 0:
                rows.append(_row(ts, total_cost=0.009, cost_cpu=None, cost_ram=None))
            else:
                rows.append(_row(ts, total_cost=0.009, cost_cpu=0.006, cost_ram=0.003))
    repository, _ = await _rolled(db, rows, start, hours=25)

    split_window = (start + timedelta(days=1), start + timedelta(days=1, hours=1))
    snapshot = await repository.read_usage_snapshot(*split_window)
    split_pods, _ = await repository.read_baseline_rows(*split_window, snapshot=snapshot)
    mixed_window = (start, start + timedelta(days=1, hours=1))
    snapshot = await repository.read_usage_snapshot(*mixed_window)
    mixed_pods, _ = await repository.read_baseline_rows(*mixed_window, snapshot=snapshot)
    return split_pods, mixed_pods


async def test_baseline_mixing_split_and_unsplit_hours_keeps_the_split_unit_prices(db):
    """F-03: hours re-aggregated from 0.3.x rows carry only ``total_cost``.

    A lookback that mixes them with split hours must give the unit prices of
    the split hours alone (within 1 %), not dilute the CPU and RAM cost over
    every hour of the window.
    """
    split_pods, mixed_pods = await _mixed_split_baselines(db)

    split_cpu, split_ram = _unit_prices(split_pods)
    mixed_cpu, mixed_ram = _unit_prices(mixed_pods)
    # 0.006 USD per 5 minutes for 0.5 core, 0.003 USD for 0.5 GiB.
    assert split_cpu == pytest.approx(0.144, rel=1e-3)
    assert split_ram == pytest.approx(0.072, rel=1e-3)
    assert mixed_cpu == pytest.approx(split_cpu, rel=0.01)
    assert mixed_ram == pytest.approx(split_ram, rel=0.01)


async def test_node_observed_prices_from_mixed_baselines_keep_the_split_prices(db):
    """F-03, tier 1: per-node observed prices divide the split cost by the split hours only."""
    from greenkube.impact.projector import ImpactProjector
    from greenkube.models.node import NodeInfo

    split_pods, mixed_pods = await _mixed_split_baselines(db)
    nodes = [NodeInfo(name="node-a", cpu_capacity_cores=4, memory_capacity_bytes=16 * 1024 * MIB)]

    def _projector(rows):
        return ImpactProjector(Config(), node_infos=nodes, rows=rows)

    split, mixed = _projector(split_pods), _projector(mixed_pods)
    split_prices = split.pool.nodes["node-a"].prices
    mixed_prices = mixed.pool.nodes["node-a"].prices
    assert split_prices.source.value == "opencost"
    assert split_prices.cpu_core_hour == pytest.approx(0.144, rel=1e-3)
    assert mixed_prices.cpu_core_hour == pytest.approx(split_prices.cpu_core_hour, rel=0.01)
    assert mixed_prices.ram_gib_hour == pytest.approx(split_prices.ram_gib_hour, rel=0.01)
    split_hourly, mixed_hourly = split.node_hourly_price("node-a"), mixed.node_hourly_price("node-a")
    assert split_hourly is not None and mixed_hourly is not None
    assert mixed_hourly == pytest.approx(split_hourly, rel=0.01)
