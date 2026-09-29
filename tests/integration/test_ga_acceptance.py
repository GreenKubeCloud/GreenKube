"""Release acceptance test for the GitOps recommendation control loop.

The test deliberately uses a real SQLite schema and the production services.
Only the external Git provider is deterministic, so this remains runnable in
CI without credentials or a Kubernetes cluster.
"""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.automation.git.base import GitFile, GitProvider, GitRepository, PullRequestInfo
from greenkube.automation.service import AutomationService
from greenkube.automation.source_resolver import ANNOTATION_PATH, ANNOTATION_REPO, AnnotationSourceResolver
from greenkube.core.db import db_manager
from greenkube.core.optimization.lifecycle import RecommendationLifecycle
from greenkube.core.savings_attributor import SavingsAttributor
from greenkube.models.metrics import ApplyPrRequest, RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.storage.sqlite.pull_request_repository import SQLitePullRequestRepository
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository
from greenkube.storage.sqlite.savings_repository import SQLiteSavingsLedgerRepository

MANIFEST = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout
  namespace: production
spec:
  template:
    spec:
      containers:
        - name: checkout
          resources:
            requests:
              cpu: 1000m
"""


class AcceptanceGitProvider(GitProvider):
    """Credential-free provider double that preserves the Git contract."""

    name = "acceptance"

    def __init__(self) -> None:
        super().__init__("acceptance-token")
        self.files = {"apps/checkout.yaml": MANIFEST}
        self.pull_requests: list[dict[str, object]] = []

    async def get_default_branch(self, repo: GitRepository) -> str:
        return "main"

    async def get_file(self, repo: GitRepository, path: str, ref: str) -> GitFile | None:
        content = self.files.get(path)
        return GitFile(path=path, content=content, sha="manifest-sha") if content else None

    async def list_files(self, repo: GitRepository, ref: str) -> list[str]:
        return list(self.files)

    async def create_branch(self, repo: GitRepository, branch: str, from_ref: str) -> None:
        return None

    async def update_file(
        self, repo: GitRepository, path: str, content: str, message: str, branch: str, sha: str | None = None
    ) -> None:
        self.files[path] = content

    async def create_pull_request(
        self, repo: GitRepository, *, head: str, base: str, title: str, body: str
    ) -> PullRequestInfo:
        self.pull_requests.append({"head": head, "base": base, "title": title, "body": body})
        return PullRequestInfo(number=42, url="https://example.test/pulls/42", status="open")

    async def get_pull_request(self, repo: GitRepository, number: int) -> PullRequestInfo:
        return PullRequestInfo(number=number, url="https://example.test/pulls/42", status="merged")


@pytest.fixture
async def acceptance_repositories():
    await db_manager.setup_sqlite(db_path=":memory:")
    yield (
        SQLiteRecommendationRepository(db_manager),
        SQLitePullRequestRepository(db_manager),
        SQLiteSavingsLedgerRepository(db_manager),
    )
    await db_manager.close()


@pytest.mark.asyncio
async def test_recommendation_to_rollback_control_loop_is_release_ready(acceptance_repositories):
    """A PR merge, verification, ledger attribution and rollback are auditable."""
    recommendation_repo, pull_request_repo, savings_repo = acceptance_repositories
    record = RecommendationRecord(
        pod_name="checkout",
        namespace="production",
        owner_kind="Deployment",
        owner_name="checkout",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="Reduce an oversized checkout workload",
        current_cpu_request_millicores=1000,
        recommended_cpu_request_millicores=500,
        potential_savings_cost=876.0,
        potential_savings_co2e_grams=8760.0,
        expires_at=datetime.now(timezone.utc) + timedelta(days=14),
        patch={
            "kind": "Deployment",
            "namespace": "production",
            "name": "checkout",
            "operations": [{"op": "set_container_resources", "resource": "cpu", "field": "requests", "value": "500m"}],
        },
    )
    await recommendation_repo.save_recommendations([record])
    saved = (await recommendation_repo.get_active_recommendations())[0]
    assert saved.id is not None

    provider = AcceptanceGitProvider()
    resolver = AnnotationSourceResolver(
        _AnnotationReader(
            {
                ANNOTATION_REPO: "https://example.test/green/manifests.git",
                ANNOTATION_PATH: "apps/checkout.yaml",
            }
        )
    )
    service = AutomationService(
        reco_repo=recommendation_repo,
        pr_repo=pull_request_repo,
        resolver=resolver,
        provider=provider,
    )
    opened = await service.apply_recommendation_pr(saved.id, request=ApplyPrRequest())
    assert opened.status == "pr_open"
    assert provider.pull_requests and "500m" in provider.files["apps/checkout.yaml"]

    lifecycle = RecommendationLifecycle(recommendation_repo)
    applied = await lifecycle.apply(
        saved.id,
        actual_cpu=500,
        application_method="pr_merge",
        actor="acceptance",
    )
    assert applied.status == RecommendationStatus.APPLIED
    verified = await lifecycle.mark_verified(
        saved.id,
        measured_co2e_saved_grams=8000.0,
        measured_cost_saved=800.0,
        verification_window_start=applied.applied_at or datetime.now(timezone.utc),
        verification_window_end=datetime.now(timezone.utc),
    )
    assert verified.status == RecommendationStatus.VERIFIED

    attributed = await SavingsAttributor(savings_repo, "acceptance-cluster").attribute_period(
        [verified],
        period_start=datetime.now(timezone.utc) - timedelta(minutes=5),
        period_end=datetime.now(timezone.utc),
    )
    assert attributed == 1
    totals = await savings_repo.get_cumulative_totals(cluster_name="acceptance-cluster")
    assert totals["RIGHTSIZING_CPU"]["cost_saved_dollars"] > 0

    rollback_record = record.model_copy(update={"pod_name": "checkout-rollback"})
    await recommendation_repo.save_recommendations([rollback_record])
    rollback_saved = next(
        item for item in await recommendation_repo.get_active_recommendations() if item.pod_name == "checkout-rollback"
    )
    await lifecycle.apply(rollback_saved.id, actual_cpu=500, application_method="pr_merge", actor="acceptance")
    rolled_back = await lifecycle.mark_rollback_review(rollback_saved.id, reasons=["acceptance health gate"])
    assert rolled_back.status == RecommendationStatus.ROLLBACK_REVIEW
    assert (await savings_repo.get_cumulative_totals(cluster_name="acceptance-cluster"))["RIGHTSIZING_CPU"][
        "cost_saved_dollars"
    ] > 0


class _AnnotationReader:
    def __init__(self, annotations: dict[str, str]) -> None:
        self.annotations = annotations

    async def read_annotations(self, namespace: str | None, kind: str | None, name: str | None) -> dict[str, str]:
        return self.annotations
