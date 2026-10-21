from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.db import DatabaseManager
from greenkube.models.metrics import PullRequestRecord, PullRequestStatus
from greenkube.storage.base_pull_request_repository import PullRequestStateConflictError
from greenkube.storage.pull_request_mapper import MUTABLE_PR_COLUMNS, row_to_pull_request
from greenkube.storage.sqlite.pull_request_repository import SQLitePullRequestRepository


@pytest.fixture
async def pull_request_repository():
    manager = DatabaseManager()
    await manager.setup_sqlite(db_path=":memory:")
    try:
        yield SQLitePullRequestRepository(manager)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_new_metadata_defaults_and_maps_legacy_rows():
    legacy = PullRequestRecord(recommendation_id=10, provider="github", repo="acme/api")

    for field in (
        "plan_id",
        "provider_id",
        "commit_sha",
        "merge_commit_sha",
        "merged_at",
        "closed_at",
        "sync_status",
        "sync_revision",
        "sync_observed_at",
        "sync_error_code",
    ):
        assert getattr(legacy, field) is None

    mapped = row_to_pull_request(
        {
            "id": 1,
            "recommendation_id": 10,
            "provider": "github",
            "repo": "acme/api",
            "base_branch": "main",
            "head_branch": None,
            "pr_number": None,
            "pr_url": None,
            "status": "pending",
            "error": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": None,
        }
    )
    assert mapped.plan_id is None
    assert mapped.provider_id is None
    assert mapped.commit_sha is None
    assert mapped.merge_commit_sha is None
    assert mapped.merged_at is None
    assert mapped.closed_at is None
    assert mapped.sync_status is None
    assert mapped.sync_revision is None
    assert mapped.sync_observed_at is None
    assert mapped.sync_error_code is None


@pytest.mark.asyncio
async def test_metadata_updates_are_mutable_and_legacy_open_queue_is_preserved(pull_request_repository):
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    legacy = await pull_request_repository.save_pull_request(
        PullRequestRecord(
            recommendation_id=22,
            provider="github",
            repo="acme/api",
            pr_number=24,
            status=PullRequestStatus.OPEN,
            created_at=created_at,
        )
    )
    assert legacy.plan_id is None
    assert (await pull_request_repository.get_open_pull_requests(22)) == [legacy]

    merged_at = created_at + timedelta(hours=1)
    closed_at = created_at + timedelta(hours=2)
    sync_observed_at = created_at + timedelta(hours=3)
    metadata = {
        "plan_id": "plan-22",
        "provider_id": "github-installation-7",
        "commit_sha": "head-sha",
        "merge_commit_sha": "merge-sha",
        "merged_at": merged_at,
        "closed_at": closed_at,
        "sync_status": "synced",
        "sync_revision": "revision-3",
        "sync_observed_at": sync_observed_at,
        "sync_error_code": None,
        "not_a_column": "ignored",
    }

    updated = await pull_request_repository.update_pull_request(legacy.id, metadata)

    assert set(metadata) - {"not_a_column"} <= MUTABLE_PR_COLUMNS
    assert updated.id == legacy.id
    assert updated.status is PullRequestStatus.OPEN
    for field, expected in metadata.items():
        if field != "not_a_column":
            assert getattr(updated, field) == expected
    assert (await pull_request_repository.get_open_pull_requests(22)) == [updated]


@pytest.mark.asyncio
async def test_pull_request_compare_and_set_prevents_terminal_state_regression(pull_request_repository):
    record = await pull_request_repository.save_pull_request(
        PullRequestRecord(
            recommendation_id=41,
            provider="gitea",
            provider_id="gitea-main",
            repo="acme/platform",
            pr_number=42,
            plan_id="plan-41",
            status=PullRequestStatus.OPEN,
        )
    )

    closed = await pull_request_repository.update_pull_request(
        record.id,
        {"status": PullRequestStatus.CLOSED},
        expected_statuses=(PullRequestStatus.OPEN.value,),
        expected_plan_id="plan-41",
    )

    assert closed.status is PullRequestStatus.CLOSED
    with pytest.raises(PullRequestStateConflictError):
        await pull_request_repository.update_pull_request(
            record.id,
            {"status": PullRequestStatus.OPEN},
            expected_statuses=(PullRequestStatus.OPEN.value,),
            expected_plan_id="plan-41",
        )
    assert (await pull_request_repository.get_pull_request_by_id(record.id)).status is PullRequestStatus.CLOSED


@pytest.mark.asyncio
async def test_provider_reference_lookup_returns_every_record_for_a_grouped_pr(pull_request_repository):
    records = [
        PullRequestRecord(
            recommendation_id=recommendation_id,
            provider="github",
            provider_id="github-main",
            repo="acme/platform",
            pr_number=42,
            plan_id="plan-group",
            status=PullRequestStatus.OPEN,
        )
        for recommendation_id in (41, 42)
    ]
    records.append(
        PullRequestRecord(
            recommendation_id=43,
            provider="github",
            provider_id="github-main",
            repo="acme/platform",
            pr_number=43,
            plan_id="other-plan",
            status=PullRequestStatus.OPEN,
        )
    )
    saved = [await pull_request_repository.save_pull_request(record) for record in records]

    linked = await pull_request_repository.list_pull_requests_by_provider_reference("github-main", "acme/platform", 42)

    assert [record.recommendation_id for record in linked] == [42, 41]
    assert [record.id for record in linked] == [saved[1].id, saved[0].id]


@pytest.mark.asyncio
async def test_sync_pending_query_retries_timeouts_and_excludes_other_terminal_or_unlinked_prs(pull_request_repository):
    records = [
        PullRequestRecord(
            recommendation_id=51,
            provider="github",
            provider_id="github-main",
            repo="acme/platform",
            pr_number=51,
            plan_id="plan-51",
            status=PullRequestStatus.MERGED,
            merge_commit_sha="a" * 40,
        ),
        PullRequestRecord(
            recommendation_id=52,
            provider="github",
            provider_id="github-main",
            repo="acme/platform",
            pr_number=52,
            plan_id="plan-52",
            status=PullRequestStatus.MERGED,
            merge_commit_sha="b" * 40,
            sync_status="synced",
        ),
        PullRequestRecord(
            recommendation_id=53,
            provider="github",
            repo="acme/platform",
            pr_number=53,
            plan_id="legacy",
            status=PullRequestStatus.MERGED,
            merge_commit_sha="c" * 40,
        ),
        PullRequestRecord(
            recommendation_id=54,
            provider="github",
            provider_id="github-main",
            repo="acme/platform",
            pr_number=54,
            plan_id="plan-54",
            status=PullRequestStatus.OPEN,
            merge_commit_sha="d" * 40,
        ),
        PullRequestRecord(
            recommendation_id=55,
            provider="github",
            provider_id="github-main",
            repo="acme/platform",
            pr_number=55,
            plan_id="plan-55",
            status=PullRequestStatus.MERGED,
            merge_commit_sha="e" * 40,
            sync_status="pending",
        ),
        PullRequestRecord(
            recommendation_id=56,
            provider="github",
            provider_id="github-main",
            repo="acme/platform",
            pr_number=56,
            plan_id="plan-56",
            status=PullRequestStatus.MERGED,
            merge_commit_sha="f" * 40,
            sync_status="failed",
            sync_error_code="sync_timeout",
        ),
        PullRequestRecord(
            recommendation_id=57,
            provider="github",
            provider_id="github-main",
            repo="acme/platform",
            pr_number=57,
            plan_id="plan-57",
            status=PullRequestStatus.MERGED,
            merge_commit_sha="1" * 40,
            sync_status="failed",
            sync_error_code="controller_failed",
        ),
    ]
    saved = [await pull_request_repository.save_pull_request(record) for record in records]

    first_page = await pull_request_repository.list_sync_pending_pull_requests(limit=1)
    after_first = await pull_request_repository.list_sync_pending_pull_requests(limit=1, after_id=saved[0].id)

    assert [record.id for record in first_page] == [saved[0].id]
    assert [record.id for record in after_first] == [saved[4].id]
    after_pending = await pull_request_repository.list_sync_pending_pull_requests(limit=10, after_id=saved[4].id)
    assert [record.id for record in after_pending] == [saved[5].id]

    with pytest.raises(ValueError):
        await pull_request_repository.list_sync_pending_pull_requests(limit=101)


@pytest.mark.asyncio
async def test_branch_cleanup_candidates_are_bounded_terminal_and_idempotent(pull_request_repository):
    async with pull_request_repository.db_manager.connection_scope() as connection:
        await connection.execute(
            "INSERT INTO automation_plans (plan_id, status) VALUES (?, ?)",
            ("terminal-plan", "failed"),
        )
        await connection.execute(
            "INSERT INTO automation_plans (plan_id, status) VALUES (?, ?)",
            ("open-plan", "pr_open"),
        )
        await connection.execute(
            "INSERT INTO automation_plans (plan_id, status) VALUES (?, ?)",
            ("active-plan", "applying"),
        )
        await connection.execute(
            "INSERT INTO automation_plans (plan_id, status) VALUES (?, ?)",
            ("completed-plan", "failed"),
        )
        await connection.commit()

    records = [
        PullRequestRecord(
            recommendation_id=61,
            provider="github",
            provider_id="github-main",
            repo="acme/platform",
            base_branch="main",
            head_branch="greenkube/plan/" + "a" * 32,
            plan_id="terminal-plan",
            status=PullRequestStatus.OPEN,
        ),
        PullRequestRecord(
            recommendation_id=62,
            provider="github",
            provider_id="github-main",
            repo="acme/platform",
            base_branch="main",
            head_branch="greenkube/plan/" + "b" * 32,
            plan_id="open-plan",
            status=PullRequestStatus.CLOSED,
        ),
        PullRequestRecord(
            recommendation_id=63,
            provider="github",
            provider_id="github-main",
            repo="acme/platform",
            base_branch="main",
            head_branch="greenkube/plan/" + "c" * 32,
            plan_id="active-plan",
            status=PullRequestStatus.OPEN,
        ),
        PullRequestRecord(
            recommendation_id=64,
            provider="github",
            provider_id="github-main",
            repo="acme/platform",
            base_branch="main",
            head_branch="greenkube/plan/" + "d" * 32,
            plan_id="completed-plan",
            status=PullRequestStatus.OPEN,
        ),
    ]
    saved = [await pull_request_repository.save_pull_request(record) for record in records]
    await pull_request_repository.mark_plan_branch_cleanup_complete(
        "completed-plan",
        provider_id="github-main",
        repo="acme/platform",
        branch_name="greenkube/plan/" + "d" * 32,
    )

    first_page = await pull_request_repository.list_branch_cleanup_candidates(limit=1)
    second_page = await pull_request_repository.list_branch_cleanup_candidates(limit=1, after_id=saved[0].id)
    completed = await pull_request_repository.list_branch_cleanup_candidates(limit=10, after_id=saved[1].id)

    assert [record.id for record in first_page] == [saved[0].id]
    assert [record.id for record in second_page] == [saved[1].id]
    assert completed == []
    assert [record.id for record in await pull_request_repository.list_pull_requests_by_plan_id("terminal-plan")] == [
        saved[0].id
    ]
