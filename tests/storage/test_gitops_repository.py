"""Parity contract tests for durable GitOps repository storage."""

import asyncio
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.gitops.bindings.manual import prepare_manual_binding_resolution
from greenkube.gitops.bindings.resolver import WorkloadBindingResolver
from greenkube.gitops.contracts import (
    ActionPlanV3,
    ActionStatus,
    AddHorizontalPodAutoscaler,
    BindingStatus,
    ControllerKind,
    FileEdit,
    FilePlan,
    Plan,
    PlanStatus,
    PreApplyWorkloadBaseline,
    PullRequestEvent,
    RendererKind,
    ResourceTarget,
    ValidationResult,
    WorkloadSource,
    binding_candidate_generation_token,
)
from greenkube.models.metrics import PullRequestRecord, PullRequestStatus
from greenkube.storage.base_gitops_repository import (
    GitOpsProviderConfig,
    PullRequestPollingLeaseNotOwnedError,
    WorkerHeartbeat,
    WorkloadBinding,
    WorkloadBindingConflictError,
    WorkloadBindingEvent,
)
from greenkube.storage.postgres.gitops_repository import PostgresGitOpsRepository
from greenkube.storage.postgres.pull_request_repository import PostgresPullRequestRepository
from greenkube.storage.sqlite.gitops_repository import SQLiteGitOpsRepository
from greenkube.storage.sqlite.pull_request_repository import SQLitePullRequestRepository
from greenkube.storage.unit_of_work import UnitOfWork


@pytest.fixture(params=("sqlite", "postgres"), ids=("sqlite", "postgres"))
async def gitops_store(request, monkeypatch):
    backend = request.param
    postgres_dsn = None
    schema = None

    if backend == "postgres":
        postgres_dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not postgres_dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run live PostgreSQL GitOps repository tests.")
        schema = f"gk_gitops_repo_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", postgres_dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", ":memory:")
        monkeypatch.setenv("DB_SCHEMA", "public")

    monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    repository = PostgresGitOpsRepository(manager) if backend == "postgres" else SQLiteGitOpsRepository(manager)

    try:
        yield repository, manager, backend
    finally:
        await manager.close()
        if postgres_dsn is not None and schema is not None:
            connection = await asyncpg.connect(postgres_dsn)
            try:
                await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            finally:
                await connection.close()


@pytest.mark.asyncio
async def test_stale_orphan_pending_pull_requests_are_quarantined(gitops_store):
    _, manager, backend = gitops_store
    repository = (
        PostgresPullRequestRepository(manager) if backend == "postgres" else SQLitePullRequestRepository(manager)
    )
    now = datetime.now(timezone.utc)
    stale_orphan = await repository.save_pull_request(
        PullRequestRecord(
            recommendation_id=501,
            provider="gitea",
            repo="acme/gitops",
            head_branch="greenkube/plan/orphaned",
            status=PullRequestStatus.PENDING,
            created_at=now - timedelta(hours=2),
        )
    )
    assert stale_orphan.id is not None
    fresh_orphan = await repository.save_pull_request(
        PullRequestRecord(
            recommendation_id=502,
            provider="gitea",
            repo="acme/gitops",
            status=PullRequestStatus.PENDING,
            created_at=now - timedelta(minutes=5),
        )
    )
    assert fresh_orphan.id is not None
    linked_pending = await repository.save_pull_request(
        PullRequestRecord(
            recommendation_id=503,
            provider="gitea",
            provider_id="gitea-main",
            repo="acme/gitops",
            head_branch="greenkube/plan/linked",
            plan_id="plan-503",
            status=PullRequestStatus.PENDING,
            created_at=now - timedelta(hours=2),
        )
    )
    assert linked_pending.id is not None
    provider_referenced = await repository.save_pull_request(
        PullRequestRecord(
            recommendation_id=504,
            provider="gitea",
            repo="acme/gitops",
            pr_number=54,
            pr_url="http://gitea/acme/gitops/pulls/54",
            status=PullRequestStatus.PENDING,
            created_at=now - timedelta(hours=2),
        )
    )
    assert provider_referenced.id is not None

    reconciled = await repository.reconcile_open_pull_requests(
        stale_before=now - timedelta(minutes=15),
        limit=10,
    )

    assert reconciled == 1
    recovered = await repository.get_pull_request_by_id(stale_orphan.id)
    assert recovered is not None
    assert recovered.status is PullRequestStatus.ERROR
    assert recovered.error == "orphaned_pending_attempt_missing_plan_provider_reference"
    fresh = await repository.get_pull_request_by_id(fresh_orphan.id)
    linked = await repository.get_pull_request_by_id(linked_pending.id)
    provider_reference = await repository.get_pull_request_by_id(provider_referenced.id)
    assert fresh is not None and fresh.status is PullRequestStatus.PENDING
    assert linked is not None and linked.status is PullRequestStatus.PENDING
    assert provider_reference is not None and provider_reference.status is PullRequestStatus.PENDING


def _source(
    *,
    source_id: str = "source-api",
    status: BindingStatus = BindingStatus.RESOLVED,
    confidence: float = 0.95,
    repo_url: str = "https://github.com/acme/platform",
    workload_name: str = "api",
    observed_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> WorkloadSource:
    observed_at = observed_at or datetime.now(timezone.utc)
    return WorkloadSource(
        id=source_id,
        cluster="cluster-a",
        namespace="production",
        workload_kind="Deployment",
        workload_name=workload_name,
        provider_id="provider-github",
        repo_url=repo_url,
        ref="refs/heads/main",
        revision="rev-123",
        path="apps/api",
        controller_kind=ControllerKind.ARGOCD,
        controller_name="production",
        controller_namespace="argocd",
        controller_uid="application-uid",
        controller_generation=7,
        source_uid="repository-uid",
        source_generation=3,
        source_revision="rev-123",
        renderer=RendererKind.RAW,
        renderer_config={"kind": "raw"},
        confidence=confidence,
        status=status,
        evidence={"controller": "application"},
        observed_at=observed_at,
        expires_at=expires_at,
        reason="manifest source",
    )


def _file_plan(plan_id: str) -> FilePlan:
    digest = f"sha256:{'a' * 64}"
    return FilePlan(
        plan_id=plan_id,
        files=[
            FileEdit(path="apps/api/deployment.yaml", operation="create", content="apiVersion: apps/v1\n"),
            FileEdit(
                path="apps/api/values.yaml",
                operation="update",
                original_sha="original-values-sha",
                content="replicas: 3\n",
            ),
            FileEdit(
                path="apps/api/legacy.yaml",
                operation="delete",
                original_sha="legacy-manifest-sha",
            ),
        ],
        diff="diff --git a/deployment.yaml b/deployment.yaml",
        rendered_diff="--- before\n+++ after\n",
        base_revision="base-revision",
        source_revision="source-revision",
        renderer=RendererKind.RAW,
        validation=ValidationResult(
            target_changed=True,
            unrelated_changes=False,
            requests_within_limits=True,
            renderer=RendererKind.RAW,
            output_digest=f"sha256:{'b' * 64}",
        ),
        warnings=["review rendered manifest"],
        digest=digest,
    )


def _plan(
    source: WorkloadSource,
    plan_id: str = "plan-api",
    *,
    rollback_of_plan_id: str | None = None,
) -> Plan:
    file_plan = _file_plan(plan_id)
    action = ActionPlanV3(
        action_id="action-api",
        recommendation_ids=[9101, 9102],
        target=ResourceTarget(
            api_version="apps/v1",
            kind="Deployment",
            namespace="production",
            name="api",
        ),
        operations=[AddHorizontalPodAutoscaler(min_replicas=2, max_replicas=5)],
        evidence_ref="recommendations:9101,9102",
    )
    return Plan(
        plan_id=plan_id,
        recommendation_ids=[9101, 9102],
        source=source,
        action=action,
        file_plan=file_plan,
        status=PlanStatus.VALIDATED,
        digest=file_plan.digest,
        change_set_id="change-set-api",
        rollback_of_plan_id=rollback_of_plan_id,
        created_at=datetime(2026, 10, 20, 12, 30, tzinfo=timezone.utc),
        expires_at=datetime(2026, 10, 22, 12, 30, tzinfo=timezone.utc),
    )


async def _insert_recommendations(manager, backend: str) -> None:
    async with manager.connection_scope() as connection:
        if backend == "postgres":
            await connection.executemany(
                """
                INSERT INTO recommendation_history (id, type, description, created_at, status)
                VALUES ($1, $2, $3, $4, $5)
                """,
                [
                    (9101, "rightsizing", "Recommendation one", datetime.now(timezone.utc), "active"),
                    (9102, "rightsizing", "Recommendation two", datetime.now(timezone.utc), "active"),
                ],
            )
        else:
            await connection.executemany(
                """
                INSERT INTO recommendation_history (id, type, description, created_at, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (9101, "rightsizing", "Recommendation one", datetime.now(timezone.utc).isoformat(), "active"),
                    (9102, "rightsizing", "Recommendation two", datetime.now(timezone.utc).isoformat(), "active"),
                ],
            )
            await connection.commit()


@pytest.mark.asyncio
async def test_discovery_refresh_generation_only_changes_for_material_binding_updates(gitops_store):
    repository, _, _ = gitops_store
    provider_id = "provider-github"
    await repository.save_provider_config(GitOpsProviderConfig(id=provider_id, provider_type="github", status="ready"))
    now = datetime.now(timezone.utc)
    source = _source(
        source_id="discovery-generation-source",
        observed_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    binding = WorkloadBinding(
        cluster=source.cluster,
        namespace=source.namespace,
        workload_kind=source.workload_kind,
        workload_name=source.workload_name,
        status=BindingStatus.RESOLVED,
        confidence=source.confidence,
        source=source,
        candidates=(source,),
        evidence={"workload_labels": {"app": source.workload_name}},
        observed_at=now,
        expires_at=source.expires_at,
    )

    saved = await repository.save_discovered_workload_binding(binding, expected_generation=None)
    assert saved is not None

    refreshed_at = now + timedelta(minutes=1)
    refreshed_source = source.model_copy(
        update={
            "observed_at": refreshed_at,
            "expires_at": refreshed_at + timedelta(minutes=30),
        }
    )
    refreshed = await repository.save_discovered_workload_binding(
        WorkloadBinding(
            cluster=binding.cluster,
            namespace=binding.namespace,
            workload_kind=binding.workload_kind,
            workload_name=binding.workload_name,
            status=binding.status,
            confidence=binding.confidence,
            source=refreshed_source,
            candidates=(refreshed_source,),
            evidence=binding.evidence,
            observed_at=refreshed_at,
            expires_at=refreshed_source.expires_at,
        ),
        expected_generation=saved.generation,
    )
    assert refreshed is not None
    assert refreshed.generation == saved.generation
    assert refreshed.observed_at == refreshed_at

    changed_source = refreshed_source.model_copy(update={"revision": "rev-124", "source_revision": "rev-124"})
    changed = await repository.save_discovered_workload_binding(
        WorkloadBinding(
            cluster=binding.cluster,
            namespace=binding.namespace,
            workload_kind=binding.workload_kind,
            workload_name=binding.workload_name,
            status=binding.status,
            confidence=binding.confidence,
            source=changed_source,
            candidates=(changed_source,),
            evidence=binding.evidence,
            observed_at=refreshed_at,
            expires_at=changed_source.expires_at,
        ),
        expected_generation=refreshed.generation,
    )
    assert changed is not None
    assert changed.generation == refreshed.generation + 1


@pytest.mark.asyncio
async def test_plan_and_action_state_updates_use_compare_and_set(gitops_store):
    repository, manager, backend = gitops_store
    await _insert_recommendations(manager, backend)
    await repository.save_provider_config(
        GitOpsProviderConfig(id="provider-github", provider_type="github", status="ready")
    )
    plan = _plan(_source())
    await repository.save_plan(plan)

    first, second = await asyncio.gather(
        repository.set_plan_status(
            plan.plan_id,
            PlanStatus.APPROVED,
            expected_statuses=(PlanStatus.VALIDATED,),
            expected_digest=plan.digest,
        ),
        repository.set_plan_status(
            plan.plan_id,
            PlanStatus.REJECTED,
            expected_statuses=(PlanStatus.VALIDATED,),
            expected_digest=plan.digest,
        ),
    )
    assert sum(result is not None for result in (first, second)) == 1
    current = await repository.get_plan(plan.plan_id)
    assert current is not None
    assert current.status in {PlanStatus.APPROVED, PlanStatus.REJECTED}

    assert (
        await repository.set_recommendation_action_status(
            [9101],
            ActionStatus.PLAN_READY,
            expected_statuses=(ActionStatus.NONE,),
        )
        == 1
    )
    assert (
        await repository.set_recommendation_action_status(
            [9101],
            ActionStatus.APPLIED,
            expected_statuses=(ActionStatus.MERGED,),
        )
        == 0
    )
    assert await repository.get_recommendation_action_status(9101) is ActionStatus.PLAN_READY


@pytest.mark.asyncio
async def test_plan_and_action_transitions_are_written_to_recommendation_audit(gitops_store):
    repository, manager, backend = gitops_store
    await _insert_recommendations(manager, backend)
    await repository.save_provider_config(
        GitOpsProviderConfig(id="provider-github", provider_type="github", status="ready")
    )
    plan = _plan(_source())
    actor = "api-key:0123456789abcdef"
    await repository.save_plan(plan, actor=actor)
    await repository.set_plan_status(
        plan.plan_id,
        PlanStatus.APPROVED,
        expected_statuses=(PlanStatus.VALIDATED,),
        expected_digest=plan.digest,
        actor=actor,
    )
    await repository.set_recommendation_action_status(
        [9101],
        ActionStatus.PLAN_READY,
        expected_statuses=(ActionStatus.NONE,),
        actor=actor,
        plan_id=plan.plan_id,
    )

    async with manager.connection_scope() as connection:
        rows = await repository._fetch(
            connection,
            """
            SELECT event_type, actor, payload
            FROM recommendation_events
            WHERE recommendation_id = ?
            ORDER BY id
            """,
            (9101,),
        )

    events = [
        (
            row["event_type"],
            row["actor"],
            json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"],
        )
        for row in rows
    ]
    assert [event[0] for event in events] == [
        "plan_created",
        "plan_status_changed",
        "plan_action_status_changed",
    ]
    assert all(event[1] == actor for event in events)
    assert events[0][2]["details"]["plan_id"] == plan.plan_id
    assert events[1][2]["details"]["new_status"] == PlanStatus.APPROVED.value
    assert events[2][2]["details"]["new_action_status"] == ActionStatus.PLAN_READY.value


@pytest.mark.asyncio
async def test_repository_credentials_are_removed_from_persisted_sources(gitops_store):
    repository, manager, backend = gitops_store
    await _insert_recommendations(manager, backend)
    await repository.save_provider_config(
        GitOpsProviderConfig(id="provider-github", provider_type="github", status="ready")
    )
    secret = "private-repository-token"
    source = _source(repo_url=f"https://robot:{secret}@github.example.test/acme/platform?token={secret}")
    plan = _plan(source)
    await repository.save_plan(plan)

    stored_plan = await repository.get_plan(plan.plan_id)
    async with manager.connection_scope() as connection:
        source_rows = await repository._fetch(
            connection, "SELECT repo_url FROM workload_sources WHERE id = ?", (source.id,)
        )
        plan_rows = await repository._fetch(
            connection,
            "SELECT source_snapshot FROM automation_plans WHERE plan_id = ?",
            (plan.plan_id,),
        )

    assert stored_plan is not None
    assert stored_plan.source.repo_url == "https://github.example.test/acme/platform"
    assert source_rows[0]["repo_url"] == "https://github.example.test/acme/platform"
    snapshot = plan_rows[0]["source_snapshot"]
    snapshot = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
    assert snapshot["repo_url"] == "https://github.example.test/acme/platform"
    assert secret not in str(snapshot)


@pytest.mark.asyncio
async def test_worker_heartbeat_retention_removes_only_stale_rows(gitops_store):
    repository, _, _ = gitops_store
    now = datetime.now(timezone.utc)
    await repository.upsert_worker_heartbeat(
        WorkerHeartbeat(worker_id="stale-worker", heartbeat_at=now - timedelta(days=8))
    )
    await repository.upsert_worker_heartbeat(WorkerHeartbeat(worker_id="active-worker", heartbeat_at=now))

    removed = await repository.prune_worker_heartbeats(cutoff=now - timedelta(days=7))

    assert removed == 1
    assert await repository.get_worker_heartbeat("stale-worker") is None
    assert await repository.get_worker_heartbeat("active-worker") is not None


@pytest.mark.asyncio
async def test_automation_backlog_summary_reports_stuck_plans_operations_webhooks_and_workers(gitops_store):
    repository, manager, backend = gitops_store
    await _insert_recommendations(manager, backend)
    await repository.save_provider_config(
        GitOpsProviderConfig(id="provider-github", provider_type="github", status="ready")
    )
    now = datetime.now(timezone.utc)
    stale = now - timedelta(minutes=30)
    plans = {
        "applying": _plan(_source(), "plan-backlog-applying").model_copy(
            update={"status": PlanStatus.APPLYING, "created_at": now, "expires_at": now + timedelta(days=1)}
        ),
        "merged": _plan(_source(), "plan-backlog-merged").model_copy(
            update={"status": PlanStatus.MERGED, "created_at": now, "expires_at": now + timedelta(days=1)}
        ),
        "queued": _plan(_source(), "plan-backlog-queued").model_copy(
            update={"created_at": now, "expires_at": now + timedelta(days=1)}
        ),
        "retrying": _plan(_source(), "plan-backlog-retrying").model_copy(
            update={"created_at": now, "expires_at": now + timedelta(days=1)}
        ),
        "failed": _plan(_source(), "plan-backlog-failed").model_copy(
            update={"status": PlanStatus.FAILED, "created_at": now, "expires_at": now + timedelta(days=1)}
        ),
        "sync-failed": _plan(_source(), "plan-backlog-sync-failed").model_copy(
            update={"status": PlanStatus.SYNC_FAILED, "created_at": now, "expires_at": now + timedelta(days=1)}
        ),
    }
    for plan in plans.values():
        await repository.save_plan(plan)

    async with repository._transaction() as connection:
        for name, status, updated_at in (
            ("applying", "running", stale),
            ("merged", "running", stale),
            ("queued", "queued", stale),
            ("retrying", "retrying", now),
            ("failed", "failed", now),
        ):
            plan = plans[name]
            running = status == "running"
            await repository._execute(
                connection,
                """
                INSERT INTO automation_plan_operations (
                    operation_id, plan_id, plan_digest, idempotency_key, status, attempts,
                    available_at, locked_at, worker_id, lease_id, lease_expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"operation-backlog-{name}",
                    plan.plan_id,
                    plan.digest,
                    f"plan:{plan.plan_id}:{plan.digest}",
                    status,
                    1 if status in {"retrying", "failed"} else 0,
                    repository._db_datetime(updated_at),
                    repository._db_datetime(updated_at) if running else None,
                    f"worker-{name}" if running else None,
                    f"lease-{name}" if running else None,
                    repository._db_datetime(stale) if running else None,
                    repository._db_datetime(updated_at),
                    repository._db_datetime(updated_at),
                ),
            )
        for name in ("applying", "merged"):
            await repository._execute(
                connection,
                "UPDATE automation_plans SET updated_at = ? WHERE plan_id = ?",
                (repository._db_datetime(stale), plans[name].plan_id),
            )

    stuck_event = PullRequestEvent(
        provider_id="provider-github",
        delivery_id="delivery-stuck",
        repo="acme/platform",
        number=81,
        action="closed",
        merged=False,
        occurred_at=stale,
    )
    pending_event = stuck_event.model_copy(update={"delivery_id": "delivery-pending", "number": 82})
    await repository.record_webhook_delivery(stuck_event)
    await repository.update_webhook_delivery(
        stuck_event.provider_id,
        stuck_event.delivery_id,
        status="retryable",
    )
    await repository.record_webhook_delivery(pending_event)
    async with repository._transaction() as connection:
        await repository._execute(
            connection,
            """
            UPDATE webhook_deliveries SET updated_at = ?
            WHERE provider_id = ? AND delivery_id = ?
            """,
            (repository._db_datetime(stale), stuck_event.provider_id, stuck_event.delivery_id),
        )

    await repository.upsert_worker_heartbeat(
        WorkerHeartbeat(worker_id="backlog-stale-worker", status="idle", heartbeat_at=stale)
    )
    await repository.upsert_worker_heartbeat(
        WorkerHeartbeat(worker_id="backlog-active-worker", status="idle", heartbeat_at=now)
    )

    summary = await repository.get_automation_backlog_summary(stale_before=now - timedelta(minutes=15))

    assert summary.queued_operations == 1
    assert summary.running_operations == 2
    assert summary.retrying_operations == 1
    assert summary.failed_operations == 1
    assert summary.stuck_operations == 3
    assert summary.applying_plans == 1
    assert summary.merged_plans == 1
    assert summary.stuck_plans == 2
    assert summary.sync_failed_plans == 1
    assert summary.pending_webhooks == 2
    assert summary.stuck_webhooks == 1
    assert summary.worker_count == 2
    assert summary.stale_workers == 1
    assert summary.latest_worker_heartbeat_timestamp > 0


@pytest.mark.asyncio
async def test_automation_history_retention_prunes_terminal_payloads_but_keeps_plan(gitops_store):
    repository, manager, backend = gitops_store
    await _insert_recommendations(manager, backend)
    await repository.save_provider_config(
        GitOpsProviderConfig(id="provider-github", provider_type="github", status="ready")
    )
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=100)
    plan = _plan(_source())
    await repository.save_plan(plan)
    await repository.set_plan_status(plan.plan_id, PlanStatus.FAILED)
    event = PullRequestEvent(
        provider_id="provider-github",
        delivery_id="old-delivery",
        repo="acme/platform",
        number=42,
        action="closed",
        merged=False,
        occurred_at=old,
    )
    await repository.record_webhook_delivery(event)
    await repository.update_webhook_delivery(
        event.provider_id,
        event.delivery_id,
        status="processed",
        processed_at=old,
    )
    async with repository._transaction() as connection:
        await repository._execute(
            connection,
            "INSERT INTO recommendation_events (recommendation_id, event_type, actor, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (1, "plan_status_changed", "tester", "{}", repository._db_datetime(old)),
        )
        await repository._execute(
            connection,
            """
            INSERT INTO workload_binding_events
                (event_key, cluster, namespace, workload_kind, workload_name, event_type, actor, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "old-binding-event",
                "prod",
                "payments",
                "Deployment",
                "api",
                "manual_resolution_expired",
                "system",
                "{}",
                repository._db_datetime(old),
            ),
        )
        await repository._execute(
            connection,
            "UPDATE automation_plans SET updated_at = ? WHERE plan_id = ?",
            (repository._db_datetime(old), plan.plan_id),
        )
        await repository._execute(
            connection,
            """
            INSERT INTO automation_plan_operations
                (operation_id, plan_id, plan_digest, idempotency_key, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "old-operation",
                plan.plan_id,
                plan.digest,
                "old-idempotency-key",
                "failed",
                repository._db_datetime(old),
            ),
        )

    pruned = await repository.prune_automation_history(cutoff=now - timedelta(days=90))

    assert pruned == {
        "plan_files": 3,
        "operations": 1,
        "webhooks": 1,
        "recommendation_events": 1,
        "binding_events": 1,
    }
    retained = await repository.get_plan(plan.plan_id)
    assert retained is not None
    assert retained.status is PlanStatus.FAILED
    assert retained.file_plan is None


@pytest.mark.asyncio
async def test_gitops_repository_starts_empty_and_persists_typed_provider_bindings(gitops_store):
    repository, _, _ = gitops_store
    assert await repository.list_provider_configs() == []
    assert await repository.list_workload_bindings() == []
    assert await repository.list_plans() == []
    assert await repository.list_worker_heartbeats() == []

    with pytest.raises(ValueError, match="set together"):
        GitOpsProviderConfig(id="incomplete-provider", provider_type="github", secret_name="token")

    provider = GitOpsProviderConfig(
        id="provider-github",
        provider_type="github",
        api_base_url="https://api.github.com",
        secret_namespace="greenkube",
        secret_name="github-token",
        secret_key="token",
        capabilities={"contents_write": True, "pull_requests": True},
    )
    saved_provider = await repository.save_provider_config(provider)
    assert saved_provider.id == provider.id
    assert saved_provider.secret_name == "github-token"
    assert saved_provider.secret_key == "token"
    assert saved_provider.capabilities == provider.capabilities
    resaved_provider = await repository.save_provider_config(provider)
    assert resaved_provider.id == saved_provider.id
    assert resaved_provider.capabilities == saved_provider.capabilities
    assert await repository.get_provider_config(provider.id) == resaved_provider
    assert await repository.list_provider_configs(enabled_only=True) == [resaved_provider]

    await repository.add_provider_allowlist(provider.id, "acme/*")
    await repository.add_provider_allowlist(provider.id, "acme/*")
    await repository.add_provider_allowlist(provider.id, "acme/platform")
    assert await repository.list_provider_allowlists(provider.id) == ["acme/*", "acme/platform"]
    assert await repository.remove_provider_allowlist(provider.id, "acme/platform")
    assert not await repository.remove_provider_allowlist(provider.id, "acme/platform")
    assert await repository.list_provider_allowlists(provider.id) == ["acme/*"]

    source = _source().model_copy(
        update={
            "evidence": {
                "source_ref": {
                    "kind": "GitRepository",
                    "name": "gitops",
                    "namespace": "flux-system",
                }
            }
        }
    )
    binding = WorkloadBinding(
        cluster=source.cluster,
        namespace=source.namespace,
        workload_kind=source.workload_kind,
        workload_name=source.workload_name,
        status=BindingStatus.RESOLVED,
        confidence=source.confidence,
        source=source,
        candidates=(source,),
        evidence={"resolved_by": "argocd"},
        reason="unique source",
        observed_at=source.observed_at,
        expires_at=source.expires_at,
    )
    saved_binding = await repository.save_workload_binding(binding)
    saved_again = await repository.save_workload_binding(binding)
    assert saved_again.generation == saved_binding.generation + 1
    loaded_binding = await repository.get_workload_binding(
        source.cluster, source.namespace, source.workload_kind, source.workload_name
    )
    assert loaded_binding == saved_again
    assert loaded_binding is not None
    assert loaded_binding.evidence == binding.evidence
    assert loaded_binding.source is not None
    assert loaded_binding.source.evidence == source.evidence
    assert loaded_binding.source.renderer is RendererKind.RAW
    assert loaded_binding.source.controller_kind is ControllerKind.ARGOCD
    assert loaded_binding.source.controller_uid == "application-uid"
    assert loaded_binding.source.controller_generation == 7
    assert loaded_binding.source.source_uid == "repository-uid"
    assert loaded_binding.source.source_generation == 3

    ambiguous_sources = (
        _source(
            source_id="source-api-one",
            status=BindingStatus.AMBIGUOUS,
            confidence=0.6,
            workload_name="ambiguous-api",
        ),
        _source(
            source_id="source-api-two",
            status=BindingStatus.AMBIGUOUS,
            confidence=0.5,
            repo_url="https://github.com/acme/platform-mirror",
            workload_name="ambiguous-api",
        ),
    )
    ambiguous = WorkloadBinding(
        cluster="cluster-a",
        namespace="production",
        workload_kind="Deployment",
        workload_name="ambiguous-api",
        status=BindingStatus.AMBIGUOUS,
        confidence=0.6,
        candidates=ambiguous_sources,
        evidence={"candidate_count": 2},
        reason="multiple repositories match",
    )
    await repository.save_workload_binding(ambiguous)
    loaded_ambiguous = await repository.get_workload_binding("cluster-a", "production", "Deployment", "ambiguous-api")
    assert loaded_ambiguous is not None
    assert loaded_ambiguous.status is BindingStatus.AMBIGUOUS
    assert loaded_ambiguous.source is None
    assert {candidate.id for candidate in loaded_ambiguous.candidates} == {"source-api-one", "source-api-two"}

    unresolved = WorkloadBinding(
        cluster="cluster-a",
        namespace="production",
        workload_kind="Deployment",
        workload_name="unmapped-api",
        status=BindingStatus.UNRESOLVED,
        confidence=0,
        reason="no source discovered",
    )
    saved_unresolved = await repository.save_workload_binding(unresolved)
    loaded_unresolved = await repository.get_workload_binding("cluster-a", "production", "Deployment", "unmapped-api")
    assert saved_unresolved.status is BindingStatus.UNRESOLVED
    assert loaded_unresolved == saved_unresolved
    assert len(await repository.list_workload_bindings(cluster="cluster-a")) == 3


@pytest.mark.asyncio
async def test_workload_binding_storage_projections_are_bounded_cursor_pages_with_fresh_statuses(
    gitops_store,
    monkeypatch,
):
    repository, _, _ = gitops_store
    await repository.save_provider_config(
        GitOpsProviderConfig(id="provider-github", provider_type="github", status="ready")
    )
    now = datetime.now(timezone.utc)

    active_source = _source(
        source_id="projection-active",
        workload_name="01-active",
        observed_at=now,
        expires_at=now + timedelta(minutes=15),
    )
    active = WorkloadBinding(
        cluster=active_source.cluster,
        namespace=active_source.namespace,
        workload_kind=active_source.workload_kind,
        workload_name=active_source.workload_name,
        status=BindingStatus.RESOLVED,
        confidence=active_source.confidence,
        source=active_source,
        candidates=(active_source,),
        observed_at=now,
        expires_at=now + timedelta(minutes=15),
    )

    ambiguous_sources = (
        _source(
            source_id="projection-ambiguous-one",
            workload_name="02-ambiguous",
            repo_url="https://github.com/acme/one",
            status=BindingStatus.AMBIGUOUS,
            observed_at=now,
        ),
        _source(
            source_id="projection-ambiguous-two",
            workload_name="02-ambiguous",
            repo_url="https://github.com/acme/two",
            status=BindingStatus.AMBIGUOUS,
            observed_at=now,
        ),
    )
    ambiguous = WorkloadBinding(
        cluster="cluster-a",
        namespace="production",
        workload_kind="Deployment",
        workload_name="02-ambiguous",
        status=BindingStatus.AMBIGUOUS,
        confidence=0.8,
        candidates=ambiguous_sources,
        observed_at=now,
        expires_at=now + timedelta(minutes=15),
    )

    blocked_source = _source(
        source_id="projection-blocked",
        workload_name="03-blocked",
        status=BindingStatus.BLOCKED,
        observed_at=now - timedelta(hours=2),
        expires_at=now - timedelta(minutes=1),
    )
    blocked = WorkloadBinding(
        cluster=blocked_source.cluster,
        namespace=blocked_source.namespace,
        workload_kind=blocked_source.workload_kind,
        workload_name=blocked_source.workload_name,
        status=BindingStatus.BLOCKED,
        confidence=blocked_source.confidence,
        source=blocked_source,
        candidates=(blocked_source,),
        observed_at=blocked_source.observed_at,
        expires_at=blocked_source.expires_at,
    )

    expired_source = _source(
        source_id="projection-stale",
        workload_name="04-stale",
        observed_at=now - timedelta(hours=2),
        expires_at=now - timedelta(minutes=1),
    )
    expired = WorkloadBinding(
        cluster=expired_source.cluster,
        namespace=expired_source.namespace,
        workload_kind=expired_source.workload_kind,
        workload_name=expired_source.workload_name,
        status=BindingStatus.RESOLVED,
        confidence=expired_source.confidence,
        source=expired_source,
        candidates=(expired_source,),
        observed_at=expired_source.observed_at,
        expires_at=expired_source.expires_at,
    )
    unresolved = WorkloadBinding(
        cluster="cluster-a",
        namespace="production",
        workload_kind="Deployment",
        workload_name="05-unresolved",
        status=BindingStatus.UNRESOLVED,
        confidence=0,
        reason="source-url-unresolved",
        observed_at=now,
    )
    for binding in (active, ambiguous, blocked, expired, unresolved):
        await repository.save_workload_binding(binding)

    fetches: list[str] = []
    fetch = repository._fetch

    async def track_fetch(connection, query, params=(), *, json_positions=()):
        fetches.append(query)
        return await fetch(connection, query, params, json_positions=json_positions)

    monkeypatch.setattr(repository, "_fetch", track_fetch)
    first = await repository.list_workload_bindings_page(
        cluster="cluster-a",
        limit=2,
        candidate_limit=1,
        now=now,
    )

    assert len(fetches) == 2
    assert [binding.workload_name for binding in first.items] == ["01-active", "02-ambiguous"]
    assert first.has_more is True
    assert first.next_cursor is not None
    assert first.items[1].status is BindingStatus.AMBIGUOUS
    assert len(first.items[1].candidates) == 1
    assert first.items[1].candidates_truncated is True

    fetches.clear()
    detail = await repository.get_workload_binding(
        "cluster-a",
        "production",
        "Deployment",
        "02-ambiguous",
    )
    assert len(fetches) == 1
    assert detail is not None
    assert len(detail.candidates) == 2

    second = await repository.list_workload_bindings_page(
        cluster="cluster-a",
        limit=2,
        cursor=first.next_cursor,
        now=now,
    )
    third = await repository.list_workload_bindings_page(
        cluster="cluster-a",
        limit=2,
        cursor=second.next_cursor,
        now=now,
    )
    stale = await repository.list_workload_bindings_page(
        cluster="cluster-a",
        limit=10,
        statuses=(BindingStatus.STALE,),
        now=now,
    )
    blocked_page = await repository.list_workload_bindings_page(
        cluster="cluster-a",
        limit=10,
        statuses=(BindingStatus.BLOCKED,),
        now=now,
    )
    coverage = await repository.get_workload_binding_coverage(cluster="cluster-a", now=now)

    assert [binding.workload_name for binding in second.items] == ["03-blocked", "04-stale"]
    assert second.items[0].status is BindingStatus.BLOCKED
    assert second.items[1].status is BindingStatus.STALE
    assert second.items[1].source is not None
    assert second.items[1].source.status is BindingStatus.STALE
    assert [binding.workload_name for binding in third.items] == ["05-unresolved"]
    assert third.has_more is False
    assert [binding.workload_name for binding in stale.items] == ["04-stale"]
    assert [binding.workload_name for binding in blocked_page.items] == ["03-blocked"]
    assert (coverage.total, coverage.resolved, coverage.ambiguous) == (5, 1, 1)
    assert (coverage.unresolved, coverage.blocked, coverage.stale) == (1, 1, 1)
    assert coverage.ratio == 0.2


@pytest.mark.asyncio
async def test_discovery_binding_save_uses_generation_cas_against_manual_resolution(gitops_store):
    repository, _, _ = gitops_store
    await repository.save_provider_config(
        GitOpsProviderConfig(id="provider-github", provider_type="github", status="ready")
    )
    now = datetime.now(timezone.utc)
    selected = _source(
        source_id="cas-candidate-one",
        workload_name="cas-api",
        repo_url="https://github.com/acme/one",
        observed_at=now,
    )
    competing = _source(
        source_id="cas-candidate-two",
        workload_name="cas-api",
        repo_url="https://github.com/acme/two",
        observed_at=now,
    )
    ambiguous = WorkloadBindingResolver().resolve([selected, competing], now=now)
    original = await repository.save_workload_binding(ambiguous)
    prepared = prepare_manual_binding_resolution(
        original,
        candidate_id=selected.id,
        generation_token=binding_candidate_generation_token(selected),
        actor="operator",
        now=now,
    )
    manual = await repository.save_manual_workload_binding(
        prepared.binding,
        prepared.event,
        expected_candidate_id=prepared.selected_candidate_id,
        expected_generation_token=prepared.generation_token,
        now=now,
    )

    stale_refresh_write = await repository.save_discovered_workload_binding(
        ambiguous,
        expected_generation=original.generation,
    )
    persisted = await repository.get_workload_binding(
        ambiguous.cluster,
        ambiguous.namespace,
        ambiguous.workload_kind,
        ambiguous.workload_name,
    )

    assert stale_refresh_write is None
    assert manual.generation == original.generation + 1
    assert persisted is not None
    assert persisted.status is BindingStatus.RESOLVED
    assert persisted.source is not None
    assert persisted.source.evidence["selected_via"] == "manual"
    assert persisted.generation == manual.generation


@pytest.mark.asyncio
async def test_binding_audit_events_are_durable_and_ordered(gitops_store):
    repository, manager, backend = gitops_store
    source = _source(source_id="manual-source-api")
    await repository.save_provider_config(
        GitOpsProviderConfig(
            id=source.provider_id,
            provider_type="github",
            secret_name="github-token",
            secret_key="token",
        )
    )
    now = datetime.now(timezone.utc)
    binding = WorkloadBinding(
        cluster=source.cluster,
        namespace=source.namespace,
        workload_kind=source.workload_kind,
        workload_name=source.workload_name,
        status=BindingStatus.RESOLVED,
        confidence=1.0,
        source=source,
        candidates=(source,),
        observed_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    created = WorkloadBindingEvent(
        cluster=source.cluster,
        namespace=source.namespace,
        workload_kind=source.workload_kind,
        workload_name=source.workload_name,
        event_type="manual_resolution_created",
        actor="operator@example.test",
        source_id=source.id,
        source_generation=f"sha256:{'a' * 64}",
        expires_at=now + timedelta(minutes=30),
        details={"selected_candidate_id": "source-api", "controller_generation": 7},
        created_at=now,
    )
    await repository.save_workload_binding_with_event(binding, created)
    await repository.record_workload_binding_event(
        replace(
            created,
            event_type="manual_resolution_expired",
            actor="system",
            details={"reason_code": "source-expired"},
            created_at=now + timedelta(seconds=1),
        )
    )
    await repository.record_workload_binding_event(
        replace(
            created,
            event_type="manual_resolution_invalidated",
            actor="system",
            details={"reason_code": "manual-resolution-source-changed"},
            created_at=now + timedelta(seconds=2),
        )
    )

    other_repository = PostgresGitOpsRepository(manager) if backend == "postgres" else SQLiteGitOpsRepository(manager)
    events = await other_repository.list_workload_binding_events(
        cluster=source.cluster,
        namespace=source.namespace,
        workload_kind=source.workload_kind,
        workload_name=source.workload_name,
    )

    assert [event.event_type for event in events] == [
        "manual_resolution_created",
        "manual_resolution_expired",
        "manual_resolution_invalidated",
    ]
    assert events[0].actor == "operator@example.test"
    assert events[0].source_generation == f"sha256:{'a' * 64}"
    assert events[0].expires_at == now + timedelta(minutes=30)
    assert events[0].details == {"selected_candidate_id": "source-api", "controller_generation": 7}
    assert all(event.id is not None for event in events)


@pytest.mark.asyncio
async def test_manual_binding_resolution_checks_generation_and_audits_atomically(gitops_store):
    repository, _, _ = gitops_store
    await repository.save_provider_config(
        GitOpsProviderConfig(
            id="provider-github",
            provider_type="github",
            status="ready",
        )
    )
    now = datetime.now(timezone.utc)
    selected = _source(
        source_id="candidate-one",
        repo_url="https://github.com/acme/one",
        workload_name="manual-api",
        observed_at=now,
        expires_at=None,
    )
    competing = _source(
        source_id="candidate-two",
        repo_url="https://github.com/acme/two",
        workload_name="manual-api",
        observed_at=now,
        expires_at=None,
    )
    binding = WorkloadBindingResolver().resolve([selected, competing], now=now)
    await repository.save_workload_binding(binding)
    generation_token = binding_candidate_generation_token(selected)
    prepared = prepare_manual_binding_resolution(
        binding,
        candidate_id=selected.id,
        generation_token=generation_token,
        actor="operator",
        now=now,
    )

    resolved = await repository.save_manual_workload_binding(
        prepared.binding,
        prepared.event,
        expected_candidate_id=selected.id,
        expected_generation_token=generation_token,
        now=now,
    )
    events = await repository.list_workload_binding_events(
        cluster=selected.cluster,
        namespace=selected.namespace,
        workload_kind=selected.workload_kind,
        workload_name=selected.workload_name,
    )

    assert resolved.status is BindingStatus.RESOLVED
    assert resolved.source is not None
    assert resolved.source.controller_kind is ControllerKind.ARGOCD
    assert [event.event_type for event in events] == ["manual_resolution_created"]
    assert events[0].source_generation == generation_token

    changed = selected.model_copy(update={"controller_generation": 8, "observed_at": now + timedelta(seconds=1)})
    current_binding = WorkloadBindingResolver().resolve([changed, competing], now=now + timedelta(seconds=1))
    await repository.save_workload_binding(current_binding)
    with pytest.raises(WorkloadBindingConflictError):
        await repository.save_manual_workload_binding(
            prepared.binding,
            prepared.event,
            expected_candidate_id=selected.id,
            expected_generation_token=generation_token,
            now=now + timedelta(seconds=1),
        )
    assert (
        await repository.list_workload_binding_events(
            cluster=selected.cluster,
            namespace=selected.namespace,
            workload_kind=selected.workload_kind,
            workload_name=selected.workload_name,
        )
        == events
    )


@pytest.mark.asyncio
async def test_plans_recommendations_webhook_dedupe_and_worker_heartbeat(gitops_store):
    repository, manager, backend = gitops_store
    await repository.save_provider_config(
        GitOpsProviderConfig(
            id="provider-github", provider_type="github", secret_name="github-token", secret_key="token"
        )
    )
    source = _source()
    await _insert_recommendations(manager, backend)

    plan = _plan(source)
    policy_result = {"allowed": True, "reasons": []}
    persisted_plan = plan.model_copy(update={"policy_result": policy_result})
    stored_plan = await repository.save_plan(plan, policy_result=policy_result)
    assert stored_plan == persisted_plan
    assert await repository.save_plan(plan, policy_result=policy_result) == persisted_plan
    assert await repository.get_plan(plan.plan_id) == persisted_plan
    plan_record = await repository.get_plan_record(plan.plan_id)
    assert plan_record is not None
    assert plan_record.plan == persisted_plan
    assert plan_record.policy_result == policy_result

    rollback_plan = _plan(source, "rbk-plan-api-rollback", rollback_of_plan_id=plan.plan_id)
    assert await repository.save_plan(rollback_plan) == rollback_plan
    stored_rollback = await repository.get_plan(rollback_plan.plan_id)
    assert stored_rollback is not None
    assert stored_rollback.rollback_of_plan_id == plan.plan_id
    approved_rollback = await repository.set_plan_status(rollback_plan.plan_id, PlanStatus.APPROVED)
    assert approved_rollback is not None
    assert await repository.save_plan_if_absent(rollback_plan) == approved_rollback
    assert await repository.list_plans(status=PlanStatus.VALIDATED) == [persisted_plan]
    assert await repository.list_recommendation_ids_for_plan(plan.plan_id) == [9101, 9102]
    followup_plan = Plan(
        plan_id="plan-api-followup",
        recommendation_ids=[9101],
        source=source,
        action=ActionPlanV3(
            action_id="action-api-followup",
            recommendation_ids=[9101],
            target=plan.action.target,
            operations=plan.action.operations,
            evidence_ref="recommendations:9101",
        ),
    )
    await repository.save_plan(followup_plan)
    assert await repository.list_recommendation_ids_for_plan(followup_plan.plan_id) == [9101]
    assert await repository.list_plans_for_workload(
        cluster=source.cluster,
        namespace=source.namespace,
        workload_kind=source.workload_kind,
        workload_name=source.workload_name,
        statuses=(PlanStatus.VALIDATED,),
    ) == [persisted_plan]
    assert await repository.list_plans_for_recommendation(9101) == [
        persisted_plan,
        followup_plan,
        approved_rollback,
    ]

    assert await repository.set_recommendation_action_status([9101, 9102, 9101], ActionStatus.PLAN_READY) == 2
    assert await repository.get_recommendation_action_status(9101) is ActionStatus.PLAN_READY
    assert await repository.list_recommendations_by_action_status(ActionStatus.PLAN_READY) == [9101, 9102]
    assert await repository.set_recommendation_action_status([9101, 9102], ActionStatus.PR_OPEN) == 2
    assert await repository.list_recommendations_by_action_status(ActionStatus.PR_OPEN) == [9101, 9102]
    assert await repository.set_plan_status(plan.plan_id, PlanStatus.APPLIED) is not None
    assert (await repository.get_plan(plan.plan_id)).status is PlanStatus.APPLIED

    event = PullRequestEvent(
        provider_id="provider-github",
        delivery_id="delivery-001",
        repo="acme/platform",
        number=42,
        action="closed",
        merged=True,
        merge_commit_sha="merge-sha",
        head_sha="head-sha",
        base_sha="base-sha",
        occurred_at=datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc),
    )
    first_delivery, created = await repository.record_webhook_delivery(event, event_type="pull_request")
    assert created
    assert first_delivery.event == event
    stale_before = datetime.now(timezone.utc) - timedelta(minutes=5)
    assert await repository.claim_webhook_delivery(
        event.provider_id,
        event.delivery_id,
        stale_before=stale_before,
    )
    assert not await repository.claim_webhook_delivery(
        event.provider_id,
        event.delivery_id,
        stale_before=stale_before,
    )

    duplicate = event.model_copy(update={"merged": False, "merge_commit_sha": None})
    existing_delivery, created = await repository.record_webhook_delivery(duplicate, event_type="pull_request")
    assert not created
    assert existing_delivery.event == event
    assert existing_delivery.id == first_delivery.id

    processed_at = datetime.now(timezone.utc)
    updated_delivery = await repository.update_webhook_delivery(
        event.provider_id,
        event.delivery_id,
        status="retryable",
        error_code="lifecycle_reconcile_failed",
        expected_statuses=("processing",),
    )
    assert updated_delivery is not None
    assert updated_delivery.status == "retryable"
    assert await repository.claim_webhook_delivery(
        event.provider_id,
        event.delivery_id,
        stale_before=stale_before,
    )
    updated_delivery = await repository.update_webhook_delivery(
        event.provider_id,
        event.delivery_id,
        status="processed",
        processed_at=processed_at,
        expected_statuses=("processing",),
    )
    assert updated_delivery is not None
    assert updated_delivery.status == "processed"
    assert updated_delivery.processed_at is not None
    assert await repository.get_webhook_delivery(event.provider_id, event.delivery_id) == updated_delivery
    assert not await repository.claim_webhook_delivery(
        event.provider_id,
        event.delivery_id,
        stale_before=stale_before,
    )
    assert await repository.update_webhook_delivery("provider-github", "missing-delivery", status="processed") is None

    heartbeat = WorkerHeartbeat(
        worker_id="worker-a",
        status="running",
        heartbeat_at=datetime.now(timezone.utc),
        lease_id="lease-a",
        lease_owner="worker-a",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        metadata={"queue": "automation", "version": 1},
    )
    saved_heartbeat = await repository.upsert_worker_heartbeat(heartbeat)
    assert await repository.get_worker_heartbeat(heartbeat.worker_id) == saved_heartbeat
    assert saved_heartbeat.metadata == heartbeat.metadata

    refreshed = WorkerHeartbeat(
        worker_id="worker-a",
        status="idle",
        heartbeat_at=datetime.now(timezone.utc),
        metadata={"queue": "automation", "version": 2},
    )
    saved_refreshed = await repository.upsert_worker_heartbeat(refreshed)
    assert saved_refreshed.status == "idle"
    assert saved_refreshed.lease_id is None
    assert saved_refreshed.metadata == {"queue": "automation", "version": 2}
    assert await repository.list_worker_heartbeats(status="idle") == [saved_refreshed]


@pytest.mark.asyncio
async def test_plan_policy_revalidation_is_persisted_with_the_plan(gitops_store):
    repository, manager, backend = gitops_store
    await repository.save_provider_config(
        GitOpsProviderConfig(
            id="provider-github", provider_type="github", secret_name="github-token", secret_key="token"
        )
    )
    await _insert_recommendations(manager, backend)
    plan = _plan(_source())
    await repository.save_plan(plan, policy_result={"version": 1, "allowed": True, "checks": []})

    rejected_result = {
        "version": 1,
        "allowed": False,
        "checks": [{"gate": "cooldown", "state": "rejected"}],
        "rejections": [{"code": "workload_cooldown_active", "detail": "retry later"}],
    }
    updated = await repository.set_plan_policy_result(
        plan.plan_id,
        rejected_result,
        expected_digest=plan.digest,
        expected_statuses=(PlanStatus.VALIDATED,),
        actor="policy-test",
    )

    assert updated is not None
    assert updated.policy_result == rejected_result
    record = await repository.get_plan_record(plan.plan_id)
    assert record is not None
    assert record.plan.policy_result == rejected_result
    assert record.policy_result == rejected_result

    await repository.set_plan_status(plan.plan_id, PlanStatus.APPLIED)
    stale = await repository.set_plan_policy_result(
        plan.plan_id,
        {"version": 1, "allowed": True},
        expected_digest=plan.digest,
        expected_statuses=(PlanStatus.VALIDATED,),
    )
    assert stale is None


@pytest.mark.asyncio
async def test_plan_pre_apply_baseline_is_json_backed_and_immutable(gitops_store):
    repository, manager, backend = gitops_store
    await _insert_recommendations(manager, backend)
    await repository.save_provider_config(
        GitOpsProviderConfig(
            id="provider-github",
            provider_type="github",
            secret_name="github-token",
            secret_key="token",
        )
    )
    plan = _plan(_source())
    await repository.save_plan(plan)

    original_baseline = PreApplyWorkloadBaseline(
        workload_uid="uid-api-1",
        generation=12,
        observed_generation=12,
        container_requests={
            "api": {"cpu_millicores": 1000, "memory_bytes": 128 * 1024 * 1024},
            "sidecar": {"cpu_millicores": 100, "memory_bytes": 64 * 1024 * 1024},
        },
    )
    retry_baseline = PreApplyWorkloadBaseline(
        workload_uid="uid-api-recreated",
        generation=3,
        observed_generation=3,
        container_requests={"api": {"cpu_millicores": 900, "memory_bytes": 128 * 1024 * 1024}},
    )

    captured = await repository.set_plan_pre_apply_baseline_if_absent(plan.plan_id, original_baseline)
    retried = await repository.set_plan_pre_apply_baseline_if_absent(plan.plan_id, retry_baseline)

    assert captured is not None
    assert captured.pre_apply_baseline == original_baseline
    assert retried == captured
    assert await repository.get_plan(plan.plan_id) == captured
    record = await repository.get_plan_record(plan.plan_id)
    assert record is not None
    assert record.plan.pre_apply_baseline == original_baseline
    assert record.policy_result == {}

    await repository.save_plan(plan, policy_result={"allowed": True})
    persisted_after_resave = await repository.get_plan(plan.plan_id)
    assert persisted_after_resave is not None
    assert persisted_after_resave.pre_apply_baseline == original_baseline


@pytest.mark.asyncio
async def test_pr_polling_lease_acquisition_is_exclusive_under_contention(gitops_store):
    repository, _, _ = gitops_store

    first, second = await asyncio.gather(
        repository.try_acquire_pr_polling_lease("worker-a", lease_seconds=30),
        repository.try_acquire_pr_polling_lease("worker-b", lease_seconds=30),
    )

    acquired = [lease for lease in (first, second) if lease is not None]
    assert len(acquired) == 1
    assert acquired[0].owner_id in {"worker-a", "worker-b"}
    assert acquired[0].fencing_token == 1


@pytest.mark.asyncio
async def test_sqlite_connection_scope_cannot_commit_another_tasks_transaction(gitops_store):
    _, manager, backend = gitops_store
    if backend != "sqlite":
        pytest.skip("SQLite uses one shared connection and requires its own serialization check.")

    async with manager.connection_scope() as connection:
        await connection.execute("CREATE TABLE connection_serialization_test (value TEXT NOT NULL)")
        await connection.commit()

    transaction_started = asyncio.Event()
    release_transaction = asyncio.Event()
    competing_write_finished = asyncio.Event()

    async def save_plan_with_files() -> None:
        async with UnitOfWork(manager) as unit_of_work:
            assert unit_of_work.connection is not None
            await unit_of_work.connection.execute(
                "INSERT INTO connection_serialization_test (value) VALUES (?)",
                ("plan",),
            )
            transaction_started.set()
            await release_transaction.wait()
            await unit_of_work.connection.execute(
                "INSERT INTO connection_serialization_test (value) VALUES (?)",
                ("plan-file",),
            )

    async def save_other_repository_row() -> None:
        await transaction_started.wait()
        async with manager.connection_scope() as connection:
            await connection.execute(
                "INSERT INTO connection_serialization_test (value) VALUES (?)",
                ("recommendation",),
            )
            await connection.commit()
        competing_write_finished.set()

    transaction_task = asyncio.create_task(save_plan_with_files())
    await transaction_started.wait()
    competing_task = asyncio.create_task(save_other_repository_row())
    await asyncio.sleep(0.05)
    assert not competing_write_finished.is_set()

    release_transaction.set()
    await asyncio.gather(transaction_task, competing_task)

    async with manager.connection_scope() as connection:
        cursor = await connection.execute("SELECT value FROM connection_serialization_test ORDER BY value")
        values = [row[0] for row in await cursor.fetchall()]
    assert values == ["plan", "plan-file", "recommendation"]


@pytest.mark.asyncio
async def test_pr_polling_lease_expiry_and_fencing_prevent_stale_mutations(gitops_store):
    repository, _, _ = gitops_store

    original = await repository.try_acquire_pr_polling_lease("worker-a", lease_seconds=1)
    assert original is not None
    assert await repository.try_acquire_pr_polling_lease("worker-b", lease_seconds=10) is None

    await asyncio.sleep(1.1)
    successor = await repository.try_acquire_pr_polling_lease("worker-b", lease_seconds=30)

    assert successor is not None
    assert successor.owner_id == "worker-b"
    assert successor.fencing_token == original.fencing_token + 1
    with pytest.raises(PullRequestPollingLeaseNotOwnedError):
        await repository.renew_pr_polling_lease(original, lease_seconds=30)
    assert not await repository.release_pr_polling_lease(original)

    await asyncio.sleep(0.01)
    renewed = await repository.renew_pr_polling_lease(successor, lease_seconds=30)
    assert renewed.fencing_token == successor.fencing_token
    assert renewed.expires_at > successor.expires_at
    assert await repository.release_pr_polling_lease(renewed)
    next_owner = await repository.try_acquire_pr_polling_lease("worker-c", lease_seconds=30)
    assert next_owner is not None
    assert next_owner.fencing_token == successor.fencing_token + 1
