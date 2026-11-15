"""REC-01: an applied rightsizing never swallows later, different advice for its container.

Repository contract on SQLite and PostgreSQL: after a decrease 1024Mi -> 308Mi
is applied, an OOM-driven increase 308Mi -> 616Mi from the next scan is
inserted as a new active recommendation, while a regenerated pre-change
advice within the rollup lag window is still absorbed.
"""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.models.metrics import RecommendationRecord, RecommendationType

from .test_real_database_repositories import (  # pyrefly: ignore[missing-import]  # noqa: F401
    RealDatabase,
    real_database,
)

MIB = 1024 * 1024


def _memory(current: int, proposed: int, namespace: str, **updates) -> RecommendationRecord:
    record = RecommendationRecord(
        pod_name="api",
        namespace=namespace,
        container_name="app",
        type=RecommendationType.RIGHTSIZING_MEMORY,
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        description="d",
        reason="r",
        priority="high",
        current_memory_request_bytes=current * MIB,
        recommended_memory_request_bytes=proposed * MIB,
    )
    return record.model_copy(update=updates)


async def _applied(repo, namespace: str, hours_ago: float) -> None:
    await repo.upsert_recommendations([_memory(1024, 308, namespace)])
    [row] = await repo.get_active_recommendations(namespace=namespace)
    assert row.id is not None
    await repo.update_recommendation_fields(
        row.id,
        {
            "status": "applied",
            "applied_at": datetime.now(timezone.utc) - timedelta(hours=hours_ago),
            "actual_memory_request_bytes": 308 * MIB,
        },
    )


@pytest.mark.parametrize("hours_ago", [1, 72], ids=["applied-1h-ago", "applied-3d-ago"])
async def test_new_advice_after_an_applied_decrease_is_inserted(real_database: RealDatabase, hours_ago):  # noqa: F811
    repo = real_database.recommendation_repository()
    namespace = f"shop{hours_ago}"
    await _applied(repo, namespace, hours_ago)

    await repo.upsert_recommendations([_memory(308, 616, namespace, reason="Reliability: OOM-killed")])

    active = await repo.get_active_recommendations(namespace=namespace)
    assert len(active) == 1
    assert active[0].current_memory_request_bytes == 308 * MIB
    assert active[0].recommended_memory_request_bytes == 616 * MIB


@pytest.mark.parametrize(("hours_ago", "absorbed"), [(1, True), (7, False)], ids=["within-6h", "after-7h"])
async def test_regenerated_pre_change_advice_is_absorbed_only_within_the_lag_window(
    real_database: RealDatabase,  # noqa: F811
    hours_ago,
    absorbed,
):
    repo = real_database.recommendation_repository()
    namespace = f"lag{hours_ago}"
    await _applied(repo, namespace, hours_ago)

    await repo.upsert_recommendations([_memory(1024, 308, namespace)])

    active = await repo.get_active_recommendations(namespace=namespace)
    assert len(active) == (0 if absorbed else 1)


async def _record_change_member(real_database: RealDatabase, change_id: str, recommendation_id: int) -> None:  # noqa: F811
    async with real_database.manager.connection_scope() as conn:
        if real_database.backend == "postgres":
            await conn.execute(
                "INSERT INTO change_members (change_id, recommendation_id, added_at) VALUES ($1, $2, $3)",
                change_id,
                recommendation_id,
                datetime.now(timezone.utc),
            )
        else:
            await conn.execute(
                "INSERT INTO change_members (change_id, recommendation_id, added_at) VALUES (?, ?, ?)",
                (change_id, recommendation_id, datetime.now(timezone.utc).isoformat()),
            )
            await conn.commit()


async def test_new_advice_after_an_applied_change_links_to_it(real_database: RealDatabase):  # noqa: F811
    repo = real_database.recommendation_repository()
    await _applied(repo, "link", 72)
    [applied] = await repo.get_recommendations_by_statuses(["applied"])
    assert applied.id is not None
    await _record_change_member(real_database, "chg-link-1", applied.id)

    await repo.upsert_recommendations([_memory(308, 616, "link", reason="Reliability: OOM-killed")])

    [active] = await repo.get_active_recommendations(namespace="link")
    assert active.previous_recommendation_id == applied.id
    assert active.previous_change_id == "chg-link-1"


async def test_upsert_reports_swallowed_records_as_not_written(real_database: RealDatabase):  # noqa: F811
    from greenkube.storage.base_repository import UpsertOutcome

    repo = real_database.recommendation_repository()
    await _applied(repo, "report", 1)

    outcomes: list[UpsertOutcome] = []
    written = await repo.upsert_recommendations([_memory(1024, 308, "report")], outcomes=outcomes)
    assert written == 0
    assert [(outcome.action, outcome.written) for outcome in outcomes] == [("absorbed", False)]

    outcomes = []
    written = await repo.upsert_recommendations([_memory(308, 616, "report")], outcomes=outcomes)
    assert written == 1
    assert [(outcome.action, outcome.written) for outcome in outcomes] == [("inserted", True)]
