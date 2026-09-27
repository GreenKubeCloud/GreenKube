# tests/core/optimization/test_applied_detector.py
"""Tests for Kubernetes-based apply detection."""

import pytest

from greenkube.core.config import Config
from greenkube.core.db import db_manager
from greenkube.core.optimization.applied_detector import AppliedDetector, WorkloadSnapshot
from greenkube.core.optimization.lifecycle import RecommendationLifecycle
from greenkube.models.metrics import (
    RecommendationRecord,
    RecommendationStatus,
    RecommendationType,
)
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository


@pytest.fixture
async def repo():
    await db_manager.setup_sqlite(db_path=":memory:")
    yield SQLiteRecommendationRepository(db_manager)
    await db_manager.close()


class FakeReader:
    def __init__(self, snapshots):
        self._snapshots = snapshots

    async def read(self):
        return self._snapshots


def _cpu_record(
    current: int = 1000,
    recommended: int = 300,
    status: RecommendationStatus = RecommendationStatus.ACTIVE,
) -> RecommendationRecord:
    return RecommendationRecord(
        pod_name="api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="CPU oversized",
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        status=status,
        current_cpu_request_millicores=current,
        recommended_cpu_request_millicores=recommended,
    )


def _memory_record() -> RecommendationRecord:
    return RecommendationRecord(
        pod_name="api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_MEMORY,
        description="Memory oversized",
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        current_memory_request_bytes=1024 * 1024 * 1024,
        recommended_memory_request_bytes=256 * 1024 * 1024,
    )


class TestAppliedDetector:
    @pytest.mark.asyncio
    async def test_detects_cpu_decrease(self, repo):
        await repo.save_recommendations([_cpu_record()])
        reader = FakeReader({("prod", "Deployment", "api"): WorkloadSnapshot("prod", "Deployment", "api", 300, 0)})
        detector = AppliedDetector(RecommendationLifecycle(repo), reader=reader, config=Config())

        applied = await detector.detect()

        assert len(applied) == 1
        assert applied[0].status == RecommendationStatus.APPLIED
        assert applied[0].application_method == "detected"
        assert applied[0].actual_cpu_request_millicores == 300

    @pytest.mark.asyncio
    async def test_ignores_unchanged_request(self, repo):
        await repo.save_recommendations([_cpu_record()])
        reader = FakeReader({("prod", "Deployment", "api"): WorkloadSnapshot("prod", "Deployment", "api", 1000, 0)})
        detector = AppliedDetector(RecommendationLifecycle(repo), reader=reader, config=Config())

        assert await detector.detect() == []

    @pytest.mark.asyncio
    async def test_within_tolerance_of_recommendation(self, repo):
        await repo.save_recommendations([_cpu_record(current=1000, recommended=300)])
        # 320m is within 25% of the 300m target even though the drop is < 25%.
        reader = FakeReader({("prod", "Deployment", "api"): WorkloadSnapshot("prod", "Deployment", "api", 320, 0)})
        detector = AppliedDetector(RecommendationLifecycle(repo), reader=reader, config=Config())

        assert len(await detector.detect()) == 1

    @pytest.mark.asyncio
    async def test_pr_open_becomes_pr_merge(self, repo):
        await repo.save_recommendations([_cpu_record(status=RecommendationStatus.PR_OPEN)])
        reader = FakeReader({("prod", "Deployment", "api"): WorkloadSnapshot("prod", "Deployment", "api", 300, 0)})
        detector = AppliedDetector(RecommendationLifecycle(repo), reader=reader, config=Config())

        applied = await detector.detect()

        assert len(applied) == 1
        assert applied[0].application_method == "pr_merge"

    @pytest.mark.asyncio
    async def test_detects_memory_decrease(self, repo):
        await repo.save_recommendations([_memory_record()])
        reader = FakeReader(
            {("prod", "Deployment", "api"): WorkloadSnapshot("prod", "Deployment", "api", 0, 256 * 1024 * 1024)}
        )
        detector = AppliedDetector(RecommendationLifecycle(repo), reader=reader, config=Config())

        applied = await detector.detect()

        assert len(applied) == 1
        assert applied[0].type == RecommendationType.RIGHTSIZING_MEMORY

    @pytest.mark.asyncio
    async def test_missing_snapshot_is_skipped(self, repo):
        await repo.save_recommendations([_cpu_record()])
        reader = FakeReader({})
        detector = AppliedDetector(RecommendationLifecycle(repo), reader=reader, config=Config())

        assert await detector.detect() == []
