"""D5 / BUG-47: opening a plan PR costs a bounded number of provider calls.

The worker runs against the production composition (registry, authorized
adapters, revision resolver) and real GitHub/GitLab/Gitea adapters whose HTTP
traffic is served by in-memory fakes of a 5,000-file repository.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest
import respx

from greenkube.automation.git.base import git_blob_id
from greenkube.automation.git.registry import GitProviderRegistry
from greenkube.automation.plan_runtime import RegistryPlanRevisionResolver
from greenkube.automation.plan_worker import PlanWorker, RegistryPlanProviderResolver, plan_branch_name
from greenkube.gitops.contracts import (
    ActionPlanV3,
    ActionStatus,
    BindingStatus,
    ContainerResources,
    ControllerKind,
    FileEdit,
    FilePlan,
    Operation,
    OperationStatus,
    Plan,
    PlanStatus,
    PreApplyWorkloadBaseline,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    ValidationResult,
    WorkloadSource,
)
from greenkube.models.metrics import PullRequestRecord, PullRequestStatus
from greenkube.storage.base_gitops_repository import GitOpsProviderConfig, GitOpsProviderRepository
from greenkube.storage.base_plan_operation_repository import PlanOperationLease
from tests.automation.fake_git_servers import (  # pyrefly: ignore[missing-import]
    FakeGiteaServer,
    FakeGitHubServer,
    FakeGitLabServer,
    FakeGitRepository,
)

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
PLAN_DIGEST = "sha256:" + "d" * 64
MANIFEST_PATH = "apps/payments/deployment.yaml"
POLICY_PATH = "apps/payments/policy.yaml"
BASE_MANIFEST = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\nspec:\n  cpu: 1000m\n"
UPDATED_MANIFEST = BASE_MANIFEST.replace("1000m", "300m")
POLICY = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: policy\n"
REPOSITORY_FILES = 5_000
MAX_PROVIDER_CALLS = 15
#: Derived labels, CODEOWNERS lookups and the validation status, on top of opening the PR.
MAX_PULL_REQUEST_METADATA_CALLS = 8
_METADATA_MARKERS = ("/labels", "/statuses/", "/requested_reviewers", "CODEOWNERS", "/comments", "/notes", "/users")


def _is_pull_request_metadata_call(request: str) -> bool:
    method, _, path = request.partition(" ")
    if method == "PUT" and "/merge_requests/" in path:
        return True
    return any(marker in path for marker in _METADATA_MARKERS)


SERVERS = {"github": FakeGitHubServer, "gitlab": FakeGitLabServer, "gitea": FakeGiteaServer}
API_BASE_URLS = {"github": None, "gitlab": None, "gitea": "http://gitea.local"}


def _repository_files(count: int = REPOSITORY_FILES) -> dict[str, str]:
    files = {
        f"apps/team-{index % 50}/service-{index}/deployment.yaml": f"kind: Deployment # {index}\n"
        for index in range(count - 1)
    }
    files[MANIFEST_PATH] = BASE_MANIFEST
    return files


def _plan(provider: str, base_revision: str) -> Plan:
    source = WorkloadSource(
        id="source-1",
        cluster="prod",
        namespace="payments",
        workload_kind="Deployment",
        workload_name="api",
        provider_id=f"{provider}-main",
        repo_url=f"https://{provider}.example/acme/gitops.git",
        ref="main",
        revision=base_revision,
        path="apps/payments",
        controller_kind=ControllerKind.ARGOCD,
        controller_name="payments-prod",
        controller_namespace="argocd",
        source_revision=base_revision,
        renderer=RendererKind.RAW,
        renderer_config={},
        confidence=0.98,
        status=BindingStatus.RESOLVED,
        evidence={},
    )
    action = ActionPlanV3(
        action_id="plan-1",
        recommendation_ids=[41],
        target=ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="payments", name="api"),
        operations=[SetContainerResources(container="api", resources=ContainerResources(requests={"cpu": "300m"}))],
        evidence_ref="recommendation/41",
    )
    file_plan = FilePlan(
        plan_id="plan-1",
        files=[
            FileEdit(
                path=MANIFEST_PATH,
                operation="update",
                original_sha=git_blob_id(BASE_MANIFEST),
                content=UPDATED_MANIFEST,
            ),
            FileEdit(path=POLICY_PATH, operation="create", content=POLICY),
        ],
        diff="diff --git ...",
        rendered_diff="rendered diff",
        base_revision=base_revision,
        source_revision=base_revision,
        renderer=RendererKind.RAW,
        validation=ValidationResult(
            target_changed=True,
            unrelated_changes=False,
            requests_within_limits=True,
            renderer=RendererKind.RAW,
            output_digest="sha256:" + "e" * 64,
        ),
        digest=PLAN_DIGEST,
    )
    return Plan(
        plan_id="plan-1",
        recommendation_ids=[41],
        source=source,
        action=action,
        file_plan=file_plan,
        status=PlanStatus.APPROVED,
        digest=PLAN_DIGEST,
        created_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=25),
        pre_apply_baseline=PreApplyWorkloadBaseline(
            workload_uid="uid-api-1",
            generation=7,
            observed_generation=7,
            container_requests={"api": {"cpu_millicores": 1000, "memory_bytes": 128 * 1024 * 1024}},
        ),
    )


class ProviderConfigs:
    def __init__(self, provider: str) -> None:
        self.config = GitOpsProviderConfig(
            id=f"{provider}-main",
            provider_type=provider,
            api_base_url=API_BASE_URLS[provider],
            secret_namespace="greenkube",
            secret_name="git-token",
            secret_key="token",
        )

    async def get_provider_config(self, provider_id: str) -> GitOpsProviderConfig | None:
        return self.config if provider_id == self.config.id else None

    async def list_provider_allowlists(self, provider_id: str) -> list[str]:
        return ["acme/*"]


class Secrets:
    async def resolve_secret(self, *, namespace: str | None, name: str, key: str) -> str:
        return "test-token"


class Operations:
    def __init__(self, plan: Plan) -> None:
        operation = Operation(
            operation_id="operation-1",
            plan_id=plan.plan_id,
            idempotency_key=f"plan:{plan.plan_id}:{plan.digest}",
            status=OperationStatus.RUNNING,
            attempts=1,
            worker_id="worker-1",
            provider_id=plan.source.provider_id,
        )
        self.leases = [
            PlanOperationLease(operation=operation, lease_id="lease-1", lease_expires_at=NOW + timedelta(minutes=15))
        ]
        self.successes: list[dict[str, Any]] = []
        self.failures: list[tuple[str, bool]] = []

    async def claim_next(self, worker_id: str, *, lease_seconds: int = 900) -> PlanOperationLease | None:
        return self.leases.pop(0) if self.leases else None

    async def heartbeat(self, lease: PlanOperationLease, *, lease_seconds: int = 900) -> PlanOperationLease:
        return replace(lease, lease_expires_at=NOW + timedelta(seconds=lease_seconds))

    async def mark_succeeded(self, lease: PlanOperationLease, **details: Any) -> Operation:
        self.successes.append(details)
        return lease.operation.model_copy(update={"status": OperationStatus.SUCCEEDED})

    async def mark_failed(
        self, lease: PlanOperationLease, error: str, *, retryable: bool = True, retry_at: datetime | None = None
    ) -> Operation:
        self.failures.append((error, retryable))
        return lease.operation.model_copy(update={"status": OperationStatus.FAILED})


class Plans:
    def __init__(self, plan: Plan) -> None:
        self.plans = {plan.plan_id: plan}
        self.action_status = {
            recommendation_id: ActionStatus.PLAN_READY for recommendation_id in plan.recommendation_ids
        }

    async def get_plan(self, plan_id: str) -> Plan | None:
        return self.plans.get(plan_id)

    async def set_plan_status(self, plan_id: str, status: PlanStatus, **_: Any) -> Plan | None:
        plan = self.plans[plan_id].model_copy(update={"status": status})
        self.plans[plan_id] = plan
        return plan

    async def set_plan_pre_apply_baseline_if_absent(self, plan_id: str, baseline: PreApplyWorkloadBaseline) -> Plan:
        return self.plans[plan_id]

    async def set_recommendation_action_status(
        self, recommendation_ids: Sequence[int], status: ActionStatus, **_: Any
    ) -> int:
        for recommendation_id in recommendation_ids:
            self.action_status[recommendation_id] = status
        return len(recommendation_ids)


class PullRequests:
    def __init__(self) -> None:
        self.records: list[PullRequestRecord] = []

    async def get_pull_requests_for_recommendation(self, recommendation_id: int) -> list[PullRequestRecord]:
        return [record for record in self.records if record.recommendation_id == recommendation_id]

    async def save_pull_request(self, record: PullRequestRecord) -> PullRequestRecord:
        saved = record.model_copy(update={"id": len(self.records) + 1})
        self.records.append(saved)
        return saved

    async def update_pull_request(self, pr_id: int, updates: dict[str, Any], **_: Any) -> PullRequestRecord:
        index = next(index for index, record in enumerate(self.records) if record.id == pr_id)
        self.records[index] = self.records[index].model_copy(update=updates)
        return self.records[index]


def _worker(provider: str, plan: Plan) -> tuple[PlanWorker, Operations, Plans, PullRequests]:
    configs = ProviderConfigs(provider)
    registry = GitProviderRegistry(cast(GitOpsProviderRepository, configs), Secrets())
    resolver = RegistryPlanProviderResolver(configs, registry)
    operations, plans, pull_requests = Operations(plan), Plans(plan), PullRequests()
    worker = PlanWorker(
        operations=cast(Any, operations),
        plans=cast(Any, plans),
        provider_resolver=resolver,
        revision_resolver=RegistryPlanRevisionResolver(resolver),
        pull_request_repository=cast(Any, pull_requests),
        clock=lambda: NOW,
    )
    return worker, operations, plans, pull_requests


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["github", "gitlab", "gitea"])
async def test_opening_a_plan_pr_in_a_5000_file_repository_costs_fewer_than_15_provider_calls(provider):
    repository = FakeGitRepository.with_files(_repository_files())
    server = SERVERS[provider](repository)
    plan = _plan(provider, repository.refs["main"])
    worker, operations, plans, pull_requests = _worker(provider, plan)

    with respx.mock(assert_all_called=False) as router:
        router.route(url__startswith=server.base_url).mock(side_effect=server.handle)
        assert await worker.process_once("worker-1")

    assert operations.failures == []
    assert plans.plans["plan-1"].status is PlanStatus.PR_OPEN
    core = [request for request in server.requests if not _is_pull_request_metadata_call(request)]
    assert len(core) < MAX_PROVIDER_CALLS, server.breakdown()
    # Labels, CODEOWNERS and the validation status (J2, J3, J5) cost a constant number of calls.
    assert server.calls() - len(core) <= MAX_PULL_REQUEST_METADATA_CALLS, server.breakdown()
    assert server.pull_request_labels[1] == ["greenkube", "optimization/rightsizing"]
    assert [(status["sha"], status.get("context", status.get("name"))) for status in server.statuses] == [
        (repository.refs[plan_branch_name(plan)], "greenkube/validation")
    ]
    branch = plan_branch_name(plan)
    assert repository.file_text(branch, MANIFEST_PATH) == UPDATED_MANIFEST
    assert repository.file_text(branch, POLICY_PATH) == POLICY
    head_tree = repository.tree_of(branch)
    base_tree = repository.tree_of("main")
    assert head_tree is not None and base_tree is not None
    assert {path for path in set(head_tree) | set(base_tree) if head_tree.get(path) != base_tree.get(path)} == {
        MANIFEST_PATH,
        POLICY_PATH,
    }
    assert len(server.pull_requests) == 1
    assert [(record.status, record.commit_sha) for record in pull_requests.records] == [
        (PullRequestStatus.OPEN, repository.refs[branch])
    ]
    assert operations.successes[0]["commit_sha"] == repository.refs[branch]
    # No request ever enumerates file contents of the whole repository.
    assert sum(1 for request in core if "/contents/" in request or "/repository/files/" in request) <= 4

    from greenkube.api.metrics_endpoint import get_metrics_output

    output = get_metrics_output().decode()
    assert f'greenkube_git_provider_requests_total{{operation="plan_apply",provider="{provider}"}}' in output
    assert 'greenkube_git_provider_operation_calls_count{operation="plan_apply"}' in output


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["github", "gitlab", "gitea"])
async def test_provider_call_count_does_not_grow_with_repository_size(provider):
    counts = []
    for size in (10, 5_000):
        repository = FakeGitRepository.with_files(_repository_files(size))
        server = SERVERS[provider](repository)
        worker, operations, plans, _ = _worker(provider, _plan(provider, repository.refs["main"]))
        with respx.mock(assert_all_called=False) as router:
            router.route(url__startswith=server.base_url).mock(side_effect=server.handle)
            assert await worker.process_once("worker-1")
        assert plans.plans["plan-1"].status is PlanStatus.PR_OPEN, operations.failures
        counts.append(server.calls())

    assert counts[0] == counts[1]


@pytest.mark.asyncio
async def test_unrelated_change_on_the_plan_branch_is_detected_without_listing_the_repository():
    repository = FakeGitRepository.with_files(_repository_files())
    server = FakeGitHubServer(repository)
    plan = _plan("github", repository.refs["main"])
    branch = plan_branch_name(plan)
    repository.refs[branch] = repository.refs["main"]
    repository.commit_on(branch, {"apps/team-1/service-1/deployment.yaml": "tampered\n"})
    worker, operations, plans, _ = _worker("github", plan)

    with respx.mock(assert_all_called=False) as router:
        router.route(url__startswith=server.base_url).mock(side_effect=server.handle)
        assert await worker.process_once("worker-1")

    assert plans.plans["plan-1"].status is PlanStatus.STALE
    assert operations.failures and operations.failures[0][1] is False
    assert server.pull_requests == []
    assert not any(request.startswith(("POST /repos/acme/gitops/git/commits", "PATCH")) for request in server.requests)


@pytest.mark.asyncio
async def test_retry_after_the_commit_adopts_the_applied_branch_without_a_second_commit():
    repository = FakeGitRepository.with_files(_repository_files(50))
    server = FakeGitLabServer(repository)
    plan = _plan("gitlab", repository.refs["main"])
    branch = plan_branch_name(plan)
    repository.refs[branch] = repository.refs["main"]
    committed = repository.commit_on(branch, {MANIFEST_PATH: UPDATED_MANIFEST, POLICY_PATH: POLICY})
    worker, operations, plans, pull_requests = _worker("gitlab", plan)

    with respx.mock(assert_all_called=False) as router:
        router.route(url__startswith=server.base_url).mock(side_effect=server.handle)
        assert await worker.process_once("worker-1")

    assert plans.plans["plan-1"].status is PlanStatus.PR_OPEN, operations.failures
    assert repository.refs[branch] == committed
    assert not any(request.endswith("/repository/commits") for request in server.requests)
    assert pull_requests.records[0].commit_sha == committed


@pytest.mark.asyncio
async def test_gitlab_concurrent_push_to_the_planned_file_is_a_conflict_not_an_overwrite():
    """A push to the edited file after the head check is refused by ``last_commit_id`` (F-01)."""
    repository = FakeGitRepository.with_files(_repository_files(50))
    server = FakeGitLabServer(repository)
    plan = _plan("gitlab", repository.refs["main"])
    branch = plan_branch_name(plan)
    worker, operations, plans, pull_requests = _worker("gitlab", plan)
    concurrent = "kind: Deployment # pushed concurrently\n"
    pushed: list[str] = []

    def handle(request):
        # The push lands after GreenKube checked the branch head and read the file.
        if request.method == "POST" and request.url.path.endswith("/repository/commits") and not pushed:
            pushed.append(repository.commit_on(branch, {MANIFEST_PATH: concurrent}))
        return server.handle(request)

    with respx.mock(assert_all_called=False) as router:
        router.route(url__startswith=server.base_url).mock(side_effect=handle)
        assert await worker.process_once("worker-1")

    assert pushed
    assert plans.plans["plan-1"].status is PlanStatus.STALE
    assert repository.refs.get(branch) in (None, pushed[0])
    if branch in repository.refs:
        assert repository.file_text(branch, MANIFEST_PATH) == concurrent
    assert server.pull_requests == []
    # Only the reservation made before the commit remains: nothing was opened or committed.
    assert [(record.status, record.commit_sha) for record in pull_requests.records] in (
        [],
        [(PullRequestStatus.PENDING, None)],
    )


@pytest.mark.asyncio
async def test_exhausted_provider_call_budget_stops_the_operation_and_fails_closed():
    repository = FakeGitRepository.with_files(_repository_files(20))
    server = FakeGitHubServer(repository)
    worker, operations, plans, _ = _worker("github", _plan("github", repository.refs["main"]))
    worker.provider_call_budget_limit = 3

    with respx.mock(assert_all_called=False) as router:
        router.route(url__startswith=server.base_url).mock(side_effect=server.handle)
        assert await worker.process_once("worker-1")

    assert server.calls() == 3
    assert server.pull_requests == []
    assert operations.failures and "call budget" in operations.failures[0][0]
    assert operations.failures[0][1] is False
    assert plans.plans["plan-1"].status is PlanStatus.FAILED

    from greenkube.api.metrics_endpoint import get_metrics_output

    assert 'greenkube_git_provider_budget_exhausted_total{operation="plan_apply"}' in get_metrics_output().decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["github", "gitlab", "gitea"])
async def test_missing_plan_branch_is_a_mismatch_instead_of_a_provider_failure(provider):
    """An adopted pull request whose branch was deleted must ask for a re-plan, as before D5."""
    from greenkube.automation.plan_worker import _Execution

    repository = FakeGitRepository.with_files(_repository_files(20))
    server = SERVERS[provider](repository)
    plan = _plan(provider, repository.refs["main"])
    worker, operations, _, _ = _worker(provider, plan)

    with respx.mock(assert_all_called=False) as router:
        router.route(url__startswith=server.base_url).mock(side_effect=server.handle)
        target = await worker.provider_resolver.resolve(plan.source)
        state = await worker._branch_state(_Execution(operations.leases[0]), target, plan_branch_name(plan), plan)

    assert state == "mismatch"
