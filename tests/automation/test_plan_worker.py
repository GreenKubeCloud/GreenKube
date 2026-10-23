from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Sequence, cast

import pytest

from greenkube.automation.git.base import (
    EMPTY_PULL_REQUEST_OPTIONS,
    GitCommitRequest,
    GitCommitResult,
    GitConflictError,
    GitFile,
    GitProvider,
    GitProviderCapability,
    GitProviderCapabilityError,
    GitProviderError,
    GitProviderTransientError,
    GitRepository,
    PullRequestInfo,
    PullRequestOptions,
    pull_request_option_capabilities,
)
from greenkube.automation.git.registry import GitProviderRegistry, ProviderAuthorizationError
from greenkube.automation.plan_worker import (
    GitProviderTarget,
    PlanWorker,
    PlanWorkerError,
    RegistryPlanProviderResolver,
    plan_branch_name,
)
from greenkube.automation.pr_lifecycle import PullRequestLifecycleOutcome, PullRequestLifecycleReconciler
from greenkube.automation.rollback import ONE_TIME_HELM_VALUES_MAPPING_EVIDENCE_KEY, inverse_plan_id
from greenkube.automation.sync_runtime import GitOpsSyncRuntime
from greenkube.core.config import Config
from greenkube.core.optimization.applied_detector import AppliedDetector, WorkloadSnapshot
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
from greenkube.models.metrics import (
    PullRequestRecord,
    PullRequestStatus,
    RecommendationRecord,
    RecommendationStatus,
    RecommendationType,
)
from greenkube.storage.base_gitops_repository import (
    GitOpsProviderConfig,
    GitOpsProviderRepository,
    WorkloadBinding,
)
from greenkube.storage.base_plan_operation_repository import PlanOperationLease, PlanOperationLeaseError

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
BASE_REVISION = "a" * 40
PLAN_DIGEST = "sha256:" + "d" * 64
OUTPUT_DIGEST = "sha256:" + "e" * 64
MANIFEST_PATH = "apps/payments/deployment.yaml"
NEW_PATH = "apps/payments/policy.yaml"
BASE_CONTENT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: payments
spec:
  template:
    spec:
      containers:
        - name: api
          image: example/api:v1
"""
UPDATED_CONTENT = BASE_CONTENT.replace("example/api:v1", "example/api:v2")
NEW_CONTENT = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: policy\n"


def _blob_sha(content: str) -> str:
    payload = content.encode()
    return hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest()


def _source(**overrides: Any) -> WorkloadSource:
    values = {
        "id": "source-1",
        "cluster": "prod",
        "namespace": "payments",
        "workload_kind": "Deployment",
        "workload_name": "api",
        "provider_id": "github-main",
        "repo_url": "https://github.com/acme/gitops.git",
        "ref": "main",
        "revision": BASE_REVISION,
        "path": "apps/payments",
        "controller_kind": ControllerKind.ARGOCD,
        "controller_name": "payments-prod",
        "controller_namespace": "argocd",
        "source_revision": BASE_REVISION,
        "renderer": RendererKind.RAW,
        "renderer_config": {},
        "confidence": 0.98,
        "status": BindingStatus.RESOLVED,
        "evidence": {},
    }
    values.update(overrides)
    return WorkloadSource(**values)


def _plan(
    *,
    status: PlanStatus = PlanStatus.APPROVED,
    expires_at: datetime | None = None,
    source: WorkloadSource | None = None,
) -> Plan:
    action = ActionPlanV3(
        action_id="plan-1",
        recommendation_ids=[41, 42],
        target=ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="payments", name="api"),
        operations=[
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"cpu": "300m"}),
            )
        ],
        evidence_ref="recommendation/41",
    )
    validation = ValidationResult(
        target_changed=True,
        unrelated_changes=False,
        requests_within_limits=True,
        renderer=RendererKind.RAW,
        output_digest=OUTPUT_DIGEST,
    )
    file_plan = FilePlan(
        plan_id=action.action_id,
        files=[
            FileEdit(
                path=MANIFEST_PATH,
                operation="update",
                original_sha=_blob_sha(BASE_CONTENT),
                content=UPDATED_CONTENT,
            ),
            FileEdit(path=NEW_PATH, operation="create", content=NEW_CONTENT),
        ],
        diff="diff --git ...",
        rendered_diff="rendered diff",
        base_revision=BASE_REVISION,
        source_revision=BASE_REVISION,
        renderer=RendererKind.RAW,
        validation=validation,
        digest=PLAN_DIGEST,
    )
    return Plan(
        plan_id=action.action_id,
        recommendation_ids=action.recommendation_ids,
        source=source or _source(),
        action=action,
        file_plan=file_plan,
        status=status,
        digest=PLAN_DIGEST,
        created_at=NOW - timedelta(minutes=5),
        expires_at=expires_at if expires_at is not None else NOW + timedelta(minutes=25),
    )


def _operation_lease(*, digest: str = PLAN_DIGEST, worker_id: str = "worker-1", attempt: int = 1) -> PlanOperationLease:
    operation = Operation(
        operation_id="operation-1",
        plan_id="plan-1",
        idempotency_key=f"plan:plan-1:{digest}",
        status=OperationStatus.RUNNING,
        attempts=attempt,
        worker_id=worker_id,
        provider_id="github-main",
    )
    return PlanOperationLease(
        operation=operation,
        lease_id=f"lease-{attempt}",
        lease_expires_at=NOW + timedelta(minutes=15),
    )


def _workload_snapshot(
    *,
    uid: str | None = "uid-api-1",
    generation: int | None = 7,
    observed_generation: int | None = 7,
    cpu_request_millicores: int = 1000,
) -> WorkloadSnapshot:
    return WorkloadSnapshot(
        namespace="payments",
        kind="Deployment",
        name="api",
        cpu_request_millicores=cpu_request_millicores,
        memory_request_bytes=128 * 1024 * 1024,
        container_requests={
            "api": (cpu_request_millicores, 128 * 1024 * 1024),
            "sidecar": (100, 64 * 1024 * 1024),
        },
        uid=uid,
        generation=generation,
        observed_generation=observed_generation,
    )


class FakeWorkloadReader:
    def __init__(self, *responses: dict[tuple[str, str, str], WorkloadSnapshot]) -> None:
        self.responses = list(responses)
        self.read_count = 0

    async def read(self) -> dict[tuple[str, str, str], WorkloadSnapshot]:
        self.read_count += 1
        if not self.responses:
            return {}
        return self.responses[min(self.read_count - 1, len(self.responses) - 1)]


class FakeOperationRepository:
    def __init__(self, *leases: PlanOperationLease) -> None:
        self.leases = list(leases)
        self.claims: list[str] = []
        self.heartbeats: list[str] = []
        self.failures: list[tuple[str, bool]] = []
        self.successes: list[dict[str, Any]] = []
        self.heartbeat_error: Exception | None = None
        self.retry_exhausted = False

    async def get_by_operation_id(self, operation_id: str) -> Operation | None:
        return next(
            (lease.operation for lease in self.leases if lease.operation.operation_id == operation_id),
            None,
        )

    async def get_by_plan_id(self, plan_id: str) -> Operation | None:
        return next((lease.operation for lease in self.leases if lease.operation.plan_id == plan_id), None)

    async def claim_next(self, worker_id: str, *, lease_seconds: int = 900) -> PlanOperationLease | None:
        self.claims.append(worker_id)
        return self.leases.pop(0) if self.leases else None

    async def heartbeat(self, lease: PlanOperationLease, *, lease_seconds: int = 900) -> PlanOperationLease:
        self.heartbeats.append(lease.lease_id)
        if self.heartbeat_error is not None:
            raise self.heartbeat_error
        return replace(lease, lease_expires_at=NOW + timedelta(seconds=lease_seconds))

    async def mark_succeeded(
        self,
        lease: PlanOperationLease,
        *,
        provider_id: str | None = None,
        commit_sha: str | None = None,
        pull_request_number: int | None = None,
    ) -> Operation:
        self.successes.append(
            {
                "lease": lease,
                "provider_id": provider_id,
                "commit_sha": commit_sha,
                "pull_request_number": pull_request_number,
            }
        )
        return lease.operation.model_copy(update={"status": OperationStatus.SUCCEEDED})

    async def mark_failed(self, lease: PlanOperationLease, error: str, *, retryable: bool = True) -> Operation:
        self.failures.append((error, retryable))
        return lease.operation.model_copy(
            update={
                "status": OperationStatus.RETRYING if retryable and not self.retry_exhausted else OperationStatus.FAILED
            }
        )


class FakePlanRepository:
    def __init__(self, plan: Plan) -> None:
        self.plans = {plan.plan_id: plan}
        self.action_status = {
            recommendation_id: ActionStatus.PLAN_READY for recommendation_id in plan.recommendation_ids
        }
        self.status_updates: list[tuple[str, PlanStatus]] = []

    async def get_plan(self, plan_id: str) -> Plan | None:
        return self.plans.get(plan_id)

    async def set_plan_status(
        self,
        plan_id: str,
        status: PlanStatus,
        *,
        expected_statuses: Sequence[PlanStatus] | None = None,
        expected_digest: str | None = None,
        actor: str = "system",
        reason_code: str | None = None,
    ) -> Plan | None:
        plan = self.plans.get(plan_id)
        if plan is None:
            return None
        updated = plan.model_copy(update={"status": status})
        self.plans[plan_id] = updated
        self.status_updates.append((plan_id, status))
        return updated

    async def set_plan_pre_apply_baseline_if_absent(
        self,
        plan_id: str,
        baseline: PreApplyWorkloadBaseline,
    ) -> Plan | None:
        plan = self.plans.get(plan_id)
        if plan is None:
            return None
        if plan.pre_apply_baseline is None:
            plan = plan.model_copy(update={"pre_apply_baseline": baseline})
            self.plans[plan_id] = plan
        return plan

    async def set_recommendation_action_status(
        self,
        recommendation_ids: Sequence[int],
        status: ActionStatus,
        *,
        expected_statuses: Sequence[ActionStatus] | None = None,
        actor: str = "system",
        plan_id: str | None = None,
    ) -> int:
        for recommendation_id in recommendation_ids:
            self.action_status[recommendation_id] = status
        return len(recommendation_ids)


class FakePullRequestRepository:
    def __init__(self) -> None:
        self.records: list[PullRequestRecord] = []
        self._next_id = 1

    async def get_pull_requests_for_recommendation(self, recommendation_id: int) -> list[PullRequestRecord]:
        return [record for record in self.records if record.recommendation_id == recommendation_id]

    async def save_pull_request(self, record: PullRequestRecord) -> PullRequestRecord:
        saved = record.model_copy(update={"id": self._next_id})
        self._next_id += 1
        self.records.append(saved)
        return saved

    async def update_pull_request(
        self,
        pr_id: int,
        updates: dict[str, Any],
        *,
        expected_statuses: Sequence[str] | None = None,
        expected_plan_id: str | None = None,
    ) -> PullRequestRecord:
        for index, record in enumerate(self.records):
            if record.id == pr_id:
                updated = record.model_copy(update=updates)
                self.records[index] = updated
                return updated
        raise ValueError(f"pull request record {pr_id} not found")


class FakeGitProvider:
    name = "github"
    capabilities = frozenset(
        {
            GitProviderCapability.MULTI_FILE_COMMIT,
            GitProviderCapability.COMPLETE_FILE_TREE,
            GitProviderCapability.DRAFT_PULL_REQUEST,
            GitProviderCapability.LABELS,
            GitProviderCapability.REVIEWERS,
            GitProviderCapability.TEAM_REVIEWERS,
        }
    )
    author_name = "GreenKube Bot"
    author_email = "bot@greenkube.cloud"

    def __init__(self) -> None:
        self.repo = GitRepository(provider="github", owner="acme", name="gitops", default_branch="main")
        self.base_tree = {
            MANIFEST_PATH: GitFile(MANIFEST_PATH, BASE_CONTENT, _blob_sha(BASE_CONTENT), _blob_sha(BASE_CONTENT))
        }
        self.branches: dict[str, dict[str, GitFile]] = {}
        self.pull_requests: dict[tuple[str, str], PullRequestInfo] = {}
        self.branch_creations: list[tuple[str, str]] = []
        self.commit_requests: list[GitCommitRequest] = []
        self.pull_request_creations: list[tuple[str, str]] = []
        self.pull_request_bodies: list[str] = []
        self.pull_request_options: list[PullRequestOptions | None] = []
        self.pull_request_option_ensures: list[PullRequestOptions | None] = []
        self.pull_request_fail_mode: str | None = None
        self.next_pr_number = 17
        self.updated_files = 0
        self.plan_repository: FakePlanRepository | None = None
        self.plan_id: str | None = None
        self.mutation_baselines: list[PreApplyWorkloadBaseline | None] = []

    def supports(self, capability: GitProviderCapability | str) -> bool:
        return capability in self.capabilities

    def validate_pull_request_options(self, options: PullRequestOptions | None = None) -> PullRequestOptions:
        selected = options if options is not None else EMPTY_PULL_REQUEST_OPTIONS
        missing = pull_request_option_capabilities(selected) - self.capabilities
        if missing:
            names = ", ".join(sorted(capability.value for capability in missing))
            raise GitProviderCapabilityError(self.name, names)
        return selected

    async def get_file(self, repo: GitRepository, path: str, ref: str) -> GitFile | None:
        if ref in self.branches:
            tree = self.branches[ref]
        elif ref == BASE_REVISION:
            tree = self.base_tree
        else:
            tree = {}
        file = tree.get(path)
        return None if file is None else GitFile(file.path, file.content, file.sha, file.blob_sha)

    async def list_files(self, repo: GitRepository, ref: str) -> list[str]:
        if ref in self.branches:
            tree = self.branches[ref]
        elif ref == BASE_REVISION:
            tree = self.base_tree
        else:
            tree = {}
        return sorted(tree)

    async def create_branch(self, repo: GitRepository, branch: str, from_ref: str) -> None:
        if self.plan_repository is not None and self.plan_id is not None:
            plan = self.plan_repository.plans.get(self.plan_id)
            self.mutation_baselines.append(plan.pre_apply_baseline if plan is not None else None)
        self.branch_creations.append((branch, from_ref))
        if branch not in self.branches:
            if from_ref != "main":
                raise GitConflictError("unexpected branch base")
            self.branches[branch] = dict(self.base_tree)

    async def update_file(self, *args: Any, **kwargs: Any) -> None:
        self.updated_files += 1
        raise AssertionError("the plan worker must use one atomic commit, not update_file")

    async def create_commit(self, request: GitCommitRequest) -> GitCommitResult:
        self.commit_requests.append(request)
        branch_tree = self.branches[request.branch]
        if set(branch_tree) != set(self.base_tree) or any(
            branch_tree[path].content != file.content for path, file in self.base_tree.items()
        ):
            raise GitConflictError("branch no longer equals the planned base tree")
        for edit in request.files:
            if edit.action == "delete":
                branch_tree.pop(edit.path, None)
            else:
                assert edit.content is not None
                branch_tree[edit.path] = GitFile(
                    edit.path,
                    edit.content,
                    _blob_sha(edit.content),
                    _blob_sha(edit.content),
                )
        return GitCommitResult("f" * 40, request.branch, [edit.path for edit in request.files])

    async def find_pull_request(self, repo: GitRepository, head: str, base: str) -> PullRequestInfo | None:
        return self.pull_requests.get((head, base))

    async def create_pull_request(
        self,
        repo: GitRepository,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
        options: PullRequestOptions | None = None,
    ) -> PullRequestInfo:
        self.validate_pull_request_options(options)
        self.pull_request_creations.append((head, base))
        self.pull_request_bodies.append(body)
        self.pull_request_options.append(options)
        if self.pull_request_fail_mode == "before":
            self.pull_request_fail_mode = None
            raise GitProviderTransientError("simulated timeout before PR creation")
        existing = self.pull_requests.get((head, base))
        if existing is not None:
            raise GitConflictError("matching pull request already exists")
        pull_request = PullRequestInfo(
            number=self.next_pr_number,
            url=f"https://github.com/acme/gitops/pull/{self.next_pr_number}",
            status="open",
            raw={"head": {"ref": head, "sha": "f" * 40}, "base": {"ref": base}},
        )
        self.next_pr_number += 1
        self.pull_requests[(head, base)] = pull_request
        if self.pull_request_fail_mode == "after":
            self.pull_request_fail_mode = None
            raise GitProviderTransientError("simulated timeout after PR creation")
        if self.pull_request_fail_mode == "conflict-after":
            self.pull_request_fail_mode = None
            raise GitProviderError("matching pull request already exists")
        return pull_request

    async def ensure_pull_request_options(
        self,
        repo: GitRepository,
        pull_request: PullRequestInfo,
        options: PullRequestOptions | None = None,
    ) -> None:
        self.validate_pull_request_options(options)
        self.pull_request_option_ensures.append(options)

    async def close(self) -> None:
        return None


class FakeGiteaProvider(FakeGitProvider):
    name = "gitea"
    capabilities = frozenset(
        capability
        for capability in FakeGitProvider.capabilities
        if capability is not GitProviderCapability.DRAFT_PULL_REQUEST
    )

    def __init__(self) -> None:
        super().__init__()
        self.repo = GitRepository(provider="gitea", owner="acme", name="gitops", default_branch="main")


class FakeProviderResolver:
    def __init__(self, provider: FakeGitProvider, *, error: Exception | None = None) -> None:
        self.provider = provider
        self.error = error
        self.calls: list[WorkloadSource] = []

    async def resolve(self, source: WorkloadSource) -> GitProviderTarget:
        self.calls.append(source)
        if self.error is not None:
            raise self.error
        return GitProviderTarget(self.provider, self.provider.repo)


class FakeRevisionResolver:
    def __init__(self, revision: str | None = BASE_REVISION) -> None:
        self.revision = revision
        self.calls: list[WorkloadSource] = []

    async def resolve_current_revision(self, source: WorkloadSource) -> str | None:
        self.calls.append(source)
        return self.revision


class FakeGitOpsProviderRepository:
    async def get_provider_config(self, provider_id: str) -> GitOpsProviderConfig | None:
        if provider_id != "github-main":
            return None
        return GitOpsProviderConfig(id=provider_id, provider_type="github")


class FakeRegistry:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, GitRepository]] = []

    async def get_provider(self, provider_id: str, repository: GitRepository) -> FakeGitProvider:
        self.calls.append((provider_id, repository))
        if self.error is not None:
            raise self.error
        return FakeGitProvider()


class FakeAuthorizedAdapter(GitProvider):
    name = "github"
    capabilities = frozenset({GitProviderCapability.MULTI_FILE_COMMIT})
    lookup_calls: list[tuple[str, str]] = []

    async def get_default_branch(self, repo: GitRepository) -> str:
        return "main"

    async def get_file(self, repo: GitRepository, path: str, ref: str) -> GitFile | None:
        return None

    async def list_files(self, repo: GitRepository, ref: str) -> list[str]:
        return []

    async def create_branch(self, repo: GitRepository, branch: str, from_ref: str) -> None:
        return None

    async def update_file(
        self,
        repo: GitRepository,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> None:
        return None

    async def create_pull_request(
        self,
        repo: GitRepository,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
        options: PullRequestOptions | None = None,
    ) -> PullRequestInfo:
        return PullRequestInfo(number=1, url="https://github.com/acme/gitops/pull/1", status="open")

    async def find_pull_request(self, repo: GitRepository, head: str, base: str) -> PullRequestInfo | None:
        self.lookup_calls.append((head, base))
        return None

    async def get_pull_request(self, repo: GitRepository, number: int) -> PullRequestInfo:
        return PullRequestInfo(number=number, url=f"https://github.com/acme/gitops/pull/{number}", status="open")


class FakeSecretResolver:
    async def resolve_secret(self, *, namespace: str | None, name: str, key: str) -> str:
        return "fake-token"


class AuthorizedGitOpsProviderRepository(FakeGitOpsProviderRepository):
    async def get_provider_config(self, provider_id: str) -> GitOpsProviderConfig | None:
        if provider_id != "github-main":
            return None
        return GitOpsProviderConfig(
            id=provider_id,
            provider_type="github",
            secret_namespace="gitops",
            secret_name="gitops-provider",
            secret_key="token",
        )

    async def list_provider_allowlists(self, provider_id: str) -> list[str]:
        return ["acme/*"]


def _worker(
    *,
    plan: Plan | None = None,
    leases: tuple[PlanOperationLease, ...] | None = None,
    provider: FakeGitProvider | None = None,
    revision: str | None = BASE_REVISION,
    provider_error: Exception | None = None,
    workload_reader: FakeWorkloadReader | None = None,
    pull_request_options: PullRequestOptions | None = None,
) -> tuple[PlanWorker, FakeOperationRepository, FakePlanRepository, FakePullRequestRepository, FakeGitProvider]:
    selected_plan = plan or _plan()
    selected_provider = provider or FakeGitProvider()
    operations = FakeOperationRepository(*(leases or (_operation_lease(),)))
    plan_repository = FakePlanRepository(selected_plan)
    selected_provider.plan_repository = plan_repository
    selected_provider.plan_id = selected_plan.plan_id
    pull_requests = FakePullRequestRepository()
    selected_workload_reader = workload_reader or FakeWorkloadReader(
        {("payments", "Deployment", "api"): _workload_snapshot()}
    )
    worker = PlanWorker(
        operations=operations,
        plans=plan_repository,
        provider_resolver=FakeProviderResolver(selected_provider, error=provider_error),
        revision_resolver=FakeRevisionResolver(revision),
        pull_request_repository=pull_requests,
        clock=lambda: NOW,
        lease_seconds=900,
        workload_reader=selected_workload_reader,
        pull_request_options=pull_request_options,
    )
    return worker, operations, plan_repository, pull_requests, selected_provider


@pytest.mark.asyncio
async def test_commits_all_file_plan_edits_atomically_and_records_one_pr_per_action():
    worker, operations, plans, pull_requests, provider = _worker()

    assert await worker.process_once("worker-1")

    branch = plan_branch_name(_plan())
    assert provider.branch_creations == [(branch, "main")]
    assert len(provider.commit_requests) == 1
    commit = provider.commit_requests[0]
    assert commit.branch == branch
    assert commit.base_revision == BASE_REVISION
    assert [(edit.path, edit.action, edit.content) for edit in commit.files] == [
        (MANIFEST_PATH, "update", UPDATED_CONTENT),
        (NEW_PATH, "create", NEW_CONTENT),
    ]
    assert provider.updated_files == 0
    assert provider.pull_request_creations == [(branch, "main")]
    assert len(provider.pull_requests) == 1
    assert {record.recommendation_id for record in pull_requests.records} == {41, 42}
    assert all(record.pr_number == 17 and record.status is PullRequestStatus.OPEN for record in pull_requests.records)
    assert all(record.provider_id == "github-main" for record in pull_requests.records)
    assert all(record.plan_id == "plan-1" for record in pull_requests.records)
    assert all(record.commit_sha == "f" * 40 for record in pull_requests.records)
    assert plans.plans["plan-1"].status is PlanStatus.PR_OPEN
    assert set(plans.action_status.values()) == {ActionStatus.PR_OPEN}
    assert operations.successes[0]["provider_id"] == "github-main"
    assert operations.successes[0]["commit_sha"] == "f" * 40
    assert operations.successes[0]["pull_request_number"] == 17
    from greenkube.api.metrics_endpoint import get_metrics_output

    output = get_metrics_output().decode()
    assert 'greenkube_automation_operations_total{status="succeeded",type="apply"}' in output
    assert 'greenkube_automation_operation_duration_seconds_count{type="apply"}' in output
    assert "greenkube_automation_queue_age_seconds_count" in output


@pytest.mark.asyncio
async def test_worker_stales_approved_plan_when_binding_generation_changed():
    source = _source(evidence={"binding_generation": 2})
    worker, _, plans, _, provider = _worker(plan=_plan(source=source))
    current_source = source.model_copy(update={"evidence": {"binding_generation": 3}})

    class Bindings:
        async def get_workload_binding(self, cluster, namespace, workload_kind, workload_name):
            return WorkloadBinding(
                cluster=cluster,
                namespace=namespace,
                workload_kind=workload_kind,
                workload_name=workload_name,
                status=BindingStatus.RESOLVED,
                confidence=1,
                source=current_source,
                generation=3,
            )

    worker.binding_reader = Bindings()

    assert await worker.process_once("worker-1")

    assert plans.plans["plan-1"].status is PlanStatus.STALE
    assert set(plans.action_status.values()) == {ActionStatus.NONE}
    assert provider.branch_creations == []


@pytest.mark.asyncio
async def test_worker_keeps_binding_cas_with_a_plan_only_helm_mapping():
    mapping = {
        "file_path": "apps/payments/values.yaml",
        "container": "api",
        "path": ["app", "resources"],
    }
    plan_source = _source(
        renderer=RendererKind.HELM_VALUES,
        renderer_config={"values_files": [mapping["file_path"]], "values_mapping": mapping},
        evidence={"binding_generation": 2, ONE_TIME_HELM_VALUES_MAPPING_EVIDENCE_KEY: True},
    )
    current_source = _source(
        renderer=RendererKind.HELM_VALUES,
        renderer_config={"values_files": [mapping["file_path"]]},
        evidence={"binding_generation": 2},
    )
    plan = _plan(source=plan_source)
    worker, _, _, _, _ = _worker(plan=plan)

    class Bindings:
        generation = 2
        source = current_source

        async def get_workload_binding(self, cluster, namespace, workload_kind, workload_name):
            return WorkloadBinding(
                cluster=cluster,
                namespace=namespace,
                workload_kind=workload_kind,
                workload_name=workload_name,
                status=BindingStatus.RESOLVED,
                confidence=1,
                source=self.source,
                generation=self.generation,
            )

    bindings = Bindings()
    worker.binding_reader = bindings

    await worker._validate_current_binding(plan)

    bindings.source = current_source.model_copy(update={"evidence": {"binding_generation": 3}})
    bindings.generation = 3
    with pytest.raises(PlanWorkerError, match="binding changed"):
        await worker._validate_current_binding(plan)


@pytest.mark.asyncio
async def test_pull_request_body_includes_evidence_impact_risk_diff_and_provenance():
    plan = _plan(source=_source(source_revision=f"main@sha1:{'b' * 40}"))
    worker, _, _, _, provider = _worker(plan=plan)
    recommendation = RecommendationRecord(
        id=41,
        pod_name="api",
        container_name="api",
        namespace="payments",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="CPU request exceeds observed usage.",
        reason="Observed p95 CPU is below the recommended request.",
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        potential_savings_cost=12.5,
        potential_savings_co2e_grams=42.0,
        confidence=0.92,
        risk_level="low",
        risk_factors=["p95 usage remains below target"],
        evidence={
            "observation_window_start": NOW - timedelta(days=7),
            "observation_window_end": NOW,
            "sample_count": 96,
            "coverage_ratio": 0.98,
            "current": {"cpu_request_millicores": 500},
            "proposed": {"cpu_request_millicores": 300},
            "cpu_usage": {
                "avg": 90,
                "p50": 80,
                "p90": 110,
                "p95": 120,
                "p99": 180,
                "max": 200,
                "sample_count": 96,
                "coverage_ratio": 0.98,
            },
            "rollback_conditions": [
                {
                    "metric": "cpu_p95_millicores",
                    "comparator": "gt",
                    "threshold": 330,
                    "description": "Review if CPU p95 exceeds 330m.",
                }
            ],
        },
    )

    class RecommendationReader:
        async def get_recommendation_by_id(self, recommendation_id: int) -> RecommendationRecord | None:
            return recommendation if recommendation_id == 41 else None

    worker.recommendation_repository = cast(Any, RecommendationReader())

    assert await worker.process_once("worker-1")

    body = provider.pull_request_bodies[0]
    assert "## Plan and provenance" in body
    assert "Repository: `acme/gitops`" in body
    assert "Revision drift: this plan targets branch head" in body
    assert "## Recommendation evidence and impact" in body
    assert "Projected annual savings: cost $12.50/year, carbon 42.000 g CO2e/year" in body
    assert "CPU usage: p95 120m, max 200m" in body
    assert "Risk factors: p95 usage remains below target" in body
    assert "Review if CPU p95 exceeds 330m." in body
    assert "## Source diff" in body
    assert "## Rendered diff" in body
    assert "## Verification" in body
    assert "Recommendation evidence is unavailable for #42" in body


@pytest.mark.asyncio
async def test_worker_forwards_pull_request_options_without_replacing_the_rich_body():
    options = PullRequestOptions(
        draft=True,
        labels=("release-review",),
        reviewers=("alice",),
        team_reviewers=("platform",),
    )
    worker, _, _, _, provider = _worker(pull_request_options=options)

    assert await worker.process_once("worker-1")

    assert provider.pull_request_options == [options]
    body = provider.pull_request_bodies[0]
    assert "## Plan and provenance" in body
    assert "## Recommendation evidence and impact" in body
    assert "## Rendered diff" in body
    assert "## Verification" in body


@pytest.mark.asyncio
async def test_worker_rejects_unsupported_pr_options_before_repository_mutation():
    provider = FakeGitProvider()
    provider.capabilities = frozenset(
        {
            GitProviderCapability.MULTI_FILE_COMMIT,
            GitProviderCapability.COMPLETE_FILE_TREE,
            GitProviderCapability.DRAFT_PULL_REQUEST,
            GitProviderCapability.LABELS,
            GitProviderCapability.REVIEWERS,
        }
    )
    worker, _, plans, _, _ = _worker(
        provider=provider,
        pull_request_options=PullRequestOptions(team_reviewers=("platform",)),
    )

    assert await worker.process_once("worker-1")

    assert provider.branch_creations == []
    assert provider.commit_requests == []
    assert provider.pull_request_creations == []
    assert plans.plans["plan-1"].status is PlanStatus.FAILED


@pytest.mark.asyncio
async def test_worker_persists_live_pre_apply_baseline_on_the_plan():
    reader = FakeWorkloadReader({("payments", "Deployment", "api"): _workload_snapshot()})
    worker, _, plans, _, provider = _worker(workload_reader=reader)

    assert await worker.process_once("worker-1")

    assert reader.read_count == 1
    baseline = plans.plans["plan-1"].pre_apply_baseline
    assert baseline is not None
    assert baseline.workload_uid == "uid-api-1"
    assert baseline.generation == 7
    assert baseline.observed_generation == 7
    assert baseline.container_requests["api"].cpu_millicores == 1000
    assert baseline.container_requests["sidecar"].memory_bytes == 64 * 1024 * 1024
    assert provider.mutation_baselines == [baseline]


@pytest.mark.asyncio
async def test_missing_live_baseline_is_retryable_and_prevents_provider_mutations():
    reader = FakeWorkloadReader({})
    worker, operations, plans, _, provider = _worker(workload_reader=reader)

    assert await worker.process_once("worker-1")

    assert operations.failures and operations.failures[0][1] is True
    assert "pre-apply workload baseline" in operations.failures[0][0].lower()
    assert "retry" in operations.failures[0][0].lower()
    assert plans.plans["plan-1"].status is PlanStatus.APPROVED
    assert plans.plans["plan-1"].pre_apply_baseline is None
    assert provider.branch_creations == []
    assert provider.commit_requests == []
    assert provider.pull_request_creations == []


@pytest.mark.asyncio
async def test_invalid_zero_generation_baseline_is_retryable_and_never_defaulted():
    reader = FakeWorkloadReader(
        {("payments", "Deployment", "api"): _workload_snapshot(generation=0, observed_generation=0)}
    )
    worker, operations, plans, _, provider = _worker(workload_reader=reader)

    assert await worker.process_once("worker-1")

    assert operations.failures and operations.failures[0][1] is True
    assert "generation" in operations.failures[0][0].lower()
    assert plans.plans["plan-1"].pre_apply_baseline is None
    assert provider.branch_creations == []
    assert provider.commit_requests == []


@pytest.mark.asyncio
async def test_retry_reuses_immutable_pre_apply_baseline():
    provider = FakeGitProvider()
    provider.pull_request_fail_mode = "before"
    reader = FakeWorkloadReader(
        {("payments", "Deployment", "api"): _workload_snapshot(generation=7)},
        {("payments", "Deployment", "api"): _workload_snapshot(uid="uid-api-2", generation=20)},
    )
    worker, operations, plans, _, _ = _worker(
        provider=provider,
        leases=(_operation_lease(attempt=1), _operation_lease(attempt=2)),
        workload_reader=reader,
    )

    assert await worker.process_once("worker-1")
    first_baseline = plans.plans["plan-1"].pre_apply_baseline
    assert first_baseline is not None
    assert first_baseline.workload_uid == "uid-api-1"

    assert await worker.process_once("worker-1")

    assert reader.read_count == 1
    assert plans.plans["plan-1"].pre_apply_baseline == first_baseline
    assert plans.plans["plan-1"].status is PlanStatus.PR_OPEN
    assert len(provider.commit_requests) == 1


@pytest.mark.asyncio
async def test_worker_refuses_rollback_when_any_linked_recommendation_left_rollback_review():
    template_plan = _plan()
    assert template_plan.file_plan is not None
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
    assert original.digest is not None
    rollback_id = inverse_plan_id(original.plan_id, original.digest, template_plan.file_plan.base_revision)
    rollback = template_plan.model_copy(
        update={
            "plan_id": rollback_id,
            "file_plan": template_plan.file_plan.model_copy(update={"plan_id": rollback_id}),
            "rollback_of_plan_id": "original-plan",
            "pre_apply_baseline": PreApplyWorkloadBaseline(
                workload_uid="uid-api-1",
                generation=8,
                observed_generation=8,
                container_requests={"api": {"cpu_millicores": 300, "memory_bytes": 128 * 1024 * 1024}},
            ),
        }
    )
    worker, _, plan_repository, _, provider = _worker(plan=rollback)
    plan_repository.plans[original.plan_id] = original
    recommendations = {
        recommendation_id: SimpleNamespace(
            id=recommendation_id,
            status=(RecommendationStatus.ROLLBACK_REVIEW if recommendation_id == 41 else RecommendationStatus.APPLIED),
            scope="workload",
            action=rollback.action,
            namespace=rollback.action.target.namespace,
            owner_kind=rollback.action.target.kind,
            owner_name=rollback.action.target.name,
            container_name="api",
        )
        for recommendation_id in rollback.recommendation_ids
    }

    class RecommendationReader:
        async def get_recommendation_by_id(self, recommendation_id: int):
            return recommendations.get(recommendation_id)

    worker.recommendation_repository = cast(Any, RecommendationReader())

    with pytest.raises(PlanWorkerError, match="remain in rollback_review"):
        await worker._validate_rollback_review(rollback)

    recommendations[42].status = RecommendationStatus.ROLLBACK_REVIEW
    await worker._validate_rollback_review(rollback)
    assert provider.branch_creations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["before", "after"])
async def test_retry_after_pr_failure_reuses_branch_and_adopts_pr_without_duplicate_commit(failure_mode: str):
    provider = FakeGitProvider()
    provider.pull_request_fail_mode = failure_mode
    first_lease = _operation_lease(attempt=1)
    second_lease = _operation_lease(attempt=2)
    worker, operations, plans, pull_requests, _ = _worker(
        provider=provider,
        leases=(first_lease, second_lease),
    )

    assert await worker.process_once("worker-1")
    assert operations.failures and operations.failures[0][1] is True
    assert plans.plans["plan-1"].status is PlanStatus.APPLYING

    assert await worker.process_once("worker-1")

    assert len(provider.commit_requests) == 1
    assert provider.pull_request_creations == [(plan_branch_name(_plan()), "main")] * (
        2 if failure_mode == "before" else 1
    )
    assert len(provider.pull_requests) == 1
    assert len(operations.successes) == 1
    assert len(pull_requests.records) == 2
    assert len({(record.recommendation_id, record.pr_number) for record in pull_requests.records}) == 2
    assert plans.plans["plan-1"].status is PlanStatus.PR_OPEN
    assert set(plans.action_status.values()) == {ActionStatus.PR_OPEN}
    assert len(provider.pull_request_option_ensures) == (1 if failure_mode == "after" else 0)


@pytest.mark.asyncio
async def test_existing_matching_pr_is_adopted_only_when_branch_tree_matches_plan():
    provider = FakeGitProvider()
    branch = plan_branch_name(_plan())
    provider.branches[branch] = {
        MANIFEST_PATH: GitFile(MANIFEST_PATH, UPDATED_CONTENT, _blob_sha(UPDATED_CONTENT), _blob_sha(UPDATED_CONTENT)),
        NEW_PATH: GitFile(NEW_PATH, NEW_CONTENT, _blob_sha(NEW_CONTENT), _blob_sha(NEW_CONTENT)),
    }
    provider.pull_requests[(branch, "main")] = PullRequestInfo(
        number=23,
        url="https://github.com/acme/gitops/pull/23",
        status="open",
        raw={"head": {"ref": branch, "sha": "f" * 40}, "base": {"ref": "main"}},
    )
    worker, operations, plans, pull_requests, _ = _worker(
        plan=_plan(status=PlanStatus.APPLYING).model_copy(
            update={
                "pre_apply_baseline": PreApplyWorkloadBaseline(
                    workload_uid="uid-api-1",
                    generation=7,
                    observed_generation=7,
                    container_requests={"api": {"cpu_millicores": 1000, "memory_bytes": 128 * 1024 * 1024}},
                )
            }
        ),
        provider=provider,
    )

    assert await worker.process_once("worker-1")

    assert provider.branch_creations == []
    assert provider.commit_requests == []
    assert provider.pull_request_creations == []
    assert provider.pull_request_option_ensures == [EMPTY_PULL_REQUEST_OPTIONS]
    assert operations.successes[0]["pull_request_number"] == 23
    assert {record.pr_number for record in pull_requests.records} == {23}
    assert plans.plans["plan-1"].status is PlanStatus.PR_OPEN


@pytest.mark.asyncio
async def test_existing_pr_with_unverified_branch_is_not_adopted_or_overwritten():
    provider = FakeGitProvider()
    branch = plan_branch_name(_plan())
    provider.branches[branch] = {MANIFEST_PATH: GitFile(MANIFEST_PATH, "unexpected change", "c" * 40, "c" * 40)}
    provider.pull_requests[(branch, "main")] = PullRequestInfo(
        number=24,
        url="https://github.com/acme/gitops/pull/24",
        status="open",
        raw={"head": {"ref": branch}, "base": {"ref": "main"}},
    )
    worker, operations, plans, pull_requests, _ = _worker(
        plan=_plan(status=PlanStatus.APPLYING).model_copy(
            update={
                "pre_apply_baseline": PreApplyWorkloadBaseline(
                    workload_uid="uid-api-1",
                    generation=7,
                    observed_generation=7,
                    container_requests={"api": {"cpu_millicores": 1000, "memory_bytes": 128 * 1024 * 1024}},
                )
            }
        ),
        provider=provider,
    )

    assert await worker.process_once("worker-1")

    assert provider.branch_creations == []
    assert provider.commit_requests == []
    assert provider.pull_request_creations == []
    assert len(pull_requests.records) == 2
    assert all(record.plan_id == "plan-1" for record in pull_requests.records)
    assert all(record.provider_id == "github-main" for record in pull_requests.records)
    assert all(record.head_branch == branch and record.pr_number is None for record in pull_requests.records)
    assert operations.failures and operations.failures[0][1] is False
    assert plans.plans["plan-1"].status is PlanStatus.STALE
    assert set(plans.action_status.values()) == {ActionStatus.NONE}


@pytest.mark.asyncio
async def test_provider_duplicate_pr_error_adopts_the_matching_request():
    provider = FakeGitProvider()
    provider.pull_request_fail_mode = "conflict-after"
    worker, operations, plans, pull_requests, _ = _worker(provider=provider)

    assert await worker.process_once("worker-1")

    assert len(provider.commit_requests) == 1
    assert provider.pull_request_creations == [(plan_branch_name(_plan()), "main")]
    assert len(provider.pull_requests) == 1
    assert len(operations.successes) == 1
    assert len(pull_requests.records) == 2
    assert plans.plans["plan-1"].status is PlanStatus.PR_OPEN


@pytest.mark.asyncio
async def test_unexpected_preexisting_branch_fails_closed_without_commit_or_pr():
    provider = FakeGitProvider()
    branch = plan_branch_name(_plan())
    provider.branches[branch] = {MANIFEST_PATH: GitFile(MANIFEST_PATH, "unexpected change", "c" * 40, "c" * 40)}
    worker, operations, plans, _, _ = _worker(provider=provider)

    assert await worker.process_once("worker-1")

    assert provider.branch_creations == [(branch, "main")]
    assert provider.commit_requests == []
    assert provider.pull_request_creations == []
    assert operations.failures and operations.failures[0][1] is False
    assert plans.plans["plan-1"].status is PlanStatus.STALE
    assert set(plans.action_status.values()) == {ActionStatus.NONE}


@pytest.mark.asyncio
async def test_expired_plan_is_marked_stale_before_any_provider_or_git_calls():
    provider = FakeGitProvider()
    worker, operations, plans, _, _ = _worker(
        plan=_plan(expires_at=NOW - timedelta(seconds=1)),
        provider=provider,
    )

    assert await worker.process_once("worker-1")

    assert provider.branch_creations == []
    assert provider.commit_requests == []
    assert operations.failures and operations.failures[0][1] is False
    assert plans.plans["plan-1"].status is PlanStatus.STALE
    assert set(plans.action_status.values()) == {ActionStatus.NONE}


@pytest.mark.asyncio
async def test_changed_source_revision_requires_replan_before_git_calls():
    provider = FakeGitProvider()
    worker, operations, plans, _, _ = _worker(provider=provider, revision="b" * 40)

    assert await worker.process_once("worker-1")

    assert provider.branch_creations == []
    assert provider.commit_requests == []
    assert operations.failures and operations.failures[0][1] is False
    assert plans.plans["plan-1"].status is PlanStatus.STALE
    assert set(plans.action_status.values()) == {ActionStatus.NONE}


@pytest.mark.asyncio
async def test_plan_staled_during_revision_check_is_not_resurrected():
    worker, operations, plans, _, provider = _worker()

    class StalingRevisionResolver:
        async def resolve_current_revision(self, source: WorkloadSource) -> str:
            await plans.set_plan_status("plan-1", PlanStatus.STALE)
            await plans.set_recommendation_action_status([41, 42], ActionStatus.NONE)
            return BASE_REVISION

    worker.revision_resolver = StalingRevisionResolver()

    assert await worker.process_once("worker-1")

    assert plans.plans["plan-1"].status is PlanStatus.STALE
    assert set(plans.action_status.values()) == {ActionStatus.NONE}
    assert provider.branch_creations == []
    assert operations.failures and operations.failures[0][1] is False


@pytest.mark.asyncio
async def test_retryable_error_marks_plan_and_action_failed_after_queue_exhaustion():
    worker, operations, plans, _, provider = _worker(revision=None)
    operations.retry_exhausted = True

    assert await worker.process_once("worker-1")

    assert provider.branch_creations == []
    assert operations.failures and operations.failures[0][1] is True
    assert plans.plans["plan-1"].status is PlanStatus.FAILED
    assert set(plans.action_status.values()) == {ActionStatus.FAILED}
    from greenkube.api.metrics_endpoint import get_metrics_output

    output = get_metrics_output().decode()
    assert 'greenkube_automation_operations_total{status="failed",type="apply"}' in output
    assert 'greenkube_automation_failures_total{reason="worker"}' in output


@pytest.mark.asyncio
async def test_denied_repository_fails_before_git_calls_and_is_terminal():
    provider = FakeGitProvider()
    worker, operations, plans, _, _ = _worker(
        provider=provider,
        provider_error=ProviderAuthorizationError("repository denied"),
    )

    assert await worker.process_once("worker-1")

    assert provider.branch_creations == []
    assert provider.commit_requests == []
    assert operations.failures and operations.failures[0][1] is False
    assert plans.plans["plan-1"].status is PlanStatus.FAILED
    assert set(plans.action_status.values()) == {ActionStatus.FAILED}


@pytest.mark.asyncio
async def test_provider_without_complete_tree_listing_is_rejected_before_git_calls():
    provider = FakeGitProvider()
    provider.capabilities = frozenset({GitProviderCapability.MULTI_FILE_COMMIT})
    worker, operations, plans, _, _ = _worker(provider=provider)

    assert await worker.process_once("worker-1")

    assert provider.branch_creations == []
    assert provider.commit_requests == []
    assert provider.pull_request_creations == []
    assert operations.failures and operations.failures[0][1] is False
    assert plans.plans["plan-1"].status is PlanStatus.FAILED
    assert set(plans.action_status.values()) == {ActionStatus.FAILED}


@pytest.mark.asyncio
async def test_gitea_with_complete_tree_listing_can_apply_a_plan():
    provider = FakeGiteaProvider()
    source = _source(repo_url="http://gitea.local/acme/gitops.git")
    worker, operations, plans, _, _ = _worker(plan=_plan(source=source), provider=provider)

    assert await worker.process_once("worker-1")

    assert len(provider.commit_requests) == 1
    assert len(provider.pull_request_creations) == 1
    assert operations.successes
    assert plans.plans["plan-1"].status is PlanStatus.PR_OPEN
    assert set(plans.action_status.values()) == {ActionStatus.PR_OPEN}


@pytest.mark.asyncio
async def test_wrong_operation_digest_fails_terminal_before_provider_resolution():
    worker, operations, _, _, provider = _worker(leases=(_operation_lease(digest="sha256:" + "f" * 64),))

    assert await worker.process_once("worker-1")

    assert provider.branch_creations == []
    assert operations.failures and operations.failures[0][1] is False
    assert not operations.successes


@pytest.mark.asyncio
async def test_lease_fence_stops_execution_when_heartbeat_rejects_the_claim():
    worker, operations, _, _, provider = _worker()
    operations.heartbeat_error = PlanOperationLeaseError("lease was reclaimed")

    with pytest.raises(PlanOperationLeaseError):
        await worker.process_once("worker-1")

    assert provider.branch_creations == []
    assert operations.failures == []
    assert operations.successes == []


@pytest.mark.asyncio
async def test_registry_provider_resolver_authorizes_provider_and_repository_before_returning_target():
    source = _source()
    registry = FakeRegistry()
    provider_repository = FakeGitOpsProviderRepository()
    resolver = RegistryPlanProviderResolver(provider_repository, registry)

    target = await resolver.resolve(source)

    assert registry.calls == [
        ("github-main", GitRepository(provider="github", owner="acme", name="gitops", default_branch="main"))
    ]
    assert target.provider is not None
    assert target.repository.full_name == "acme/gitops"


@pytest.mark.asyncio
async def test_registry_provider_resolver_propagates_allowlist_denial_before_git_use():
    registry = FakeRegistry(error=ProviderAuthorizationError("repository denied"))
    resolver = RegistryPlanProviderResolver(FakeGitOpsProviderRepository(), registry)

    with pytest.raises(ProviderAuthorizationError):
        await resolver.resolve(_source())

    assert len(registry.calls) == 1
    assert registry.calls[0][1].full_name == "acme/gitops"
    assert "repository denied" in str(registry.error)


@pytest.mark.asyncio
async def test_authorized_registry_forwards_pr_lookup_through_the_allowlist_guard(monkeypatch: pytest.MonkeyPatch):
    from greenkube.automation.git import registry as registry_module

    FakeAuthorizedAdapter.lookup_calls.clear()
    monkeypatch.setitem(registry_module._PROVIDER_ADAPTERS, "github", FakeAuthorizedAdapter)
    provider_registry = GitProviderRegistry(
        cast(GitOpsProviderRepository, AuthorizedGitOpsProviderRepository()),
        FakeSecretResolver(),
    )
    repository = GitRepository(provider="github", owner="acme", name="gitops", default_branch="main")

    provider = await provider_registry.get_provider("github-main", repository)
    pull_request = await provider.find_pull_request(repository, "greenkube/plan/test", "main")

    assert pull_request is None
    assert FakeAuthorizedAdapter.lookup_calls == [("greenkube/plan/test", "main")]
    await provider.close()


@pytest.mark.asyncio
async def test_existing_worker_entry_point_can_process_injected_plan_worker():
    from greenkube.cli import automation_worker

    class OnePlanWorker:
        def __init__(self) -> None:
            self.worker_ids: list[str] = []

        async def process_once(self, worker_id: str) -> bool:
            self.worker_ids.append(worker_id)
            return True

    plan_worker = OnePlanWorker()

    assert await automation_worker.process_once(
        "worker-1",
        queue="plans",
        plan_worker=cast(PlanWorker, plan_worker),
    )
    assert plan_worker.worker_ids == ["worker-1"]


@pytest.mark.asyncio
async def test_worker_entry_point_defaults_to_the_plan_queue(monkeypatch: pytest.MonkeyPatch):
    from greenkube.cli import automation_worker

    class EmptyPlanWorker:
        worker_ids: list[str] = []

        async def process_once(self, worker_id: str) -> bool:
            self.worker_ids.append(worker_id)
            return False

    plan_worker = EmptyPlanWorker()

    async def create_runtime():
        return SimpleNamespace(worker=plan_worker)

    monkeypatch.setattr(automation_worker, "create_plan_runtime", create_runtime)

    assert not await automation_worker.process_once("plan-worker")
    assert plan_worker.worker_ids == ["plan-worker"]


@pytest.mark.asyncio
async def test_plan_worker_merge_sync_and_apply_form_one_closed_loop():
    plan = _plan()
    action = plan.action.model_copy(update={"recommendation_ids": [41]})
    plan = plan.model_copy(update={"recommendation_ids": [41], "action": action})
    worker, _, plans, pull_requests, _ = _worker(plan=plan)

    assert await worker.process_once("worker-1")
    assert plans.plans[plan.plan_id].status is PlanStatus.PR_OPEN
    pull_request = pull_requests.records[0]
    assert pull_request.provider_id is not None
    assert pull_request.pr_number is not None
    merge_sha = "f" * 40

    class ProviderState:
        async def get_pull_request(self, provider_id: str, repo: str, number: int) -> PullRequestInfo:
            assert provider_id == pull_request.provider_id
            assert repo == pull_request.repo
            assert number == pull_request.pr_number
            return PullRequestInfo(
                number=number,
                url="https://github.com/acme/gitops/pull/17",
                status="closed",
                merged=True,
                raw={
                    "state": "closed",
                    "merged": True,
                    "merge_commit_sha": merge_sha,
                    "merged_at": NOW.isoformat(),
                    "closed_at": NOW.isoformat(),
                },
            )

    class LifecycleAndSyncPullRequestRepository:
        async def list_pull_requests_by_provider_reference(
            self, provider_id: str, repo: str, pr_number: int
        ) -> list[PullRequestRecord]:
            return [
                record
                for record in pull_requests.records
                if (record.provider_id, record.repo, record.pr_number) == (provider_id, repo, pr_number)
            ]

        async def get_pull_request_by_provider_reference(self, provider_id: str, repo: str, pr_number: int):
            matches = await self.list_pull_requests_by_provider_reference(provider_id, repo, pr_number)
            return matches[-1] if matches else None

        async def get_pull_request_by_plan_id(self, plan_id: str) -> PullRequestRecord | None:
            matches = [record for record in pull_requests.records if record.plan_id == plan_id]
            return matches[-1] if matches else None

        async def update_pull_request(
            self,
            pr_id: int,
            updates: dict[str, Any],
            *,
            expected_statuses: Sequence[str] | None = None,
            expected_plan_id: str | None = None,
        ) -> PullRequestRecord:
            return await pull_requests.update_pull_request(pr_id, updates)

        async def list_sync_pending_pull_requests(
            self,
            *,
            limit: int,
            after_id: int | None = None,
        ) -> list[PullRequestRecord]:
            records = [
                record
                for record in pull_requests.records
                if record.id is not None
                and (after_id is None or record.id > after_id)
                and getattr(record.status, "value", record.status) == PullRequestStatus.MERGED.value
                and record.sync_status not in {"synced", "failed"}
            ]
            return sorted(records, key=lambda record: record.id or 0)[:limit]

    class PlanLifecycleRepository:
        async def get_plan(self, plan_id: str) -> Plan | None:
            return await plans.get_plan(plan_id)

        async def set_plan_status(
            self,
            plan_id: str,
            status: PlanStatus,
            *,
            expected_statuses: Sequence[PlanStatus] | None = None,
            expected_digest: str | None = None,
            actor: str = "system",
            reason_code: str | None = None,
        ) -> Plan | None:
            return await plans.set_plan_status(plan_id, status)

        async def get_recommendation_action_status(self, recommendation_id: int) -> ActionStatus | None:
            return plans.action_status.get(recommendation_id)

        async def set_recommendation_action_status(
            self,
            recommendation_ids: Sequence[int],
            status: ActionStatus,
            *,
            expected_statuses: Sequence[ActionStatus] | None = None,
            actor: str = "system",
            plan_id: str | None = None,
        ) -> int:
            return await plans.set_recommendation_action_status(recommendation_ids, status)

        async def list_plans_for_recommendation(self, recommendation_id: int) -> list[Plan]:
            return [
                candidate for candidate in plans.plans.values() if recommendation_id in candidate.recommendation_ids
            ]

    class ArgoApplication:
        async def list_namespaced_custom_object(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs["namespace"] == plan.source.controller_namespace
            return {
                "metadata": {},
                "items": [
                    {
                        "apiVersion": "argoproj.io/v1alpha1",
                        "kind": "Application",
                        "metadata": {
                            "name": plan.source.controller_name,
                            "namespace": plan.source.controller_namespace,
                        },
                        "spec": {
                            "source": {
                                "repoURL": plan.source.repo_url,
                                "targetRevision": plan.source.ref,
                                "path": plan.source.path,
                            }
                        },
                        "status": {
                            "sync": {"status": "Synced", "revision": merge_sha},
                            "health": {"status": "Healthy"},
                        },
                    }
                ],
            }

    pull_request_repository = LifecycleAndSyncPullRequestRepository()
    plan_repository = PlanLifecycleRepository()
    reconciler = PullRequestLifecycleReconciler(
        pull_request_repository=pull_request_repository,
        plan_repository=plan_repository,
        git_provider=ProviderState(),
    )

    merged = await reconciler.reconcile_polled(
        pull_request.provider_id,
        pull_request.repo,
        pull_request.pr_number,
    )

    assert merged.outcome is PullRequestLifecycleOutcome.MERGED
    assert plans.plans[plan.plan_id].status is PlanStatus.MERGED
    assert plans.action_status[41] is ActionStatus.MERGED

    sync_runtime = GitOpsSyncRuntime(
        pull_request_repository,
        plan_repository,
        ArgoApplication(),
        clock=lambda: NOW + timedelta(minutes=1),
    )
    sync = await sync_runtime.poll_once()

    assert sync.synced == 1
    assert plans.plans[plan.plan_id].status is PlanStatus.SYNCED
    assert plans.action_status[41] is ActionStatus.SYNC_PENDING
    assert pull_requests.records[0].sync_revision == merge_sha
    assert pull_requests.records[0].sync_status == "synced"

    record = RecommendationRecord(
        id=41,
        namespace="payments",
        owner_kind="Deployment",
        owner_name="api",
        container_name="api",
        scope="workload",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="Reduce the api container CPU request.",
        action=action,
        status=RecommendationStatus.ACTIVE,
        current_cpu_request_millicores=1000,
        recommended_cpu_request_millicores=300,
    )

    class RecommendationRepository:
        current = record

        async def get_recommendations_by_statuses(
            self,
            statuses: Sequence[RecommendationStatus | str],
        ) -> list[RecommendationRecord]:
            expected = {str(getattr(status, "value", status)) for status in statuses}
            return [self.current] if self.current.status.value in expected else []

        async def get_recommendation_by_id(self, recommendation_id: int) -> RecommendationRecord | None:
            return self.current if recommendation_id == self.current.id else None

    class RecommendationLifecycle:
        def __init__(self) -> None:
            self.repo = RecommendationRepository()

        async def apply(self, recommendation_id: int, **updates: Any) -> RecommendationRecord:
            assert recommendation_id == self.repo.current.id
            self.repo.current = self.repo.current.model_copy(
                update={
                    "status": RecommendationStatus.APPLIED,
                    "actual_cpu_request_millicores": updates["actual_cpu"],
                    "application_method": updates["application_method"],
                    "applied_at": NOW + timedelta(minutes=1),
                }
            )
            return self.repo.current

    detector = AppliedDetector(
        cast(Any, RecommendationLifecycle()),
        reader=FakeWorkloadReader(
            {
                ("payments", "Deployment", "api"): _workload_snapshot(
                    generation=8,
                    observed_generation=8,
                    cpu_request_millicores=300,
                )
            }
        ),
        config=Config(),
        action_status_repository=plan_repository,
    )
    applied = await detector.detect()

    assert [item.status for item in applied] == [RecommendationStatus.APPLIED]
    assert plans.action_status[41] is ActionStatus.APPLIED
    assert plans.plans[plan.plan_id].status is PlanStatus.APPLIED
