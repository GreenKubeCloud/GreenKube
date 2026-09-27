# tests/core/optimization/test_engine.py
"""Tests for the unified OptimizationEngine orchestration."""

from unittest.mock import AsyncMock

from greenkube.core.config import get_config
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.engine import OptimizationEngine
from greenkube.core.optimization.providers.base import RecommendationSource
from greenkube.models.metrics import Recommendation, RecommendationType


class FakeSource(RecommendationSource):
    name = "fake"
    priority = 0

    def __init__(self, recs=None, available=True, error=None):
        self._recs = recs or []
        self._available = available
        self._error = error

    async def is_available(self) -> bool:
        return self._available

    async def collect(self, context):
        if self._error:
            raise self._error
        return list(self._recs)


def _context():
    return OptimizationContext(config=get_config(), metrics=[])


def _rec(pod_name="pod", cpu_recommended=None):
    return Recommendation(
        pod_name=pod_name,
        namespace="default",
        type=RecommendationType.RIGHTSIZING_CPU,
        scope="pod",
        description="desc",
        recommended_cpu_request_millicores=cpu_recommended,
    )


class TestGenerate:
    async def test_deduplicates_across_sources(self):
        engine = OptimizationEngine(sources=[FakeSource([_rec()]), FakeSource([_rec()])])
        recs = await engine.generate(_context())
        assert len(recs) == 1

    async def test_applies_minimum_thresholds(self):
        below_min = get_config().RECOMMENDATION_MIN_CPU_MILLICORES - 1
        engine = OptimizationEngine(sources=[FakeSource([_rec(cpu_recommended=below_min)])])
        recs = await engine.generate(_context())
        assert recs[0].recommended_cpu_request_millicores == get_config().RECOMMENDATION_MIN_CPU_MILLICORES

    async def test_isolates_source_failure(self):
        engine = OptimizationEngine(
            sources=[
                FakeSource(error=RuntimeError("connector down")),
                FakeSource([_rec(pod_name="healthy")]),
            ]
        )
        recs = await engine.generate(_context())
        assert [r.pod_name for r in recs] == ["healthy"]

    async def test_skips_unavailable_source(self):
        engine = OptimizationEngine(
            sources=[
                FakeSource([_rec(pod_name="should-not-appear")], available=False),
                FakeSource([_rec(pod_name="visible")]),
            ]
        )
        recs = await engine.generate(_context())
        assert [r.pod_name for r in recs] == ["visible"]


class TestRefreshAndPersist:
    async def test_refresh_persists_and_reconciles(self):
        engine = OptimizationEngine(sources=[FakeSource([_rec()])])
        engine.build_context = AsyncMock(return_value=_context())
        reco_repo = AsyncMock()

        recommendations = await engine.refresh(AsyncMock(), AsyncMock(), reco_repo, namespace="ns")

        assert len(recommendations) == 1
        reco_repo.upsert_recommendations.assert_awaited_once()
        records = reco_repo.upsert_recommendations.call_args[0][0]
        assert records[0].pod_name == "pod"
        reco_repo.reconcile_active_recommendations.assert_awaited_once_with(records, namespace="ns")

    async def test_refresh_without_recommendations_only_reconciles(self):
        engine = OptimizationEngine(sources=[FakeSource([])])
        engine.build_context = AsyncMock(return_value=_context())
        reco_repo = AsyncMock()

        await engine.refresh(AsyncMock(), AsyncMock(), reco_repo, namespace=None)

        reco_repo.upsert_recommendations.assert_not_awaited()
        reco_repo.reconcile_active_recommendations.assert_awaited_once_with([], namespace=None)

    async def test_persist_swallows_repository_errors(self):
        engine = OptimizationEngine(sources=[])
        reco_repo = AsyncMock()
        reco_repo.upsert_recommendations = AsyncMock(side_effect=RuntimeError("db down"))

        # Must not raise: persistence failures are logged, not propagated.
        await engine.persist([_rec()], reco_repo, namespace=None)
