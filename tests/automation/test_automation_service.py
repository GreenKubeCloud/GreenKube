# tests/automation/test_automation_service.py
"""Orchestration tests for the recommendation PR bot."""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.automation.git.base import GitFile, GitProvider, GitRepository, PullRequestInfo
from greenkube.automation.service import AutomationService
from greenkube.automation.source_resolver import (
    ANNOTATION_PATH,
    ANNOTATION_REPO,
    AnnotationSourceResolver,
)
from greenkube.core.config import Config
from greenkube.core.db import db_manager
from greenkube.models.metrics import (
    ApplyPrRequest,
    PullRequestStatus,
    RecommendationRecord,
    RecommendationStatus,
    RecommendationType,
)
from greenkube.storage.sqlite.automation_operation_repository import SQLiteAutomationOperationRepository
from greenkube.storage.sqlite.pull_request_repository import SQLitePullRequestRepository
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository

MANIFEST = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: payments-api
  namespace: prod
spec:
  template:
    spec:
      containers:
        - name: api
          resources:
            requests:
              cpu: 500m
"""


class FakeReader:
    def __init__(self, annotations):
        self.annotations = annotations

    async def read_annotations(self, namespace, kind, name):
        return self.annotations


class FakeProvider(GitProvider):
    name = "fake"

    def __init__(self, files):
        super().__init__("token")
        self.files = files
        self.branches = []
        self.updates = []
        self.pulls = []

    async def get_default_branch(self, repo):
        return "main"

    async def get_file(self, repo: GitRepository, path: str, ref: str):
        content = self.files.get(path)
        if content is None:
            return None
        return GitFile(path=path, content=content, sha="sha-1")

    async def list_files(self, repo, ref):
        return list(self.files)

    async def create_branch(self, repo, branch, from_ref):
        self.branches.append((branch, from_ref))

    async def update_file(self, repo, path, content, message, branch, sha=None):
        self.updates.append({"path": path, "content": content, "branch": branch, "sha": sha})

    async def create_pull_request(
        self,
        repo: GitRepository,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequestInfo:
        self.pulls.append({"head": head, "base": base, "title": title, "body": body})
        return PullRequestInfo(
            number=12,
            url=f"https://example.test/{repo.full_name}/pulls/12",
            status="open",
        )

    async def get_pull_request(self, repo: GitRepository, number: int) -> PullRequestInfo:
        return PullRequestInfo(number=number, url="https://example.test/pull/12", status="open")


@pytest.fixture
async def repos():
    await db_manager.setup_sqlite(db_path=":memory:")
    yield SQLiteRecommendationRepository(db_manager), SQLitePullRequestRepository(db_manager)
    await db_manager.close()


def _record(**overrides) -> RecommendationRecord:
    defaults = dict(
        pod_name="payments-api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="CPU oversized",
        scope="workload",
        owner_kind="Deployment",
        owner_name="payments-api",
        current_cpu_request_millicores=500,
        recommended_cpu_request_millicores=300,
        potential_savings_cost=73.5,
        potential_savings_co2e_grams=32594.0,
        expires_at=datetime.now(timezone.utc) + timedelta(days=14),
        patch={
            "kind": "Deployment",
            "namespace": "prod",
            "name": "payments-api",
            "operations": [{"op": "set_container_resources", "resource": "cpu", "field": "requests", "value": "300m"}],
        },
    )
    defaults.update(overrides)
    return RecommendationRecord(**defaults)


def _service(repo, pr_repo, provider, annotations=None) -> AutomationService:
    default_annotations = {
        ANNOTATION_REPO: "https://example.test/acme/manifests.git",
        ANNOTATION_PATH: "apps/api.yaml",
    }
    resolver = AnnotationSourceResolver(FakeReader(default_annotations if annotations is None else annotations))
    return AutomationService(
        reco_repo=repo,
        pr_repo=pr_repo,
        config=Config(),
        resolver=resolver,
        provider=provider,
    )


class TestAutomationService:
    @pytest.mark.asyncio
    async def test_dry_run_returns_diff_without_git_writes(self, repos):
        repo, pr_repo = repos
        await repo.save_recommendations([_record()])
        rec_id = (await repo.get_active_recommendations())[0].id
        provider = FakeProvider({"apps/api.yaml": MANIFEST})

        response = await _service(repo, pr_repo, provider).apply_recommendation_pr(rec_id, ApplyPrRequest(dry_run=True))

        assert response.status == "dry_run"
        assert response.diff is not None and "cpu: 300m" in response.diff
        assert provider.branches == [] and provider.updates == [] and provider.pulls == []
        assert await pr_repo.get_pull_requests_for_recommendation(rec_id) == []

    @pytest.mark.asyncio
    async def test_full_flow_opens_pr_and_tracks_it(self, repos):
        repo, pr_repo = repos
        await repo.save_recommendations([_record()])
        rec_id = (await repo.get_active_recommendations())[0].id
        provider = FakeProvider({"apps/api.yaml": MANIFEST})

        response = await _service(repo, pr_repo, provider).apply_recommendation_pr(rec_id, ApplyPrRequest())

        assert response.status == "pr_open"
        assert response.pr_url is not None and response.pr_url.endswith("/pulls/12")
        assert len(provider.updates) == 1
        assert "cpu: 300m" in provider.updates[0]["content"]
        assert provider.pulls[0]["base"] == "main"

        attempts = await pr_repo.get_pull_requests_for_recommendation(rec_id)
        assert len(attempts) == 1
        assert attempts[0].status == PullRequestStatus.OPEN

        repeated = await _service(repo, pr_repo, provider).apply_recommendation_pr(rec_id, ApplyPrRequest())
        assert repeated.status == "pr_open"
        assert len(provider.pulls) == 1
        assert len(await pr_repo.get_pull_requests_for_recommendation(rec_id)) == 1

        updated = await repo.get_recommendation_by_id(rec_id)
        assert updated.status == RecommendationStatus.PR_OPEN
        events = [e.event_type for e in await repo.get_events(rec_id)]
        assert "pr_opened" in events

    @pytest.mark.asyncio
    async def test_manifest_discovery_without_path_annotation(self, repos):
        repo, pr_repo = repos
        await repo.save_recommendations([_record()])
        rec_id = (await repo.get_active_recommendations())[0].id
        provider = FakeProvider({"some/nested/deploy.yaml": MANIFEST})
        service = _service(
            repo,
            pr_repo,
            provider,
            annotations={ANNOTATION_REPO: "https://example.test/acme/manifests.git"},
        )

        response = await service.apply_recommendation_pr(rec_id, ApplyPrRequest(dry_run=True))

        assert response.status == "dry_run"
        assert response.path == "some/nested/deploy.yaml"

    @pytest.mark.asyncio
    async def test_missing_annotation_returns_error(self, repos):
        repo, pr_repo = repos
        await repo.save_recommendations([_record()])
        rec_id = (await repo.get_active_recommendations())[0].id
        provider = FakeProvider({"apps/api.yaml": MANIFEST})

        response = await _service(repo, pr_repo, provider, annotations={}).apply_recommendation_pr(
            rec_id, ApplyPrRequest()
        )

        assert response.status == "error"
        assert "git-repo" in (response.message or "")

    @pytest.mark.asyncio
    async def test_non_rightsizing_type_is_rejected(self, repos):
        repo, pr_repo = repos
        await repo.save_recommendations([_record(type=RecommendationType.ZOMBIE_POD, patch=None)])
        rec_id = (await repo.get_active_recommendations())[0].id

        response = await _service(repo, pr_repo, FakeProvider({})).apply_recommendation_pr(rec_id, ApplyPrRequest())

        assert response.status == "error"
        assert "rightsizing" in (response.message or "").lower()

    @pytest.mark.asyncio
    async def test_unknown_recommendation_raises(self, repos):
        repo, pr_repo = repos
        with pytest.raises(ValueError):
            await _service(repo, pr_repo, FakeProvider({})).apply_recommendation_pr(999, ApplyPrRequest())

    @pytest.mark.asyncio
    async def test_automation_status_reports_configuration(self, repos):
        repo, pr_repo = repos
        status = await _service(repo, pr_repo, FakeProvider({})).automation_status()
        assert status["token_configured"] is False
        assert status["provider"] == "github"

    @pytest.mark.asyncio
    async def test_enqueue_is_idempotent_and_rejects_fingerprint_reuse(self, repos):
        repo, pr_repo = repos
        await repo.save_recommendations([_record()])
        rec_id = (await repo.get_active_recommendations())[0].id
        service = _service(repo, pr_repo, FakeProvider({"apps/api.yaml": MANIFEST}))
        operations = SQLiteAutomationOperationRepository(db_manager)

        first = await service.enqueue_recommendation_pr(
            rec_id, ApplyPrRequest(), operations, idempotency_key="request-1", actor="ci"
        )
        second = await service.enqueue_recommendation_pr(
            rec_id, ApplyPrRequest(), operations, idempotency_key="request-1", actor="ci"
        )
        assert first.id == second.id

        with pytest.raises(ValueError, match="different operation"):
            await service.enqueue_recommendation_pr(
                rec_id, ApplyPrRequest(base_branch="release"), operations, idempotency_key="request-1"
            )


class ShaShiftFakeProvider(FakeProvider):
    """FakeProvider where the head branch already carries a different file SHA."""

    async def get_file(self, repo, path, ref):
        content = self.files.get(path)
        if content is None:
            return None
        sha = "head-sha" if ref.startswith("greenkube/reco-") else "base-sha"
        return GitFile(path=path, content=content, sha=sha)


class TestApplyPrHardening:
    @pytest.mark.asyncio
    async def test_apply_uses_file_sha_from_the_head_branch(self, repos):
        repo, pr_repo = repos
        await repo.save_recommendations([_record()])
        rec_id = (await repo.get_active_recommendations())[0].id
        provider = ShaShiftFakeProvider({"apps/api.yaml": MANIFEST})

        first = await _service(repo, pr_repo, provider).apply_recommendation_pr(rec_id, ApplyPrRequest())
        second = await _service(repo, pr_repo, provider).apply_recommendation_pr(rec_id, ApplyPrRequest())

        assert first.status == "pr_open"
        assert second.status == "pr_open"
        assert provider.updates[-1]["sha"] == "head-sha"

    @pytest.mark.asyncio
    async def test_invalid_base_branch_returns_error(self, repos):
        repo, pr_repo = repos
        await repo.save_recommendations([_record()])
        rec_id = (await repo.get_active_recommendations())[0].id
        provider = FakeProvider({"apps/api.yaml": MANIFEST})

        response = await _service(repo, pr_repo, provider).apply_recommendation_pr(
            rec_id, ApplyPrRequest(base_branch="../../etc")
        )

        assert response.status == "error"
        assert provider.updates == []
