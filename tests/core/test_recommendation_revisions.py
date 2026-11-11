"""Plans pin recommendation revisions and newer advice reaches the PR (roadmap E7, J5)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from greenkube.automation.plan_runtime import RuntimePlanService
from greenkube.automation.pr_feedback import create_pull_request_feedback_publisher
from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.recommendation_revisions import newer_advice_hook, newer_revision_for_plan
from greenkube.models.metrics import RecommendationRecord, RecommendationType
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository


@pytest.fixture
async def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "revisions.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield SQLiteRecommendationRepository(manager)
    finally:
        await manager.close()


def _cpu(cpu: int) -> RecommendationRecord:
    return RecommendationRecord(
        pod_name="api",
        namespace="shop",
        container_name="web",
        type=RecommendationType.RIGHTSIZING_CPU,
        description=f"cpu {cpu}m",
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        current_cpu_request_millicores=500,
        recommended_cpu_request_millicores=cpu,
        fingerprint="fp-cpu",
    )


async def _open_plan(repo: SQLiteRecommendationRepository, rec_id: int) -> None:
    async with repo.db_manager.connection_scope() as conn:
        await conn.execute("UPDATE recommendation_history SET action_status = 'pr_open' WHERE id = ?", (rec_id,))
        await conn.commit()


class TestNewerRevisionForPlan:
    async def test_nothing_newer_while_the_plan_implements_the_latest_advice(self, repo):
        await repo.upsert_recommendations([_cpu(300)])
        [record] = await repo.get_active_recommendations()
        await repo.pin_plan_revisions("plan-1", {record.id: record.revision})

        assert await newer_revision_for_plan("plan-1", repo) == []
        assert await newer_advice_hook(repo)(SimpleNamespace(plan_id="plan-1"), []) is None

    async def test_queued_advice_behind_an_open_plan_is_reported(self, repo):
        await repo.upsert_recommendations([_cpu(300)])
        [record] = await repo.get_active_recommendations()
        await repo.pin_plan_revisions("plan-1", {record.id: 1})
        await _open_plan(repo, record.id)

        await repo.upsert_recommendations([_cpu(220)])

        [newer] = await newer_revision_for_plan("plan-1", repo)
        assert (newer.recommendation_id, newer.pinned_revision, newer.latest_revision, newer.queued) == (
            record.id,
            1,
            2,
            True,
        )
        note = await newer_advice_hook(repo)(SimpleNamespace(plan_id="plan-1"), [])
        assert note is not None
        assert f"recommendation #{record.id} is at revision 2 (this change implements revision 1)" in note
        assert "CPU request 220m" in note

    async def test_promoted_revision_after_the_plan_is_reported(self, repo):
        await repo.upsert_recommendations([_cpu(300)])
        [record] = await repo.get_active_recommendations()
        await repo.pin_plan_revisions("plan-1", {record.id: 1})
        await repo.upsert_recommendations([_cpu(250)])

        [newer] = await newer_revision_for_plan("plan-1", repo)
        assert (newer.latest_revision, newer.queued, newer.snapshot["recommended_cpu_request_millicores"]) == (
            2,
            False,
            250,
        )

    async def test_a_plan_without_pins_reports_nothing(self, repo):
        assert await newer_revision_for_plan("legacy-plan", repo) == []

    def test_the_feedback_publisher_wires_the_hook(self, repo):
        publisher = create_pull_request_feedback_publisher(
            pull_request_repository=object(),
            plan_repository=object(),
            provider_resolver=object(),
            recommendation_repository=repo,
            config=Config(),
        )
        assert publisher is not None and publisher.newer_advice is not None


class TestPlanPins:
    async def test_runtime_plan_service_pins_the_planned_revisions(self, repo):
        await repo.upsert_recommendations([_cpu(300)])
        await repo.upsert_recommendations([_cpu(260)])
        [record] = await repo.get_active_recommendations()
        assert record.revision == 2
        service = RuntimePlanService(
            recommendation_repository=repo,
            binding_repository=cast(Any, object()),
            cluster="prod",
            snapshot_resolver=cast(Any, object()),
            plan_service=cast(Any, SimpleNamespace(planners={})),
        )

        revisions = await service._recommendation_revisions([record.id])
        await service._pin_revisions(
            cast(Any, SimpleNamespace(plan_id="plan-9", recommendation_ids=[record.id])), revisions
        )

        assert await repo.get_plan_pins("plan-9") == {record.id: 2}
