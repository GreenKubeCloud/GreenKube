"""REC-06: an ignored recommendation survives the next scan (SQLite and PostgreSQL).

The next scan must not insert the same advice again as a new active row; the
user's decision stays on the one row of that target. Un-ignoring gives exactly
one active row, and advice that changed materially while ignored is recorded
as a ``changed_while_ignored`` event on the ignored row.
"""

import pytest

from greenkube.models.metrics import IgnoreRecommendationRequest, RecommendationRecord, RecommendationType
from greenkube.storage.base_repository import UpsertOutcome

from .test_real_database_repositories import (  # pyrefly: ignore[missing-import]  # noqa: F401
    RealDatabase,
    real_database,
)

ALL_STATUSES = ["active", "ignored", "stale", "expired", "pr_open", "applied"]


def _cpu(current: int, proposed: int, namespace: str) -> RecommendationRecord:
    return RecommendationRecord(
        pod_name="api",
        namespace=namespace,
        container_name="app",
        type=RecommendationType.RIGHTSIZING_CPU,
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        fingerprint=f"v3:workload:{namespace}:api:app::Deployment:api:RIGHTSIZING_CPU",
        description="cpu rightsizing",
        reason="r",
        priority="medium",
        current_cpu_request_millicores=current,
        recommended_cpu_request_millicores=proposed,
    )


async def _ignored(repo, namespace: str) -> int:
    await repo.upsert_recommendations([_cpu(1000, 300, namespace)])
    [row] = await repo.get_active_recommendations(namespace=namespace)
    assert row.id is not None
    await repo.ignore_recommendation(row.id, IgnoreRecommendationRequest(reason="batch job, sized on purpose"))
    return row.id


async def test_ignored_advice_is_not_inserted_again(real_database: RealDatabase):  # noqa: F811
    repo = real_database.recommendation_repository()
    ignored_id = await _ignored(repo, "ign1")

    outcomes: list[UpsertOutcome] = []
    written = await repo.upsert_recommendations([_cpu(1000, 300, "ign1")], outcomes=outcomes)

    assert written == 0
    assert [(outcome.action, outcome.recommendation_id) for outcome in outcomes] == [("ignored", ignored_id)]
    rows = await repo.get_recommendations_by_statuses(ALL_STATUSES, namespace="ign1")
    assert [(row.id, row.status.value) for row in rows] == [(ignored_id, "ignored")]


async def test_unignore_then_scan_leaves_one_active_row(real_database: RealDatabase):  # noqa: F811
    repo = real_database.recommendation_repository()
    ignored_id = await _ignored(repo, "ign2")
    await repo.upsert_recommendations([_cpu(1000, 300, "ign2")])

    await repo.unignore_recommendation(ignored_id)
    await repo.upsert_recommendations([_cpu(1000, 300, "ign2")])

    rows = await repo.get_recommendations_by_statuses(ALL_STATUSES, namespace="ign2")
    assert [(row.id, row.status.value) for row in rows] == [(ignored_id, "active")]


async def test_changed_advice_while_ignored_records_an_event_once(real_database: RealDatabase):  # noqa: F811
    repo = real_database.recommendation_repository()
    ignored_id = await _ignored(repo, "ign3")

    await repo.upsert_recommendations([_cpu(1000, 150, "ign3")])
    await repo.upsert_recommendations([_cpu(1000, 150, "ign3")])

    rows = await repo.get_recommendations_by_statuses(ALL_STATUSES, namespace="ign3")
    assert [(row.id, row.status.value) for row in rows] == [(ignored_id, "ignored")]
    assert rows[0].recommended_cpu_request_millicores == 300
    changed = [event for event in await repo.get_events(ignored_id) if event.event_type == "changed_while_ignored"]
    assert len(changed) == 1
    assert changed[0].payload.details["recommended_cpu_request_millicores"] == 150
    [listed] = await repo.get_ignored_recommendations(namespace="ign3")
    assert listed.changed_since_ignored is not None
    assert listed.changed_since_ignored["recommended_cpu_request_millicores"] == 150
    assert listed.changed_since_ignored["ignored"]["recommended_cpu_request_millicores"] == 300


@pytest.mark.parametrize("status", ["stale", "expired"])
async def test_ignoring_an_old_row_retires_its_active_twin(real_database: RealDatabase, status):  # noqa: F811
    repo = real_database.recommendation_repository()
    namespace = f"twin-{status}"
    await repo.upsert_recommendations([_cpu(1000, 300, namespace)])
    [old] = await repo.get_active_recommendations(namespace=namespace)
    assert old.id is not None
    await repo.update_recommendation_fields(old.id, {"status": status})
    await repo.upsert_recommendations([_cpu(1000, 300, namespace)])
    [twin] = await repo.get_active_recommendations(namespace=namespace)

    await repo.ignore_recommendation(old.id, IgnoreRecommendationRequest(reason="not now"))

    assert await repo.get_active_recommendations(namespace=namespace) == []
    assert twin.id is not None
    retired = await repo.get_recommendation_by_id(twin.id)
    assert retired is not None and retired.status.value == "stale"
