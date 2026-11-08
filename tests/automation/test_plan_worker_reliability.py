"""Plan worker reliability regressions: moved heads (D7, BUG-49) and lease loss (D10, BUG-52)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

from greenkube.automation.git.base import GitCommitRequest, GitCommitResult, GitConflictError, GitFile, GitRepository
from greenkube.automation.plan_worker import plan_branch_name
from greenkube.automation.rollback import inverse_plan_id
from greenkube.gitops.contracts import (
    ActionStatus,
    OperationStatus,
    Plan,
    PlanStatus,
    PreApplyWorkloadBaseline,
)
from greenkube.models.metrics import (
    PullRequestRecord,
    PullRequestStatus,
    RecommendationRecord,
    RecommendationStatus,
    RecommendationType,
)
from tests.automation.test_plan_worker import (  # pyrefly: ignore[missing-import]
    BASE_CONTENT,
    BASE_REVISION,
    MANIFEST_PATH,
    NEW_CONTENT,
    NEW_PATH,
    PLAN_DIGEST,
    UPDATED_CONTENT,
    FakeGitProvider,
    FakePlanRepository,
    _blob_sha,
    _operation_lease,
    _plan,
    _worker,
)

MOVED_HEAD = "c" * 40
UNRELATED_PATH = "apps/other/configmap.yaml"


class MovedHeadProvider(FakeGitProvider):
    """The source branch gained an unrelated commit after the plan was rendered."""

    def __init__(self, *, touched_file_changed: bool = False) -> None:
        super().__init__()
        self.head_tree = dict(self.base_tree)
        self.head_tree[UNRELATED_PATH] = GitFile(UNRELATED_PATH, "unrelated\n", "e" * 40, "e" * 40)
        if touched_file_changed:
            self.head_tree[MANIFEST_PATH] = GitFile(MANIFEST_PATH, "edited by a human\n", "9" * 40, "9" * 40)
        # Commit trees are immutable; ``head`` is what the source branch currently points at.
        self.trees: dict[str, dict[str, GitFile]] = {BASE_REVISION: self.base_tree, MOVED_HEAD: self.head_tree}
        self.head: str = MOVED_HEAD
        self.file_reads: list[tuple[str, str]] = []

    def advance_head(self, revision: str, extra: dict[str, GitFile]) -> None:
        self.trees[revision] = {**self.trees[self.head], **extra}
        self.head = revision

    def _tree(self, ref: str) -> dict[str, GitFile]:
        if ref in self.branches:
            return self.branches[ref]
        return self.trees.get(ref, {})

    async def get_file(self, repo: GitRepository, path: str, ref: str) -> GitFile | None:
        self.file_reads.append((path, ref))
        file = self._tree(ref).get(path)
        return None if file is None else GitFile(file.path, file.content, file.sha, file.blob_sha)

    async def list_files(self, repo: GitRepository, ref: str) -> list[str]:
        return sorted(self._tree(ref))

    async def create_branch(self, repo: GitRepository, branch: str, from_ref: str) -> None:
        self.branch_creations.append((branch, from_ref))
        if branch not in self.branches:
            self.branches[branch] = dict(self.trees[self.head])

    async def create_commit(self, request: GitCommitRequest) -> GitCommitResult:
        self.commit_requests.append(request)
        branch_tree = self.branches[request.branch]
        if request.base_revision != self.head or branch_tree != self.trees[self.head]:
            raise GitConflictError("branch head does not equal the requested parent")
        for edit in request.files:
            assert edit.content is not None
            branch_tree[edit.path] = GitFile(edit.path, edit.content, _blob_sha(edit.content), _blob_sha(edit.content))
        return GitCommitResult("f" * 40, request.branch, [edit.path for edit in request.files])


class RecordingPlanRepository(FakePlanRepository):
    def __init__(self, plan: Plan) -> None:
        super().__init__(plan)
        self.committed_on: list[str] = []

    async def set_plan_committed_on_revision(self, plan_id: str, revision: str, *, expected_digest: str | None):
        plan = self.plans.get(plan_id)
        if plan is None or plan.digest != expected_digest:
            return None
        self.committed_on.append(revision)
        updated = plan.model_copy(update={"committed_on_revision": revision})
        self.plans[plan_id] = updated
        return updated


def _recording_worker(**kwargs: Any):
    worker, operations, plans, pull_requests, provider = _worker(**kwargs)
    recording = RecordingPlanRepository(plans.plans["plan-1"])
    worker.plans = recording
    provider.plan_repository = recording
    return worker, operations, recording, pull_requests, provider


@pytest.mark.asyncio
async def test_unrelated_commit_on_the_source_branch_does_not_stale_an_approved_plan():
    provider = MovedHeadProvider()
    worker, operations, plans, pull_requests, _ = _recording_worker(provider=provider, revision=MOVED_HEAD)

    assert await worker.process_once("worker-1")

    assert operations.failures == []
    assert plans.plans["plan-1"].status is PlanStatus.PR_OPEN
    assert set(plans.action_status.values()) == {ActionStatus.PR_OPEN}
    assert len(provider.commit_requests) == 1
    assert provider.commit_requests[0].base_revision == MOVED_HEAD
    # Base and committed-on revisions are recorded separately.
    assert plans.committed_on == [MOVED_HEAD]
    assert plans.plans["plan-1"].committed_on_revision == MOVED_HEAD
    assert plans.plans["plan-1"].file_plan is not None
    assert plans.plans["plan-1"].file_plan.base_revision == BASE_REVISION
    branch = provider.branches[plan_branch_name(_plan())]
    assert branch[MANIFEST_PATH].content == UPDATED_CONTENT
    assert branch[NEW_PATH].content == NEW_CONTENT
    assert branch[UNRELATED_PATH].content == "unrelated\n"
    assert {record.pr_number for record in pull_requests.records} == {17}
    # Only the touched files were compared at the moved head.
    assert {(path, ref) for path, ref in provider.file_reads if ref == MOVED_HEAD} >= {
        (MANIFEST_PATH, MOVED_HEAD),
        (NEW_PATH, MOVED_HEAD),
    }


@pytest.mark.asyncio
async def test_a_change_to_a_touched_file_still_requires_a_replan():
    provider = MovedHeadProvider(touched_file_changed=True)
    worker, operations, plans, _, _ = _recording_worker(provider=provider, revision=MOVED_HEAD)

    assert await worker.process_once("worker-1")

    assert provider.branch_creations == []
    assert provider.commit_requests == []
    assert operations.failures and operations.failures[0][1] is False
    assert plans.plans["plan-1"].status is PlanStatus.STALE
    assert plans.committed_on == []


@pytest.mark.asyncio
async def test_a_file_the_plan_creates_appearing_upstream_requires_a_replan():
    provider = MovedHeadProvider()
    provider.head_tree[NEW_PATH] = GitFile(NEW_PATH, "someone else\n", "8" * 40, "8" * 40)
    worker, _, plans, _, _ = _recording_worker(provider=provider, revision=MOVED_HEAD)

    assert await worker.process_once("worker-1")

    assert provider.branch_creations == []
    assert plans.plans["plan-1"].status is PlanStatus.STALE


def _rollback_on_moved_head() -> tuple[Plan, Plan]:
    """A synced original plan and its approved rollback, rendered on ``BASE_REVISION``."""
    template = _plan()
    assert template.file_plan is not None
    original = _plan(status=PlanStatus.SYNCED).model_copy(
        update={
            "plan_id": "original-plan",
            "pre_apply_baseline": PreApplyWorkloadBaseline(
                workload_uid="uid-api-1",
                generation=7,
                observed_generation=7,
                container_requests={"api": {"cpu_millicores": 1000, "memory_bytes": 128 * 1024 * 1024}},
            ),
        }
    )
    rollback_id = inverse_plan_id(original.plan_id, PLAN_DIGEST, template.file_plan.base_revision)
    rollback = template.model_copy(
        update={
            "plan_id": rollback_id,
            "file_plan": template.file_plan.model_copy(update={"plan_id": rollback_id}),
            "rollback_of_plan_id": original.plan_id,
            "pre_apply_baseline": PreApplyWorkloadBaseline(
                workload_uid="uid-api-1",
                generation=8,
                observed_generation=8,
                container_requests={"api": {"cpu_millicores": 300, "memory_bytes": 128 * 1024 * 1024}},
            ),
        }
    )
    return original, rollback


@pytest.mark.asyncio
async def test_rollback_plan_on_a_moved_head_with_unchanged_files_opens_its_pull_request():
    """D7 must hold for rollback plans: their identity is derived from the planned base, not the moved head."""
    original, rollback = _rollback_on_moved_head()
    template_lease = _operation_lease()
    lease = replace(
        template_lease,
        operation=template_lease.operation.model_copy(
            update={"plan_id": rollback.plan_id, "idempotency_key": f"plan:{rollback.plan_id}:{PLAN_DIGEST}"}
        ),
    )
    provider = MovedHeadProvider()
    worker, operations, _, pull_requests, _ = _worker(
        plan=rollback, provider=provider, revision=MOVED_HEAD, leases=(lease,)
    )
    plans = RecordingPlanRepository(rollback)
    plans.plans[original.plan_id] = original
    worker.plans = plans
    provider.plan_repository = plans
    recommendations = {
        recommendation_id: RecommendationRecord(
            id=recommendation_id,
            pod_name="api",
            container_name="api",
            namespace=rollback.action.target.namespace,
            type=RecommendationType.RIGHTSIZING_CPU,
            description="Roll back the CPU request change.",
            scope="workload",
            status=RecommendationStatus.ROLLBACK_REVIEW,
            owner_kind=rollback.action.target.kind,
            owner_name=rollback.action.target.name,
            action=rollback.action,
        )
        for recommendation_id in rollback.recommendation_ids
    }

    class RecommendationReader:
        async def get_recommendation_by_id(self, recommendation_id: int):
            return recommendations.get(recommendation_id)

    worker.recommendation_repository = cast(Any, RecommendationReader())

    assert await worker.process_once("worker-1")

    assert operations.failures == []
    persisted = plans.plans[rollback.plan_id]
    assert persisted.status is PlanStatus.PR_OPEN
    assert persisted.committed_on_revision == MOVED_HEAD
    assert persisted.file_plan is not None and persisted.file_plan.base_revision == BASE_REVISION
    assert plans.committed_on == [MOVED_HEAD]
    assert len(provider.commit_requests) == 1
    assert provider.commit_requests[0].base_revision == MOVED_HEAD
    assert provider.commit_requests[0].message.startswith("Rollback GreenKube plan original-plan")
    assert {record.pr_number for record in pull_requests.records} == {17}
    # The PR body names the planned base and the moved head the commit was made on.
    body = provider.pull_request_bodies[0]
    assert f"`main` / `{BASE_REVISION}`" in body
    assert f"Committed on moved head: `{MOVED_HEAD}`" in body


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_at", ["mark_succeeded", "set_pr_open"])
async def test_reclaimed_operation_adopts_the_open_pull_request_instead_of_staling(lost_at):
    """Port of the BUG-52 simulation: the lease was lost after the PR was recorded."""
    provider = MovedHeadProvider()
    first_lease = _operation_lease(attempt=1)
    reclaimed_lease = _operation_lease(attempt=2, worker_id="worker-2")
    worker, operations, plans, pull_requests, _ = _recording_worker(
        provider=provider,
        revision=MOVED_HEAD,
        leases=(first_lease, reclaimed_lease),
    )
    original_mark_succeeded = operations.mark_succeeded
    lost = {"done": False}

    async def lose_lease_once(lease, **kwargs):
        if not lost["done"]:
            lost["done"] = True
            raise ConnectionError("database connection lost before mark_succeeded")
        return await original_mark_succeeded(lease, **kwargs)

    original_set_plan_status = plans.set_plan_status

    async def lose_lease_before_pr_open(plan_id, status, **kwargs):
        if status is PlanStatus.PR_OPEN and not lost["done"]:
            lost["done"] = True
            raise ConnectionError("database connection lost before the plan became PR_OPEN")
        return await original_set_plan_status(plan_id, status, **kwargs)

    if lost_at == "mark_succeeded":
        operations.mark_succeeded = lose_lease_once  # type: ignore[method-assign]
    else:
        plans.set_plan_status = lose_lease_before_pr_open  # type: ignore[method-assign]

    assert await worker.process_once("worker-1")
    assert plans.plans["plan-1"].status is (PlanStatus.PR_OPEN if lost_at == "mark_succeeded" else PlanStatus.APPLYING)
    assert pull_requests.records and all(record.status is PullRequestStatus.OPEN for record in pull_requests.records)

    # Another unrelated commit lands before the reclaiming worker runs.
    provider.advance_head(
        "d" * 40, {"apps/other/second.yaml": GitFile("apps/other/second.yaml", "x\n", "7" * 40, "7" * 40)}
    )
    worker.revision_resolver.revision = "d" * 40  # type: ignore[attr-defined]

    assert await worker.process_once("worker-2")

    assert plans.plans["plan-1"].status is PlanStatus.PR_OPEN
    assert set(plans.action_status.values()) == {ActionStatus.PR_OPEN}
    assert len(provider.pull_requests) == 1
    assert len(provider.commit_requests) == 1
    assert operations.successes and operations.successes[-1]["pull_request_number"] == 17
    assert {record.pr_number for record in pull_requests.records} == {17}


@pytest.mark.asyncio
async def test_reclaimed_plan_already_marked_pr_open_completes_its_operation():
    provider = FakeGitProvider()
    branch = plan_branch_name(_plan())
    provider.branches[branch] = {
        MANIFEST_PATH: GitFile(MANIFEST_PATH, UPDATED_CONTENT, _blob_sha(UPDATED_CONTENT), _blob_sha(UPDATED_CONTENT)),
        NEW_PATH: GitFile(NEW_PATH, NEW_CONTENT, _blob_sha(NEW_CONTENT), _blob_sha(NEW_CONTENT)),
    }
    from greenkube.automation.git.base import PullRequestInfo

    provider.pull_requests[(branch, "main")] = PullRequestInfo(
        number=31,
        url="https://github.com/acme/gitops/pull/31",
        status="open",
        raw={"head": {"ref": branch, "sha": "f" * 40}, "base": {"ref": "main"}},
    )
    worker, operations, plans, pull_requests, _ = _recording_worker(
        plan=_plan(status=PlanStatus.PR_OPEN),
        provider=provider,
        revision="d" * 40,
        leases=(_operation_lease(attempt=2),),
    )
    for recommendation_id in (41, 42):
        await pull_requests.save_pull_request(
            PullRequestRecord(
                recommendation_id=recommendation_id,
                provider="github",
                provider_id="github-main",
                plan_id="plan-1",
                repo=provider.repo.full_name,
                base_branch="main",
                head_branch=branch,
                pr_number=31,
                pr_url="https://github.com/acme/gitops/pull/31",
                status=PullRequestStatus.OPEN,
            )
        )

    assert await worker.process_once("worker-1")

    assert operations.failures == []
    assert operations.successes and operations.successes[0]["pull_request_number"] == 31
    assert plans.plans["plan-1"].status is PlanStatus.PR_OPEN
    assert provider.branch_creations == []
    assert provider.commit_requests == []
    assert OperationStatus.SUCCEEDED
    assert BASE_CONTENT
