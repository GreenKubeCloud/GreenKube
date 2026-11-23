"""Demo GitOps automation: plans, pull requests, recommendations and changes agree across pages."""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.automation.plans import AutomationPlanError
from greenkube.core.db import db_manager
from greenkube.core.optimization import lifecycle
from greenkube.demo.automation import (
    DEMO_DELIVERIES,
    PLAN_PATH,
    DemoLiveState,
    DemoPlanService,
    plan_path,
    populate_demo_automation,
)
from greenkube.demo.changes import DEMO_CHANGES, merge_sha, populate_demo_changes, pull_request_url
from greenkube.demo.data_generator import generate_recommendations
from greenkube.gitops.contracts import ActionStatus, PlanStatus
from greenkube.models.metrics import PullRequestStatus, RecommendationStatus
from greenkube.storage.change_repository import ChangeRepository
from greenkube.storage.sqlite.gitops_repository import SQLiteGitOpsRepository
from greenkube.storage.sqlite.plan_operation_repository import SQLitePlanOperationRepository
from greenkube.storage.sqlite.pull_request_repository import SQLitePullRequestRepository
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository
from greenkube.verify.model import MeasurementState, is_allowed_transition

#: Recommendation status of each plan status, as the PR lifecycle and the apply detector leave it.
_REC_STATUS = {
    PlanStatus.VALIDATED: {RecommendationStatus.ACTIVE},
    PlanStatus.PR_OPEN: {RecommendationStatus.PR_OPEN},
    PlanStatus.MERGED: {RecommendationStatus.PR_OPEN},
    PlanStatus.SYNCED: {RecommendationStatus.PR_OPEN},
    PlanStatus.APPLIED: {
        RecommendationStatus.APPLIED,
        RecommendationStatus.VERIFYING,
        RecommendationStatus.VERIFIED,
        RecommendationStatus.REGRESSED,
    },
    PlanStatus.CLOSED: {RecommendationStatus.IGNORED},
}
_ACTION_STATUS = {
    PlanStatus.VALIDATED: ActionStatus.PLAN_READY,
    PlanStatus.PR_OPEN: ActionStatus.PR_OPEN,
    PlanStatus.MERGED: ActionStatus.MERGED,
    PlanStatus.SYNCED: ActionStatus.SYNC_PENDING,
    PlanStatus.APPLIED: ActionStatus.APPLIED,
    PlanStatus.CLOSED: ActionStatus.CLOSED,
}
#: Recommendation status paths from ``active`` the production lifecycle allows.
_REC_PATHS = {
    RecommendationStatus.ACTIVE: ["active"],
    RecommendationStatus.PR_OPEN: ["active", "pr_open"],
    RecommendationStatus.APPLIED: ["active", "pr_open", "applied"],
    RecommendationStatus.VERIFYING: ["active", "pr_open", "applied", "verifying"],
    RecommendationStatus.VERIFIED: ["active", "pr_open", "applied", "verifying", "verified"],
    RecommendationStatus.REGRESSED: ["active", "pr_open", "applied", "verifying", "verified", "regressed"],
    RecommendationStatus.IGNORED: ["active", "pr_open", "active", "ignored"],
}
#: Change state paths of each measurement stage.
_CHANGE_PATHS = {
    MeasurementState.APPLIED: [MeasurementState.APPLIED],
    MeasurementState.MEASURING: [MeasurementState.APPLIED, MeasurementState.MEASURING],
    MeasurementState.REALIZED: [MeasurementState.APPLIED, MeasurementState.MEASURING, MeasurementState.REALIZED],
    MeasurementState.NOT_REALIZED: [
        MeasurementState.APPLIED,
        MeasurementState.MEASURING,
        MeasurementState.NOT_REALIZED,
    ],
    MeasurementState.INCONCLUSIVE: [
        MeasurementState.APPLIED,
        MeasurementState.MEASURING,
        MeasurementState.INCONCLUSIVE,
    ],
    MeasurementState.REGRESSED: [
        MeasurementState.APPLIED,
        MeasurementState.MEASURING,
        MeasurementState.REALIZED,
        MeasurementState.REGRESSED,
    ],
}


def test_plan_paths_follow_the_delivery_pipeline():
    for delivery in DEMO_DELIVERIES:
        path = plan_path(delivery.status)
        assert path[0] is PlanStatus.VALIDATED and path[-1] is delivery.status
        for previous, current in zip(path, path[1:]):
            if current is PlanStatus.CLOSED:
                assert previous is PlanStatus.PR_OPEN
            else:
                assert PLAN_PATH.index(current) == PLAN_PATH.index(previous) + 1


def test_recommendation_paths_are_lifecycle_transitions():
    for path in _REC_PATHS.values():
        for previous, current in zip(path, path[1:]):
            assert current in lifecycle._ALLOWED_TRANSITIONS[previous], (previous, current)


def test_change_paths_are_measurement_transitions():
    for path in _CHANGE_PATHS.values():
        for previous, current in zip(path, path[1:]):
            assert is_allowed_transition(previous, current), (previous, current)


def test_deliveries_cover_the_pipeline_stages():
    statuses = {delivery.status for delivery in DEMO_DELIVERIES}
    assert {
        PlanStatus.VALIDATED,
        PlanStatus.PR_OPEN,
        PlanStatus.MERGED,
        PlanStatus.SYNCED,
        PlanStatus.APPLIED,
        PlanStatus.CLOSED,
    } <= statuses
    numbers = [(d.repository, d.pr_number) for d in DEMO_DELIVERIES if d.pr_number is not None]
    assert len(numbers) == len(set(numbers))
    landed = {d.owner_name for d in DEMO_DELIVERIES if d.status is PlanStatus.APPLIED}
    assert {spec.owner_name for spec in DEMO_CHANGES} == landed


@pytest.fixture
async def demo_db():
    await db_manager.setup_sqlite(db_path=":memory:")
    try:
        reco = SQLiteRecommendationRepository(db_manager)
        await reco.save_recommendations(generate_recommendations())
        gitops = SQLiteGitOpsRepository(db_manager)
        operations = SQLitePlanOperationRepository(db_manager)
        prs = SQLitePullRequestRepository(db_manager)
        changes = ChangeRepository(db_manager)
        now = datetime.now(timezone.utc)
        result = await populate_demo_automation(reco, gitops, operations, prs, db_manager, now=now)
        await populate_demo_changes(reco, changes, plan_ids=result.plan_ids, now=now)
        yield {
            "reco": reco,
            "gitops": gitops,
            "operations": operations,
            "prs": prs,
            "changes": changes,
            "result": result,
            "now": now,
        }
    finally:
        await db_manager.close()


@pytest.mark.asyncio
async def test_plans_pull_requests_and_recommendations_agree(demo_db):
    reco, gitops, prs = demo_db["reco"], demo_db["gitops"], demo_db["prs"]
    plan_ids = demo_db["result"].plan_ids
    records = await reco.get_recommendations_by_statuses([status.value for status in RecommendationStatus])
    by_owner = {(record.namespace, record.owner_name): record for record in records}
    assert len(plan_ids) == len(DEMO_DELIVERIES)
    open_numbers = {pr.pr_number for pr in await prs.get_open_pull_requests()}
    for delivery in DEMO_DELIVERIES:
        record = by_owner[(delivery.namespace, delivery.owner_name)]
        assert record.id is not None
        plan = await gitops.get_plan(plan_ids[record.id])
        assert plan is not None and plan.status is delivery.status
        assert plan.recommendation_ids == [record.id]
        assert plan.file_plan is not None and plan.file_plan.validation.passed
        assert record.status in _REC_STATUS[delivery.status], (delivery.owner_name, record.status)
        assert await gitops.get_recommendation_action_status(record.id) is _ACTION_STATUS[delivery.status]
        attempts = await prs.get_pull_requests_for_recommendation(record.id)
        if delivery.pr_number is None:
            assert attempts == []
            continue
        (pr,) = attempts
        assert pr.plan_id == plan.plan_id and pr.pr_number == delivery.pr_number
        assert pr.pr_url == pull_request_url(delivery.repository, delivery.pr_number)
        assert (pr.pr_number in open_numbers) is (delivery.status is PlanStatus.PR_OPEN)
        if delivery.status is PlanStatus.CLOSED:
            assert pr.status is PullRequestStatus.CLOSED and pr.merged_at is None
        elif delivery.status is not PlanStatus.PR_OPEN:
            assert pr.status is PullRequestStatus.MERGED
            assert pr.merge_commit_sha == merge_sha(delivery.repository, delivery.pr_number)
            assert pr.sync_status == ("pending" if delivery.status is PlanStatus.MERGED else "synced")


@pytest.mark.asyncio
async def test_changes_link_their_plan_and_pull_request(demo_db):
    gitops, changes, prs = demo_db["gitops"], demo_db["changes"], demo_db["prs"]
    rows = await changes.list_changes(limit=50)
    assert len(rows) == len(DEMO_CHANGES)
    for change, status in rows:
        assert status.state in _CHANGE_PATHS
        plan = await gitops.get_plan(change.plan_id)
        assert plan is not None and plan.status is PlanStatus.APPLIED
        assert plan.recommendation_ids == change.recommendation_ids
        (pr,) = await prs.get_pull_requests_for_recommendation(change.recommendation_ids[0])
        assert pr.pr_url == change.pr_url and pr.merge_commit_sha == change.merge_sha
        assert pr.merged_at is not None and change.synced_at is not None and change.rollout_completed_at is not None
        assert pr.merged_at < change.synced_at < change.rollout_completed_at


@pytest.mark.asyncio
async def test_audit_events_carry_the_time_of_their_step(demo_db):
    reco, now = demo_db["reco"], demo_db["now"]
    for record_id in demo_db["result"].plan_ids:
        events = await reco.get_events(record_id)
        assert events
        record = await reco.get_recommendation_by_id(record_id)
        assert record is not None
        if record.applied_at is not None and record.applied_at < now - timedelta(days=2):
            # Months-old deliveries: no event is stamped with the time the demo started.
            assert all(event.created_at < now - timedelta(hours=1) for event in events), record.owner_name


@pytest.mark.asyncio
async def test_demo_plan_service_reads_and_refuses_writes(demo_db):
    service = DemoPlanService(demo_db["gitops"], demo_db["operations"])
    operations = {}
    for plan_id in demo_db["result"].plan_ids.values():
        plan = await service.read_plan(plan_id)
        assert plan is not None
        operations[plan.status] = await service.read_operation_for_plan(plan_id)
    # A preview has no operation; every approved plan's operation succeeded (the PR was opened).
    assert operations.pop(PlanStatus.VALIDATED) is None
    assert all(operation is not None and operation.status.value == "succeeded" for operation in operations.values())
    with pytest.raises(AutomationPlanError):
        await service.approve_plan(next(iter(demo_db["result"].plan_ids.values())), "sha256:" + "0" * 64)
    with pytest.raises(AutomationPlanError):
        await service.prepare_plan([1])


@pytest.mark.asyncio
async def test_live_state_reports_a_configured_install(demo_db):
    from greenkube.storage.automation_capability_repository import AutomationCapabilityRepository

    await DemoLiveState(db_manager, demo_db["gitops"], demo_db["operations"]).refresh()
    rows = {row.subject: row for row in await AutomationCapabilityRepository(db_manager).list_capabilities()}
    assert rows["discovery"].state == "active"
    assert rows["delivery"].state == "active"
    repositories = [row for row in rows.values() if row.scope == "repository"]
    assert len(repositories) == 2 and all(row.payload["allowlisted"] for row in repositories)
    pending = await demo_db["gitops"].list_plans(status=PlanStatus.VALIDATED)
    assert pending and all(plan.expires_at is not None and plan.expires_at > demo_db["now"] for plan in pending)
