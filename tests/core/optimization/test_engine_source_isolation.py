"""Per-source isolation, reconciliation and run outcome of the optimization engine.

Regression tests for BUG-22 (E8): a failed or incomplete source used to block
persistence and reconciliation for every source. Also covers the run outcome
recorded for E10 (per-source health, counts and diff).
"""

import asyncio
from typing import List, Optional
from unittest.mock import AsyncMock

import pytest

from greenkube.core.config import Config, config, get_config
from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.engine import OptimizationEngine, ScanResult
from greenkube.core.optimization.providers.base import RecommendationSource
from greenkube.models.metrics import (
    Recommendation,
    RecommendationRecord,
    RecommendationType,
)
from greenkube.models.metrics import (
    RecommendationSource as SourceName,
)
from greenkube.models.optimization_run import (
    OptimizationRun,
    OptimizationRunStatus,
    OptimizationRunTrigger,
    SourceRunStatus,
)
from greenkube.storage.sqlite.optimization_run_repository import SQLiteOptimizationRunRepository
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository


class FakeSource(RecommendationSource):
    def __init__(
        self,
        name: str,
        recs: Optional[List[Recommendation]] = None,
        *,
        available: bool = True,
        error: Optional[Exception] = None,
        delay: float = 0.0,
        complete: Optional[bool] = None,
    ):
        self.name = name
        self._recs = recs or []
        self._available = available
        self._error = error
        self._delay = delay
        if complete is not None:
            self.last_collect_complete = complete

    async def is_available(self) -> bool:
        return self._available

    async def collect(self, context):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return list(self._recs)


def _rec(pod: str, *, source: str = "greenkube", cpu: int = 100, namespace: str = "prod") -> Recommendation:
    return Recommendation(
        pod_name=pod,
        namespace=namespace,
        type=RecommendationType.RIGHTSIZING_CPU,
        scope="pod",
        description=f"Reduce {pod}",
        current_cpu_request_millicores=500,
        recommended_cpu_request_millicores=cpu,
        potential_savings_cost=10.0,
        source=SourceName(source),
    )


def _engine(sources, run_repository=None, **kwargs) -> OptimizationEngine:
    engine = OptimizationEngine(sources=sources, run_repository=run_repository, **kwargs)
    engine.build_context = AsyncMock(return_value=OptimizationContext(config=get_config(), metrics=[]))
    return engine


@pytest.fixture
async def sqlite_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "engine.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield manager
    finally:
        await manager.close()


async def _seed(repo: SQLiteRecommendationRepository, *records: Recommendation) -> None:
    await repo.upsert_recommendations([RecommendationRecord.from_recommendation(record) for record in records])


async def _active(repo: SQLiteRecommendationRepository) -> dict:
    return {record.pod_name: record for record in await repo.get_active_recommendations()}


async def _stored(runs: SQLiteOptimizationRunRepository, run_id: Optional[int]) -> OptimizationRun:
    assert run_id is not None
    run = await runs.get(run_id)
    assert run is not None
    return run


def _run_id(result: ScanResult) -> Optional[int]:
    return result.run.id if result.run is not None else None


class TestBug22FailedSourceDoesNotBlockOthers:
    async def test_failed_source_does_not_block_persistence_of_others(self):
        healthy = _rec("healthy")
        engine = _engine([FakeSource("vpa", error=RuntimeError("connector down")), FakeSource("greenkube", [healthy])])
        reco_repo = AsyncMock()
        reco_repo.get_active_recommendations = AsyncMock(return_value=[])

        recommendations = await engine.refresh(AsyncMock(), AsyncMock(), reco_repo, namespace=None)

        assert [rec.pod_name for rec in recommendations] == ["healthy"]
        reco_repo.upsert_recommendations.assert_awaited()
        persisted = reco_repo.upsert_recommendations.await_args_list[0].args[0]
        assert [record.pod_name for record in persisted] == ["healthy"]
        reco_repo.reconcile_active_recommendations.assert_awaited_once()
        assert reco_repo.reconcile_active_recommendations.await_args.kwargs["incomplete_sources"] == ["vpa"]

    async def test_failed_source_recommendations_are_never_marked_stale(self, sqlite_db):
        repo = SQLiteRecommendationRepository(sqlite_db)
        await _seed(repo, _rec("vpa-advice", source="vpa"), _rec("native-gone"), _rec("native-kept"))
        engine = _engine(
            [FakeSource("vpa", error=RuntimeError("apiserver timeout")), FakeSource("greenkube", [_rec("native-kept")])]
        )

        await engine.refresh(AsyncMock(), AsyncMock(), repo)

        active = await _active(repo)
        assert set(active) == {"vpa-advice", "native-kept"}

    async def test_unavailable_source_keeps_its_previous_recommendations(self, sqlite_db):
        repo = SQLiteRecommendationRepository(sqlite_db)
        await _seed(repo, _rec("karpenter-pool", source="karpenter"))
        engine = _engine([FakeSource("karpenter", available=False), FakeSource("greenkube", [])])

        await engine.refresh(AsyncMock(), AsyncMock(), repo)

        assert set(await _active(repo)) == {"karpenter-pool"}

    async def test_completed_sources_still_resolve_their_recommendations(self, sqlite_db):
        repo = SQLiteRecommendationRepository(sqlite_db)
        await _seed(repo, _rec("vpa-advice", source="vpa"))
        engine = _engine([FakeSource("vpa", []), FakeSource("greenkube", [])])

        await engine.refresh(AsyncMock(), AsyncMock(), repo)

        assert await _active(repo) == {}

    async def test_incomplete_source_is_not_authoritative(self, sqlite_db):
        repo = SQLiteRecommendationRepository(sqlite_db)
        await _seed(repo, _rec("vpa-a", source="vpa"), _rec("vpa-b", source="vpa"))
        engine = _engine([FakeSource("vpa", [_rec("vpa-a", source="vpa")], complete=False)])

        result = await engine.run_scan(AsyncMock(), AsyncMock(), repo)

        assert set(await _active(repo)) == {"vpa-a", "vpa-b"}
        assert result.source_outcomes[0].status == SourceRunStatus.INCOMPLETE
        assert result.source_outcomes[0].recommendation_count == 1

    async def test_hung_source_times_out_without_blocking_others(self):
        engine = _engine(
            [FakeSource("vpa", [_rec("late", source="vpa")], delay=5), FakeSource("greenkube", [_rec("on-time")])],
            collect_timeout_seconds=0.05,
        )
        reco_repo = AsyncMock()
        reco_repo.get_active_recommendations = AsyncMock(return_value=[])

        result = await engine.run_scan(AsyncMock(), AsyncMock(), reco_repo)

        assert [rec.pod_name for rec in result.recommendations] == ["on-time"]
        assert result.source_outcomes[0].status == SourceRunStatus.FAILED
        assert result.source_outcomes[0].error == "timeout"
        assert result.status == OptimizationRunStatus.SUCCEEDED

    async def test_higher_priority_last_known_advice_survives_a_failed_source(self, sqlite_db):
        repo = SQLiteRecommendationRepository(sqlite_db)
        vpa_advice = _rec("api", source="vpa", cpu=300).model_copy(update={"sources": ["vpa", "greenkube"]})
        await _seed(repo, vpa_advice)
        engine = _engine(
            [FakeSource("vpa", error=RuntimeError("down")), FakeSource("greenkube", [_rec("api", cpu=150)])]
        )

        await engine.refresh(AsyncMock(), AsyncMock(), repo)

        kept = (await _active(repo))["api"]
        assert kept.source == SourceName.VPA
        assert kept.recommended_cpu_request_millicores == 300


class TestRunOutcome:
    async def test_run_records_source_health_counts_and_diff(self, sqlite_db):
        repo = SQLiteRecommendationRepository(sqlite_db)
        runs = SQLiteOptimizationRunRepository(sqlite_db)
        await _seed(repo, _rec("same", cpu=100), _rec("moved", cpu=100), _rec("gone"))
        engine = _engine(
            [
                FakeSource("vpa", available=False),
                FakeSource("greenkube", [_rec("same", cpu=100), _rec("moved", cpu=50), _rec("fresh")]),
            ],
            run_repository=runs,
        )

        result = await engine.run_scan(AsyncMock(), AsyncMock(), repo)

        run = await _stored(runs, _run_id(result))
        assert run.status == OptimizationRunStatus.SUCCEEDED
        assert run.trigger == OptimizationRunTrigger.DIRECT
        assert run.recommendation_count == 3
        assert (run.new_count, run.changed_count, run.resolved_count, run.unchanged_count) == (1, 1, 1, 1)
        assert [o.source for o in run.source_outcomes] == ["vpa", "greenkube"]
        assert [o.status for o in run.source_outcomes] == [SourceRunStatus.UNAVAILABLE, SourceRunStatus.SUCCEEDED]
        assert run.failed_analyzer_count == 0
        assert run.error is None
        assert run.diff is not None
        assert [fp.split(":")[3] for fp in run.diff.new] == ["fresh"]
        assert [fp.split(":")[3] for fp in run.diff.changed] == ["moved"]
        assert [fp.split(":")[3] for fp in run.diff.resolved] == ["gone"]
        records = {record.pod_name: record for record in await repo.get_recommendations_by_statuses(["stale"])}
        assert set(records) == {"gone"}

    async def test_partial_failure_is_a_degraded_success(self, sqlite_db):
        runs = SQLiteOptimizationRunRepository(sqlite_db)
        engine = _engine(
            [FakeSource("vpa", error=RuntimeError("down")), FakeSource("greenkube", [_rec("ok")])],
            run_repository=runs,
        )

        result = await engine.run_scan(AsyncMock(), AsyncMock(), SQLiteRecommendationRepository(sqlite_db))

        run = await _stored(runs, _run_id(result))
        assert run.status == OptimizationRunStatus.SUCCEEDED
        assert run.failed_analyzer_count == 1
        assert run.error == "Sources failed or incomplete: vpa"
        assert run.source_outcomes[0].error == "RuntimeError"

    async def test_all_attempted_sources_failing_fails_the_run(self, sqlite_db):
        runs = SQLiteOptimizationRunRepository(sqlite_db)
        engine = _engine(
            [FakeSource("vpa", error=RuntimeError("down")), FakeSource("karpenter", available=False)],
            run_repository=runs,
        )

        result = await engine.run_scan(AsyncMock(), AsyncMock(), SQLiteRecommendationRepository(sqlite_db))

        assert result.status == OptimizationRunStatus.FAILED
        assert (await _stored(runs, _run_id(result))).status == OptimizationRunStatus.FAILED

    async def test_persistence_error_fails_the_run_and_raises(self, sqlite_db):
        runs = SQLiteOptimizationRunRepository(sqlite_db)
        engine = _engine([FakeSource("greenkube", [_rec("a")])], run_repository=runs)
        reco_repo = AsyncMock()
        reco_repo.get_active_recommendations = AsyncMock(return_value=[])
        reco_repo.upsert_recommendations = AsyncMock(side_effect=ConnectionError("db down"))

        with pytest.raises(ConnectionError):
            await engine.run_scan(AsyncMock(), AsyncMock(), reco_repo)

        latest = await runs.latest()
        assert latest is not None
        assert latest.status == OptimizationRunStatus.FAILED
        assert latest.error == "ConnectionError"
        assert latest.completed_at is not None

    async def test_claimed_run_is_completed_instead_of_creating_a_new_one(self, sqlite_db):
        runs = SQLiteOptimizationRunRepository(sqlite_db)
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        queued, _ = await runs.enqueue(namespace="prod", trigger=OptimizationRunTrigger.MANUAL, requested_at=now)
        claimed = await runs.claim_next(claimed_by="controller", started_at=now)
        engine = _engine([FakeSource("greenkube", [_rec("a")])], run_repository=runs)

        result = await engine.run_scan(
            AsyncMock(), AsyncMock(), SQLiteRecommendationRepository(sqlite_db), namespace="prod", run=claimed
        )

        assert _run_id(result) == queued.id
        assert [run.id for run in await runs.list_runs()] == [queued.id]
        assert (await _stored(runs, queued.id)).status == OptimizationRunStatus.SUCCEEDED
