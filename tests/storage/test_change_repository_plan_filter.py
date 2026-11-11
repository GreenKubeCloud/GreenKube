"""``list_changes(plan_id=...)``: the change of one plan, on SQLite and PostgreSQL (J1, migration 0078)."""

from datetime import datetime, timedelta, timezone

from greenkube.storage.change_repository import ChangeRepository
from greenkube.verify.model import ChangeEvent, WorkloadRef
from tests.storage.test_workload_usage_repository import db  # noqa: F401  # pyrefly: ignore[missing-import]


def _change(change_id: str, plan_id: str | None, created_at: datetime, name: str) -> ChangeEvent:
    return ChangeEvent(
        change_id=change_id,
        plan_id=plan_id,
        recommendation_ids=[1],
        target=WorkloadRef(namespace="prod", kind="Deployment", name=name),
        workload_uid=f"uid-{name}",
        generation_before=1,
        generation_after=2,
        spec_digest_before="digest-before",
        spec_digest_after=f"digest-after-{change_id}",
        rollout_started_at=created_at - timedelta(minutes=5),
        rollout_completed_at=created_at,
        baseline_window_end=created_at - timedelta(minutes=30),
        created_at=created_at,
    )


async def test_list_changes_filters_by_plan_id(db):  # noqa: F811
    repository = ChangeRepository(db)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    await repository.insert_change(_change("chg-old", "pln-target", now - timedelta(hours=3), "api"))
    for index in range(5):
        await repository.insert_change(
            _change(f"chg-other-{index}", f"pln-other-{index}", now - timedelta(minutes=index), f"web-{index}")
        )
    await repository.insert_change(_change("chg-detected", None, now, "worker"))

    rows = await repository.list_changes(plan_id="pln-target", limit=1)
    assert [change.change_id for change, _ in rows] == ["chg-old"]
    assert rows[0][0].plan_id == "pln-target"
    assert rows[0][1].state.value == "applied"

    assert await repository.list_changes(plan_id="pln-unknown") == []
    # Without the filter, every change is listed (newest first).
    assert len(await repository.list_changes(limit=50)) == 7
