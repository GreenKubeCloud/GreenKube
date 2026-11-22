"""Regression (E8): a failing native analyzer must not make its recommendations stale.

``NativeSource`` swallowed analyzer exceptions and reported a complete result,
so reconciliation marked every active recommendation of the failed analyzer
stale although nothing said it was resolved.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from greenkube.core.config import Config, config, get_config
from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.engine import OptimizationEngine
from greenkube.core.optimization.providers.native import NativeSource
from greenkube.models.metrics import Recommendation, RecommendationRecord, RecommendationType
from greenkube.models.optimization_run import SourceRunStatus
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository


def _rec(name: str, rec_type: RecommendationType) -> Recommendation:
    return Recommendation(
        pod_name=name,
        namespace="prod",
        type=rec_type,
        scope="workload",
        owner_kind="Deployment",
        owner_name=name,
        description=f"{name} advice",
    )


class GoodAnalyzer(Analyzer):
    capability = "zombie_cleanup"

    def analyze(self, context):
        return [_rec("alive", RecommendationType.ZOMBIE_POD)]


class BrokenAnalyzer(Analyzer):
    capability = "lb_cleanup"

    def analyze(self, context):
        raise RuntimeError("analyzer bug")


@pytest.fixture
async def sqlite_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "native.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield manager
    finally:
        await manager.close()


async def test_failed_analyzer_makes_the_native_result_partial(sqlite_db):
    repo = SQLiteRecommendationRepository(sqlite_db)
    previous = [
        _rec("alive", RecommendationType.ZOMBIE_POD),
        _rec("lb-user", RecommendationType.ORPHANED_LOAD_BALANCER),
    ]
    await repo.upsert_recommendations([RecommendationRecord.from_recommendation(rec) for rec in previous])
    native = NativeSource(get_config(), analyzer_classes=[GoodAnalyzer, BrokenAnalyzer])
    engine = OptimizationEngine(sources=[native])
    engine.build_context = AsyncMock(return_value=OptimizationContext(config=get_config(), metrics=[]))

    result = await engine.run_scan(AsyncMock(), AsyncMock(), repo)

    assert result.source_outcomes[0].status == SourceRunStatus.INCOMPLETE
    active = {record.pod_name for record in await repo.get_active_recommendations()}
    assert active == {"alive", "lb-user"}


async def test_the_flag_is_reset_when_every_analyzer_succeeds():
    native = NativeSource(get_config(), analyzer_classes=[GoodAnalyzer, BrokenAnalyzer])
    await native.collect(OptimizationContext(config=get_config(), metrics=[]))
    assert native.last_collect_complete is False

    native.analyzers = [GoodAnalyzer(get_config())]
    await native.collect(OptimizationContext(config=get_config(), metrics=[]))
    assert native.last_collect_complete is True
