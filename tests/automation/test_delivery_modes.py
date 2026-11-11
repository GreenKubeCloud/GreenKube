"""Delivery modes (roadmap F4): direct, patch and ci reach PR_OPEN -> MERGED -> SYNCED.

``patch`` and ``ci`` never write to Git from the cluster: an operator or a CI job
applies the approved plan's ``git format-patch`` and reports the pull request,
which GreenKube verifies through the (read-only) provider before tracking it.
"""

from __future__ import annotations

import difflib
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence

import pytest

from greenkube.automation.delivery import (
    PatchUnavailableError,
    build_plan_patch,
    configured_delivery_mode,
    normalized_url,
    plan_trailer_of,
    pull_request_number_from_url,
)
from greenkube.automation.git.base import GitFile, GitProviderError, GitRepository, PullRequestInfo
from greenkube.automation.plan_worker import ExternalDeliveryError, plan_branch_name
from greenkube.automation.pr_lifecycle import PullRequestLifecycleOutcome, PullRequestLifecycleReconciler
from greenkube.automation.sync_runtime import GitOpsSyncRuntime
from greenkube.gitops.contracts import ActionStatus, Plan, PlanStatus
from greenkube.models.metrics import PullRequestRecord, PullRequestStatus
from tests.automation.test_plan_worker import (  # pyrefly: ignore[missing-import]
    BASE_CONTENT,
    MANIFEST_PATH,
    NEW_CONTENT,
    NEW_PATH,
    NOW,
    UPDATED_CONTENT,
    FakeGitProvider,
    _blob_sha,
    _plan,
    _worker,
)

HEAD_SHA = "f" * 40
PR_NUMBER = 42
PR_URL = f"https://github.com/acme/gitops/pull/{PR_NUMBER}"
MERGE_SHA = "9" * 40


def _diff(before: str, after: str, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True), fromfile=old, tofile=new
        )
    )


PLAN_DIFF = _diff(BASE_CONTENT, UPDATED_CONTENT, f"a/{MANIFEST_PATH}", f"b/{MANIFEST_PATH}") + _diff(
    "", NEW_CONTENT, "/dev/null", f"b/{NEW_PATH}"
)


def _external_plan(mode: str | None, *, status: PlanStatus = PlanStatus.APPROVED) -> Plan:
    plan = _plan(status=status)
    assert plan.file_plan is not None
    file_plan = plan.file_plan.model_copy(update={"diff": PLAN_DIFF})
    return plan.model_copy(update={"file_plan": file_plan, "delivery_mode": mode})


class ExternalProvider(FakeGitProvider):
    """A read-only view of the repository: the PR head commit holds the applied plan."""

    def __init__(self, *, head_tree: dict[str, GitFile] | None = None) -> None:
        super().__init__()
        self.commits: dict[str, dict[str, GitFile]] = {}
        self.pull_request_info: PullRequestInfo | None = None
        self.reads: list[int] = []
        applied = dict(self.base_tree)
        applied[MANIFEST_PATH] = GitFile(
            MANIFEST_PATH, UPDATED_CONTENT, _blob_sha(UPDATED_CONTENT), _blob_sha(UPDATED_CONTENT)
        )
        applied[NEW_PATH] = GitFile(NEW_PATH, NEW_CONTENT, _blob_sha(NEW_CONTENT), _blob_sha(NEW_CONTENT))
        self.commits[HEAD_SHA] = head_tree if head_tree is not None else applied

    def _tree_at(self, ref: str) -> dict[str, GitFile]:
        if ref in self.commits:
            return self.commits[ref]
        return super()._tree_at(ref)

    async def get_file(self, repo: GitRepository, path: str, ref: str) -> GitFile | None:
        file = self._tree_at(ref).get(path)
        return None if file is None else GitFile(file.path, file.content, file.sha, file.blob_sha)

    def open_pull_request(
        self,
        *,
        head_ref: str,
        head_sha: str = HEAD_SHA,
        base_ref: str = "main",
        body: str = "",
        state: str = "open",
        merged: bool = False,
        url: str = PR_URL,
    ) -> None:
        self.pull_request_info = PullRequestInfo(
            number=PR_NUMBER,
            url=url,
            status=state,
            merged=merged,
            raw={
                "state": state,
                "merged": merged,
                "body": body,
                "head": {"ref": head_ref, "sha": head_sha},
                "base": {"ref": base_ref, "sha": "a" * 40},
            },
        )

    async def get_pull_request(self, repo: GitRepository, number: int) -> PullRequestInfo:
        self.reads.append(number)
        if self.pull_request_info is None or number != self.pull_request_info.number:
            raise GitProviderError(f"Pull request {number} not found.", status_code=404)
        return self.pull_request_info


def _external_worker(mode: str | None, provider: ExternalProvider | None = None):
    selected = provider or ExternalProvider()
    worker, operations, plans, pull_requests, _ = _worker(plan=_external_plan(mode), provider=selected)
    return worker, operations, plans, pull_requests, selected


# --------------------------------------------------------------------------- helpers


def test_pull_request_numbers_are_parsed_from_every_provider_url_shape():
    assert pull_request_number_from_url("https://github.com/acme/gitops/pull/42") == 42
    assert pull_request_number_from_url("https://gitlab.com/acme/sub/gitops/-/merge_requests/7/") == 7
    assert pull_request_number_from_url("https://gitea.local/acme/gitops/pulls/3?tab=files") == 3
    for invalid in ("ftp://github.com/a/b/pull/1", "https://github.com/acme/gitops", "not a url", "x" * 3000):
        assert pull_request_number_from_url(invalid) is None
    assert normalized_url("HTTPS://GitHub.com/Acme/gitops/pull/42/") == "https://github.com/Acme/gitops/pull/42"
    assert plan_trailer_of("Body\n\nGreenKube-Plan: pln-1\n") == "pln-1"
    assert plan_trailer_of("no trailer") is None


def test_delivery_mode_defaults_to_direct_and_rejects_unknown_values():
    from greenkube.core.config import Config

    assert configured_delivery_mode(Config()) == "direct"
    assert configured_delivery_mode(Config(AUTOMATION_DELIVERY_MODE="CI")) == "ci"
    with pytest.raises(ValueError):
        Config(AUTOMATION_DELIVERY_MODE="push")


# --------------------------------------------------------------------------- patch


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repository, check=True, capture_output=True, text=True, timeout=60
    ).stdout.strip()


@pytest.fixture
def gitops_repository(tmp_path: Path) -> tuple[Path, str]:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repository = tmp_path / "gitops"
    repository.mkdir()
    _git(repository, "init", "--quiet", "--initial-branch=main")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "commit.gpgsign", "false")
    (repository / MANIFEST_PATH).parent.mkdir(parents=True)
    (repository / MANIFEST_PATH).write_text(BASE_CONTENT)
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "initial")
    return repository, _git(repository, "rev-parse", "HEAD")


def _plan_on(base_revision: str, mode: str = "patch") -> Plan:
    plan = _external_plan(mode)
    assert plan.file_plan is not None
    return plan.model_copy(update={"file_plan": plan.file_plan.model_copy(update={"base_revision": base_revision})})


def test_plan_patch_applies_with_git_am_on_the_base_revision(gitops_repository):
    from greenkube.cli.plan import apply_patch_to_repository

    repository, base_revision = gitops_repository
    plan = _plan_on(base_revision)
    branch = plan_branch_name(plan)
    patch = build_plan_patch(
        plan,
        branch=branch,
        commit_message="greenkube: rightsize payments/api\n\nApply GreenKube plan plan-1.\n\nGreenKube-Plan: plan-1\n",
        date=NOW,
    )

    assert patch.text.startswith("From ")
    assert "Subject: [PATCH] greenkube: rightsize payments/api" in patch.text
    assert f"index {_blob_sha(BASE_CONTENT)}..{_blob_sha(UPDATED_CONTENT)}" in patch.text
    assert "new file mode 100644" in patch.text
    assert patch.files == (MANIFEST_PATH, NEW_PATH)

    apply_patch_to_repository(
        patch.text,
        branch=branch,
        base_branch="main",
        base_revision=base_revision,
        repository=repository,
        fetch=False,
    )

    assert _git(repository, "rev-parse", "--abbrev-ref", "HEAD") == branch
    assert (repository / MANIFEST_PATH).read_text() == UPDATED_CONTENT
    assert (repository / NEW_PATH).read_text() == NEW_CONTENT
    assert _git(repository, "rev-parse", "HEAD~1") == base_revision
    assert "GreenKube-Plan: plan-1" in _git(repository, "log", "-1", "--format=%B")
    assert _git(repository, "log", "-1", "--format=%an") == "GreenKube Bot"


def test_apply_patch_refuses_a_dirty_tree_and_an_existing_branch(gitops_repository):
    from greenkube.cli.plan import PlanCliError, apply_patch_to_repository

    repository, base_revision = gitops_repository
    plan = _plan_on(base_revision)
    patch = build_plan_patch(plan, branch=plan_branch_name(plan), commit_message="subject\n", date=NOW)
    (repository / "untracked.txt").write_text("local edit")
    with pytest.raises(PlanCliError, match="uncommitted"):
        apply_patch_to_repository(
            patch.text,
            branch=patch.branch,
            base_branch="main",
            base_revision=base_revision,
            repository=repository,
            fetch=False,
        )
    (repository / "untracked.txt").unlink()
    _git(repository, "branch", patch.branch)
    with pytest.raises(PlanCliError, match="already exists"):
        apply_patch_to_repository(
            patch.text,
            branch=patch.branch,
            base_branch="main",
            base_revision=base_revision,
            repository=repository,
            fetch=False,
        )


def test_plan_patch_fails_closed_when_the_stored_diff_does_not_reproduce_the_original_blob():
    plan = _external_plan("patch")
    assert plan.file_plan is not None
    tampered = PLAN_DIFF.replace("-          image: example/api:v1", "-          image: example/api:v0")
    broken = plan.model_copy(update={"file_plan": plan.file_plan.model_copy(update={"diff": tampered})})

    with pytest.raises(PatchUnavailableError):
        build_plan_patch(broken, branch="b", commit_message="s\n", date=NOW)

    missing = plan.model_copy(
        update={"file_plan": plan.file_plan.model_copy(update={"diff": PLAN_DIFF.split("--- /dev/null")[0]})}
    )
    with pytest.raises(PatchUnavailableError):
        build_plan_patch(missing, branch="b", commit_message="s\n", date=NOW)


# --------------------------------------------------------------------------- delivery reports


@pytest.mark.asyncio
async def test_ci_delivery_report_is_verified_then_tracked_without_any_git_write():
    worker, operations, plans, pull_requests, provider = _external_worker("ci")
    branch = plan_branch_name(plans.plans["plan-1"])
    provider.open_pull_request(head_ref=branch)

    result = await worker.record_external_delivery("plan-1", pr_url=PR_URL, head_sha=HEAD_SHA, actor="apikey:ci")

    assert result.created and result.linked_by == "branch" and result.pull_request_number == PR_NUMBER
    plan = plans.plans["plan-1"]
    assert plan.status is PlanStatus.PR_OPEN
    assert plan.pre_apply_baseline is not None
    assert set(plans.action_status.values()) == {ActionStatus.PR_OPEN}
    assert {(record.pr_number, record.commit_sha, record.head_branch) for record in pull_requests.records} == {
        (PR_NUMBER, HEAD_SHA, branch)
    }
    assert all(record.status is PullRequestStatus.OPEN for record in pull_requests.records)
    # No branch, commit or pull request was written from the cluster, and no queued operation ran.
    assert provider.branch_creations == [] and provider.commit_requests == [] and provider.pull_request_creations == []
    assert operations.claims == []

    again = await worker.record_external_delivery("plan-1", pr_url=PR_URL, head_sha=HEAD_SHA, actor="apikey:ci")
    assert not again.created and again.pull_request_number == PR_NUMBER
    assert len(pull_requests.records) == 2  # one per recommendation, not duplicated

    with pytest.raises(ExternalDeliveryError) as conflict:
        await worker.record_external_delivery(
            "plan-1", pr_url="https://github.com/acme/gitops/pull/43", head_sha=HEAD_SHA, actor="apikey:ci"
        )
    assert conflict.value.code == "delivery_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"head_ref": "feature/other"}, "delivery_not_verified"),
        ({"head_sha": "e" * 40}, "delivery_not_verified"),
        ({"base_ref": "release"}, "delivery_not_verified"),
        ({"state": "closed"}, "delivery_not_verified"),
        ({"report_url": "https://github.com/evil/gitops/pull/42"}, "delivery_not_verified"),
    ],
    ids=["other-branch", "head-sha-mismatch", "other-base", "closed-unmerged", "other-repository"],
)
async def test_ci_delivery_report_fails_closed_when_the_provider_does_not_confirm_it(change, code):
    worker, _, plans, pull_requests, provider = _external_worker("ci")
    options: dict[str, Any] = {"head_ref": plan_branch_name(plans.plans["plan-1"]), **change}
    # The reported URL must be the one the provider (scoped to the plan repository) returns.
    pr_url = options.pop("report_url", PR_URL)
    provider.open_pull_request(**options)

    with pytest.raises(ExternalDeliveryError) as error:
        await worker.record_external_delivery("plan-1", pr_url=pr_url, head_sha=HEAD_SHA, actor="apikey:ci")

    assert error.value.code == code
    assert plans.plans["plan-1"].status is PlanStatus.APPROVED
    assert pull_requests.records == []


@pytest.mark.asyncio
async def test_ci_delivery_report_rejects_a_head_without_exactly_the_planned_change():
    provider = ExternalProvider()
    provider.commits[HEAD_SHA] = {
        **provider.commits[HEAD_SHA],
        "apps/payments/extra.yaml": GitFile("apps/payments/extra.yaml", "x\n", _blob_sha("x\n"), _blob_sha("x\n")),
    }
    worker, _, plans, pull_requests, _ = _external_worker("ci", provider)
    provider.open_pull_request(head_ref=plan_branch_name(plans.plans["plan-1"]))

    with pytest.raises(ExternalDeliveryError) as error:
        await worker.record_external_delivery("plan-1", pr_url=PR_URL, head_sha=HEAD_SHA, actor="apikey:ci")

    assert error.value.code == "delivery_not_verified"
    assert "exactly the planned change" in str(error.value)
    assert pull_requests.records == []


@pytest.mark.asyncio
async def test_delivery_reports_are_refused_for_direct_plans_and_need_a_head_sha_in_ci_mode():
    direct, *_ = _external_worker("direct")
    with pytest.raises(ExternalDeliveryError) as mismatch:
        await direct.record_external_delivery("plan-1", pr_url=PR_URL, head_sha=HEAD_SHA, actor="apikey:ci")
    assert mismatch.value.code == "delivery_mode_mismatch"

    legacy, *_ = _external_worker(None)
    with pytest.raises(ExternalDeliveryError):
        await legacy.record_external_delivery("plan-1", pr_url=PR_URL, head_sha=HEAD_SHA, actor="apikey:ci")

    ci, *_ = _external_worker("ci")
    with pytest.raises(ExternalDeliveryError) as missing:
        await ci.record_external_delivery("plan-1", pr_url=PR_URL, head_sha=None, actor="apikey:ci")
    assert (missing.value.code, missing.value.status_code) == ("invalid_head_sha", 422)
    with pytest.raises(ExternalDeliveryError) as invalid_url:
        await ci.record_external_delivery("plan-1", pr_url="https://example.com/", head_sha=HEAD_SHA, actor="x")
    assert invalid_url.value.status_code == 422


@pytest.mark.asyncio
async def test_patch_link_back_accepts_the_plan_trailer_and_reads_the_head_from_the_provider():
    worker, _, plans, pull_requests, provider = _external_worker("patch")
    provider.open_pull_request(head_ref="my-own-branch", body="Rightsizing.\n\nGreenKube-Plan: plan-1\n")

    result = await worker.record_external_delivery("plan-1", pr_url=PR_URL, head_sha=None, actor="user:alice")

    assert result.linked_by == "trailer" and result.head_sha == HEAD_SHA
    assert plans.plans["plan-1"].status is PlanStatus.PR_OPEN
    assert {record.head_branch for record in pull_requests.records} == {"my-own-branch"}

    # Without the trailer, another branch is not linked.
    other, _, other_plans, _, other_provider = _external_worker("patch")
    other_provider.open_pull_request(head_ref="my-own-branch", body="Rightsizing.")
    with pytest.raises(ExternalDeliveryError) as error:
        await other.record_external_delivery("plan-1", pr_url=PR_URL, head_sha=None, actor="user:alice")
    assert "GreenKube-Plan" in str(error.value)
    assert other_plans.plans["plan-1"].status is PlanStatus.APPROVED


# --------------------------------------------------------------------------- closed loop


class _LoopPullRequests:
    def __init__(self, store) -> None:
        self.store = store

    async def list_pull_requests_by_provider_reference(self, provider_id: str, repo: str, pr_number: int):
        return [
            record
            for record in self.store.records
            if (record.provider_id, record.repo, record.pr_number) == (provider_id, repo, pr_number)
        ]

    async def get_pull_request_by_provider_reference(self, provider_id: str, repo: str, pr_number: int):
        matches = await self.list_pull_requests_by_provider_reference(provider_id, repo, pr_number)
        return matches[-1] if matches else None

    async def get_pull_request_by_plan_id(self, plan_id: str) -> PullRequestRecord | None:
        matches = [record for record in self.store.records if record.plan_id == plan_id]
        return matches[-1] if matches else None

    async def update_pull_request(self, pr_id: int, updates: dict[str, Any], **_: Any) -> PullRequestRecord:
        return await self.store.update_pull_request(pr_id, updates)

    async def list_sync_pending_pull_requests(self, *, limit: int, after_id: int | None = None):
        records = [
            record
            for record in self.store.records
            if record.id is not None
            and (after_id is None or record.id > after_id)
            and getattr(record.status, "value", record.status) == PullRequestStatus.MERGED.value
            and record.sync_status not in {"synced", "failed"}
        ]
        return sorted(records, key=lambda record: record.id or 0)[:limit]


class _LoopPlans:
    def __init__(self, plans) -> None:
        self.plans = plans

    async def get_plan(self, plan_id: str) -> Plan | None:
        return await self.plans.get_plan(plan_id)

    async def set_plan_status(self, plan_id: str, status: PlanStatus, **_: Any) -> Plan | None:
        return await self.plans.set_plan_status(plan_id, status)

    async def get_recommendation_action_status(self, recommendation_id: int) -> ActionStatus | None:
        return self.plans.action_status.get(recommendation_id)

    async def set_recommendation_action_status(self, recommendation_ids: Sequence[int], status, **_: Any) -> int:
        return await self.plans.set_recommendation_action_status(recommendation_ids, status)

    async def list_plans_for_recommendation(self, recommendation_id: int) -> list[Plan]:
        return [plan for plan in self.plans.plans.values() if recommendation_id in plan.recommendation_ids]


class _MergedProvider:
    async def get_pull_request(self, provider_id: str, repo: str, number: int) -> PullRequestInfo:
        return PullRequestInfo(
            number=number,
            url=f"https://github.com/acme/gitops/pull/{number}",
            status="closed",
            merged=True,
            raw={
                "state": "closed",
                "merged": True,
                "merge_commit_sha": MERGE_SHA,
                "merged_at": NOW.isoformat(),
                "closed_at": NOW.isoformat(),
            },
        )


def _argo(plan: Plan):
    class ArgoApplication:
        async def list_namespaced_custom_object(self, **kwargs: Any) -> dict[str, Any]:
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
                            "sync": {"status": "Synced", "revision": MERGE_SHA},
                            "health": {"status": "Healthy"},
                        },
                    }
                ],
            }

    return ArgoApplication()


async def _merge_and_sync(plans, pull_requests) -> None:
    from datetime import timedelta

    record = pull_requests.records[0]
    loop_pull_requests = _LoopPullRequests(pull_requests)
    loop_plans = _LoopPlans(plans)
    reconciler = PullRequestLifecycleReconciler(
        pull_request_repository=loop_pull_requests, plan_repository=loop_plans, git_provider=_MergedProvider()
    )
    merged = await reconciler.reconcile_polled(record.provider_id, record.repo, record.pr_number)
    assert merged.outcome is PullRequestLifecycleOutcome.MERGED
    assert plans.plans["plan-1"].status is PlanStatus.MERGED

    plan = plans.plans["plan-1"]
    sync = await GitOpsSyncRuntime(
        loop_pull_requests, loop_plans, _argo(plan), clock=lambda: NOW + timedelta(minutes=1)
    ).poll_once()
    assert sync.synced == 1
    assert plans.plans["plan-1"].status is PlanStatus.SYNCED


@pytest.mark.asyncio
async def test_direct_mode_reaches_pr_open_merged_and_synced():
    worker, _, plans, pull_requests, _ = _worker(plan=_external_plan("direct"))

    assert await worker.process_once("worker-1")
    assert plans.plans["plan-1"].status is PlanStatus.PR_OPEN

    await _merge_and_sync(plans, pull_requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["patch", "ci"])
async def test_external_modes_reach_pr_open_merged_and_synced(mode):
    worker, operations, plans, pull_requests, provider = _external_worker(mode)
    provider.open_pull_request(head_ref=plan_branch_name(plans.plans["plan-1"]))

    await worker.record_external_delivery(
        "plan-1", pr_url=PR_URL, head_sha=HEAD_SHA if mode == "ci" else None, actor="apikey:ci"
    )
    assert plans.plans["plan-1"].status is PlanStatus.PR_OPEN

    await _merge_and_sync(plans, pull_requests)
    assert provider.commit_requests == [] and operations.claims == []


# --------------------------------------------------------------------------- approval


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "enqueued"), [("direct", 1), ("patch", 0), ("ci", 0)])
async def test_approval_records_the_delivery_mode_and_queues_only_direct_plans(monkeypatch, mode, enqueued):
    from greenkube.core.config import Config
    from tests.automation.test_plan_service import (  # pyrefly: ignore[missing-import]
        IdempotentQueue,
        MemoryPlanRepository,
        _action,
        _archive_snapshot,
        _service,
        _source,
    )

    monkeypatch.setattr("greenkube.core.config.get_config", lambda: Config(AUTOMATION_DELIVERY_MODE=mode))
    repository = MemoryPlanRepository()
    queue = IdempotentQueue()
    service = _service(repository, operation_enqueuer=queue)
    source = _source()
    plan = await service.create_plan(_action(), source, _archive_snapshot(monkeypatch, source))
    assert plan.digest is not None and plan.delivery_mode is None

    approved = await service.approve_plan(plan.plan_id, plan.digest)

    assert approved.status is PlanStatus.APPROVED
    assert approved.delivery_mode == mode
    assert (await repository.get_plan(plan.plan_id)).delivery_mode == mode
    assert len(queue.operations) == enqueued

    # A later change of the setting never re-routes an approval.
    monkeypatch.setattr("greenkube.core.config.get_config", lambda: Config(AUTOMATION_DELIVERY_MODE="direct"))
    again = await service.approve_plan(plan.plan_id, plan.digest)
    assert again.delivery_mode == mode
    assert len(queue.operations) == enqueued


# --------------------------------------------------------------------------- linkage by branch name


class _ListingPlans:
    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def list_plans(self, *, status: PlanStatus | None = None, **_: Any) -> list[Plan]:
        return [plan for plan in self.inner.plans.values() if status is None or plan.status is status]


class _BranchLookupProvider(ExternalProvider):
    async def find_pull_request(self, repo: GitRepository, head: str, base: str) -> PullRequestInfo | None:
        info = self.pull_request_info
        if info is None or info.raw.get("head", {}).get("ref") != head or info.raw.get("base", {}).get("ref") != base:
            return None
        return info


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["patch", "ci"])
async def test_a_pull_request_opened_from_the_plan_branch_is_linked_without_a_report(mode):
    worker, _, plans, pull_requests, provider = _external_worker(mode, _BranchLookupProvider())
    worker.plans = _ListingPlans(plans)

    assert await worker.link_external_deliveries() == 0  # nothing opened yet

    provider.open_pull_request(head_ref=plan_branch_name(plans.plans["plan-1"]))
    assert await worker.link_external_deliveries() == 1

    assert plans.plans["plan-1"].status is PlanStatus.PR_OPEN
    assert {record.pr_number for record in pull_requests.records} == {PR_NUMBER}
    assert await worker.link_external_deliveries() == 0  # no longer approved


@pytest.mark.asyncio
async def test_branch_linkage_ignores_direct_plans_and_unverified_pull_requests():
    direct, _, direct_plans, _, direct_provider = _external_worker("direct", _BranchLookupProvider())
    direct.plans = _ListingPlans(direct_plans)
    direct_provider.open_pull_request(head_ref=plan_branch_name(direct_plans.plans["plan-1"]))
    assert await direct.link_external_deliveries() == 0

    provider = _BranchLookupProvider()
    provider.commits[HEAD_SHA] = dict(provider.base_tree)  # the branch does not hold the plan
    worker, _, plans, pull_requests, _ = _external_worker("ci", provider)
    worker.plans = _ListingPlans(plans)
    provider.open_pull_request(head_ref=plan_branch_name(plans.plans["plan-1"]))
    assert await worker.link_external_deliveries() == 0
    assert plans.plans["plan-1"].status is PlanStatus.APPROVED and pull_requests.records == []


@pytest.mark.asyncio
async def test_approved_external_plans_wait_for_their_pull_request_beyond_the_preview_ttl(monkeypatch):
    """F4: a person or a scheduled CI job delivers a patch/ci plan; it does not expire with the 30-minute TTL."""
    from datetime import timedelta

    from greenkube.automation.delivery import EXTERNAL_DELIVERY_GRACE
    from greenkube.core.config import Config
    from tests.automation.test_plan_service import (  # pyrefly: ignore[missing-import]
        MemoryPlanRepository,
        _action,
        _archive_snapshot,
        _service,
        _source,
    )

    now = [NOW]
    repository = MemoryPlanRepository()
    service = _service(repository, clock=lambda: now[0])
    source = _source()
    for mode in ("ci", "direct"):
        monkeypatch.setattr("greenkube.core.config.get_config", lambda mode=mode: Config(AUTOMATION_DELIVERY_MODE=mode))
        plan = await service.create_plan(_action(), source, _archive_snapshot(monkeypatch, source))
        assert plan.digest is not None
        await service.approve_plan(plan.plan_id, plan.digest)
        assert plan.expires_at is not None
        now[0] = plan.expires_at + timedelta(minutes=1)
        expired = await service.expire_due_plans(now=now[0])
        if mode == "ci":
            assert expired == [] and (await repository.get_plan(plan.plan_id)).status is PlanStatus.APPROVED
            now[0] = plan.expires_at + EXTERNAL_DELIVERY_GRACE + timedelta(minutes=1)
            assert [item.plan_id for item in await service.expire_due_plans(now=now[0])] == [plan.plan_id]
        else:
            assert [item.plan_id for item in expired] == [plan.plan_id]
        now[0] = NOW


@pytest.mark.asyncio
async def test_a_browser_url_on_another_origin_names_the_same_pull_request():
    """Gitea behind webBaseUrl: the provider answers with its API origin, people paste the browser URL."""
    worker, _, plans, _, provider = _external_worker("ci")
    provider.open_pull_request(
        head_ref=plan_branch_name(plans.plans["plan-1"]), url="http://gitea-http.gitea:3000/acme/gitops/pulls/42"
    )

    result = await worker.record_external_delivery(
        "plan-1", pr_url="https://git.example.com/acme/gitops/pulls/42", head_sha=HEAD_SHA, actor="apikey:ci"
    )

    assert result.created
    with pytest.raises(ExternalDeliveryError):
        other, _, other_plans, _, other_provider = _external_worker("ci")
        other_provider.open_pull_request(head_ref=plan_branch_name(other_plans.plans["plan-1"]))
        await other.record_external_delivery(
            "plan-1", pr_url="https://github.com/evil/gitops/pull/42", head_sha=HEAD_SHA, actor="apikey:ci"
        )


# --------------------------------------------------------------------------- interrupted recording


def _fail_second_save(pull_requests: Any) -> list[int]:
    """Make the second ``save_pull_request`` raise once (a transient DB error mid-recording)."""
    original = pull_requests.save_pull_request
    calls: list[int] = []

    async def flaky(record: PullRequestRecord) -> PullRequestRecord:
        calls.append(record.recommendation_id)
        if len(calls) == 2:
            raise ConnectionError("database connection lost")
        return await original(record)

    pull_requests.save_pull_request = flaky
    return calls


def _release_fails(plans: Any) -> None:
    """Make the release write (back to APPROVED) fail too, leaving the plan APPLYING."""
    original = plans.set_plan_status

    async def set_plan_status(plan_id: str, status: PlanStatus, **kwargs: Any) -> Plan | None:
        if status is PlanStatus.APPROVED:
            raise ConnectionError("database connection lost")
        return await original(plan_id, status, **kwargs)

    plans.set_plan_status = set_plan_status


@pytest.mark.asyncio
async def test_a_failed_delivery_recording_returns_the_plan_to_approved_and_the_retry_succeeds():
    worker, operations, plans, pull_requests, provider = _external_worker("ci")
    provider.open_pull_request(head_ref=plan_branch_name(plans.plans["plan-1"]))
    _fail_second_save(pull_requests)

    with pytest.raises(ExternalDeliveryError) as failed:
        await worker.record_external_delivery("plan-1", pr_url=PR_URL, head_sha=HEAD_SHA, actor="apikey:ci")
    assert failed.value.code == "delivery_not_recorded" and failed.value.status_code == 503
    # Never stuck APPLYING: approved again, so it stays cancellable, expirable and retryable.
    assert plans.plans["plan-1"].status is PlanStatus.APPROVED
    assert plans.reason_codes[-1] == "external_delivery_failed"

    result = await worker.record_external_delivery("plan-1", pr_url=PR_URL, head_sha=HEAD_SHA, actor="apikey:ci")
    assert result.created and plans.plans["plan-1"].status is PlanStatus.PR_OPEN
    assert set(plans.action_status.values()) == {ActionStatus.PR_OPEN}
    assert sorted(record.recommendation_id for record in pull_requests.records) == sorted(
        plans.plans["plan-1"].recommendation_ids
    )
    assert {record.pr_number for record in pull_requests.records} == {PR_NUMBER}
    assert operations.claims == []


@pytest.mark.asyncio
async def test_a_plan_left_applying_by_an_interrupted_recording_resumes_on_the_next_report():
    worker, _, plans, pull_requests, provider = _external_worker("ci")
    provider.open_pull_request(head_ref=plan_branch_name(plans.plans["plan-1"]))
    _fail_second_save(pull_requests)
    _release_fails(plans)

    with pytest.raises(ExternalDeliveryError):
        await worker.record_external_delivery("plan-1", pr_url=PR_URL, head_sha=HEAD_SHA, actor="apikey:ci")
    assert plans.plans["plan-1"].status is PlanStatus.APPLYING  # the release write failed too

    result = await worker.record_external_delivery("plan-1", pr_url=PR_URL, head_sha=HEAD_SHA, actor="apikey:ci")
    assert result.created and plans.plans["plan-1"].status is PlanStatus.PR_OPEN
    assert len(pull_requests.records) == len(plans.plans["plan-1"].recommendation_ids)
    assert {record.pr_number for record in pull_requests.records} == {PR_NUMBER}


@pytest.mark.asyncio
async def test_resuming_an_interrupted_recording_refuses_another_pull_request():
    worker, _, plans, pull_requests, provider = _external_worker("ci")
    provider.open_pull_request(head_ref=plan_branch_name(plans.plans["plan-1"]))
    _fail_second_save(pull_requests)
    _release_fails(plans)
    with pytest.raises(ExternalDeliveryError):
        await worker.record_external_delivery("plan-1", pr_url=PR_URL, head_sha=HEAD_SHA, actor="apikey:ci")

    other_url = "https://github.com/acme/gitops/pull/43"
    assert provider.pull_request_info is not None
    provider.pull_request_info = PullRequestInfo(
        number=43, url=other_url, status="open", raw=dict(provider.pull_request_info.raw)
    )
    with pytest.raises(ExternalDeliveryError) as conflict:
        await worker.record_external_delivery("plan-1", pr_url=other_url, head_sha=HEAD_SHA, actor="apikey:ci")
    assert conflict.value.code == "delivery_conflict"
    assert {record.pr_number for record in pull_requests.records} == {PR_NUMBER}


@pytest.mark.asyncio
async def test_the_controller_link_step_recovers_a_plan_left_applying():
    from datetime import datetime, timedelta, timezone

    worker, _, plans, pull_requests, provider = _external_worker("patch", _BranchLookupProvider())
    worker.plans = _ListingPlans(plans)
    provider.open_pull_request(head_ref=plan_branch_name(plans.plans["plan-1"]))
    _fail_second_save(pull_requests)
    _release_fails(plans)
    with pytest.raises(ExternalDeliveryError):
        await worker.record_external_delivery("plan-1", pr_url=PR_URL, head_sha=HEAD_SHA, actor="apikey:ci")
    assert plans.plans["plan-1"].status is PlanStatus.APPLYING

    # A recording that may still be in flight (recent update) is left alone.
    now = datetime.now(timezone.utc)
    worker.clock = lambda: now
    plans.plans["plan-1"] = plans.plans["plan-1"].model_copy(update={"updated_at": now - timedelta(seconds=30)})
    assert await worker.link_external_deliveries() == 0
    assert plans.plans["plan-1"].status is PlanStatus.APPLYING

    plans.plans["plan-1"] = plans.plans["plan-1"].model_copy(update={"updated_at": now - timedelta(minutes=10)})
    assert await worker.link_external_deliveries() == 1
    assert plans.plans["plan-1"].status is PlanStatus.PR_OPEN
    assert set(plans.action_status.values()) == {ActionStatus.PR_OPEN}


@pytest.mark.asyncio
async def test_the_link_step_resumes_a_link_back_recorded_from_another_branch():
    from datetime import datetime, timedelta, timezone

    worker, _, plans, pull_requests, provider = _external_worker("patch", _BranchLookupProvider())
    worker.plans = _ListingPlans(plans)
    trailer = f"GreenKube-Plan: {plans.plans['plan-1'].plan_id}"
    provider.open_pull_request(head_ref="feature/my-branch", body=trailer)
    _fail_second_save(pull_requests)
    _release_fails(plans)
    with pytest.raises(ExternalDeliveryError):
        await worker.record_external_delivery("plan-1", pr_url=PR_URL, head_sha=HEAD_SHA, actor="user:alice")

    now = datetime.now(timezone.utc)
    worker.clock = lambda: now
    plans.plans["plan-1"] = plans.plans["plan-1"].model_copy(update={"updated_at": now - timedelta(minutes=10)})
    assert await worker.link_external_deliveries() == 1
    assert plans.plans["plan-1"].status is PlanStatus.PR_OPEN


@pytest.mark.asyncio
async def test_branch_linkage_rotates_so_plans_without_a_pull_request_cannot_starve_the_others():
    provider = _BranchLookupProvider()
    worker, _, plans, _, _ = _external_worker("ci", provider)
    template = plans.plans["plan-1"]
    lookups: list[str] = []
    original = provider.find_pull_request

    async def find(repo: GitRepository, head: str, base: str) -> PullRequestInfo | None:
        lookups.append(head)
        return await original(repo, head, base)

    provider.find_pull_request = find  # type: ignore[method-assign]
    worker.plans = _ListingPlans(plans)
    for index in range(2, 6):
        plans.plans[f"plan-{index}"] = template.model_copy(update={"plan_id": f"plan-{index}"})

    for _ in range(3):
        await worker.link_external_deliveries(limit=2)
    assert len(set(lookups)) == 5  # every plan was looked up within three passes
