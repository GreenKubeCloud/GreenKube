"""The engine runs Source SDK sources and records their runs and signals (roadmap E4, E5, E8)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import ClassVar, FrozenSet, List
from unittest.mock import AsyncMock

import pytest

from greenkube.core.config import Config, config, get_config
from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.engine import OptimizationEngine
from greenkube.models.metrics import Recommendation, RecommendationRecord, RecommendationType
from greenkube.models.optimization_run import SourceRunStatus as V1Status
from greenkube.signals.adapters import analyzer_source_class
from greenkube.signals.model import AlgorithmInfo, Capability, DeletionProposal, SourceSignal, TargetRef
from greenkube.signals.runtime import SourceRunRecord, SourceRunStatus
from greenkube.signals.sdk import Source, SourceBatch, SourceDeps, SourceSettings
from greenkube.storage.base_signal_repository import create_signal_repository
from greenkube.storage.sqlite.optimization_run_repository import SQLiteOptimizationRunRepository
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository


def _rec(pod: str, rec_type: RecommendationType = RecommendationType.ORPHANED_PERSISTENT_VOLUME) -> Recommendation:
    return Recommendation(
        pod_name=pod,
        namespace="prod",
        type=rec_type,
        scope="workload",
        owner_kind="Deployment",
        owner_name=pod,
        description=f"{pod} is idle",
        potential_savings_cost=10.0,
    )


class GoodAnalyzer(Analyzer):
    """Emits one orphaned-volume recommendation."""

    capability = "storage_cleanup"

    def analyze(self, context):
        return [_rec("alive")]


class BrokenAnalyzer(Analyzer):
    """Always fails."""

    capability = "lb_cleanup"

    def analyze(self, context):
        raise RuntimeError("analyzer bug")


def _engine(sources, **kwargs) -> OptimizationEngine:
    engine = OptimizationEngine(sources=sources, **kwargs)
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


async def _seed(repo, *recs: Recommendation) -> None:
    await repo.upsert_recommendations([RecommendationRecord.from_recommendation(rec) for rec in recs])


async def _active(repo) -> set:
    return {record.pod_name for record in await repo.get_active_recommendations()}


class TestFailedAnalyzerNeverStalesNativeAdvice:
    """E8: one failing native analyzer makes the v1 source ``greenkube`` incomplete for the run."""

    async def test_sdk_native_sources_make_the_greenkube_group_incomplete(self, sqlite_db):
        repo = SQLiteRecommendationRepository(sqlite_db)
        await _seed(repo, _rec("alive"), _rec("lb-user", RecommendationType.ORPHANED_LOAD_BALANCER))
        deps = SourceDeps(config=get_config(), cluster_uid="c1")
        sources = [analyzer_source_class(cls)(SourceSettings(), deps) for cls in (GoodAnalyzer, BrokenAnalyzer)]

        result = await _engine(sources).run_scan(AsyncMock(), AsyncMock(), repo)

        [outcome] = result.source_outcomes
        assert outcome.source == "greenkube"
        assert outcome.status == V1Status.INCOMPLETE
        assert outcome.error == "greenkube.broken: RuntimeError"
        assert await _active(repo) == {"alive", "lb-user"}
        assert [r.run.status for r in result.source_reports] == [SourceRunStatus.SUCCEEDED, SourceRunStatus.FAILED]


class PluginSource(Source):
    id: ClassVar[str] = "acme"
    display_name: ClassVar[str] = "Acme"
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.CLEANUP})

    async def collect(self, ctx):
        signal = SourceSignal(
            source_id="acme",
            target=TargetRef.for_kind(ctx.cluster_uid, "PersistentVolume", "pv-9"),
            capability=Capability.CLEANUP,
            kind="ORPHANED_PERSISTENT_VOLUME",
            proposal=DeletionProposal(reason="Released"),
            algorithm=AlgorithmInfo(name="acme.cleanup"),
        )
        return SourceBatch(signals=[signal])


class TestSourceRunPersistence:
    async def test_runs_and_signals_are_recorded_and_linked(self, sqlite_db):
        repo = SQLiteRecommendationRepository(sqlite_db)
        runs = SQLiteOptimizationRunRepository(sqlite_db)
        signals = create_signal_repository(sqlite_db, "sqlite")
        deps = SourceDeps(config=get_config(), cluster_uid="c1")
        engine = _engine(
            [analyzer_source_class(GoodAnalyzer)(SourceSettings(), deps), PluginSource(SourceSettings(), deps)],
            run_repository=runs,
            signal_repository=signals,
        )

        result = await engine.run_scan(AsyncMock(), AsyncMock(), repo)

        assert result.run is not None and result.run.id is not None
        native_runs = await signals.list_runs("greenkube.good")
        assert [run.optimization_run_id for run in native_runs] == [result.run.id]
        assert native_runs[0].signal_count == 1 and native_runs[0].recommendation_count == 1
        [stored] = await signals.list_signals("greenkube.good")
        persisted = {record.fingerprint for record in await repo.get_active_recommendations()}
        assert stored.recommendation_fingerprint in persisted
        assert stored.signal.target.key() == f"{engine.cluster_uid}/apps.Deployment/prod/alive"

        # A plugin's signals are recorded and become a recommendation only through consensus (E6),
        # under the plugin's own source id.
        [plugin_signal] = await signals.list_signals("acme")
        assert plugin_signal.recommendation_fingerprint is None
        assert await _active(repo) == {"alive", "pv-9"}
        [pv] = [record for record in await repo.get_active_recommendations() if record.pod_name == "pv-9"]
        assert pv.source == "acme"
        assert pv.sources == ["acme"]
        assert pv.consensus is not None and pv.consensus["chosen_from"] == ["acme"]
        assert {o.source for o in result.source_outcomes} == {"greenkube", "acme"}

    async def test_circuit_state_survives_engines_through_storage(self, sqlite_db):
        repo = SQLiteRecommendationRepository(sqlite_db)
        signals = create_signal_repository(sqlite_db, "sqlite")
        await _seed(repo, _rec("alive"))
        now = datetime.now(timezone.utc)
        await signals.record_run(
            SourceRunRecord(
                source_id="greenkube.good",
                status=SourceRunStatus.FAILED,
                started_at=now - timedelta(minutes=1),
                finished_at=now - timedelta(minutes=1),
                consecutive_failures=3,
                error_code="RuntimeError",
            )
        )
        deps = SourceDeps(config=get_config(), cluster_uid="c1")
        engine = _engine([analyzer_source_class(GoodAnalyzer)(SourceSettings(), deps)], signal_repository=signals)

        result = await engine.run_scan(AsyncMock(), AsyncMock(), repo)

        [report] = result.source_reports
        assert report.run.status == SourceRunStatus.SKIPPED
        assert report.run.error_code == "circuit_open"
        assert result.source_outcomes[0].status == V1Status.INCOMPLETE
        # Skipped is not authoritative: the last known advice stays active.
        assert await _active(repo) == {"alive"}

    async def test_storage_errors_never_fail_the_scan(self, sqlite_db):
        repo = SQLiteRecommendationRepository(sqlite_db)
        broken = AsyncMock()
        broken.latest_runs.side_effect = ConnectionError("db down")
        broken.record_run.side_effect = ConnectionError("db down")
        deps = SourceDeps(config=get_config(), cluster_uid="c1")
        engine = _engine([analyzer_source_class(GoodAnalyzer)(SourceSettings(), deps)], signal_repository=broken)

        result = await engine.run_scan(AsyncMock(), AsyncMock(), repo)

        assert [r.pod_name for r in result.recommendations] == ["alive"]
        assert result.source_outcomes[0].status == V1Status.SUCCEEDED


async def test_v1_connectors_are_wrapped_and_keep_their_output(sqlite_db):
    from greenkube.core.optimization.providers.base import RecommendationSource

    class Legacy(RecommendationSource):
        name = "greenkube"

        async def is_available(self):
            return True

        async def collect(self, context):
            return [_rec("legacy")]

    engine = _engine([Legacy()])
    recs: List[Recommendation] = await engine.generate(OptimizationContext(config=get_config(), metrics=[]))
    assert [r.pod_name for r in recs] == ["legacy"]
    assert engine.sources[0].source_id == "greenkube"
    assert engine.source_outcomes[0].status == V1Status.SUCCEEDED
