"""Integration checks for the GitOps automation schema migrations."""

import json
import os
import sqlite3
from uuid import uuid4

import aiosqlite
import asyncpg
import pytest

from greenkube.core.db import DatabaseManager
from greenkube.core.migrations import MigrationRunner
from greenkube.models.repository_binding import BindingStatus


def _table_names(path: str) -> set[str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def _column_info(path: str, table: str) -> dict[str, tuple[str, str, int]]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1]: (row[2].upper(), row[4], row[3]) for row in rows}


async def _create_database_at_migration(path: str, target_version: int) -> None:
    runner = MigrationRunner("sqlite")
    async with aiosqlite.connect(path) as connection:
        await runner._ensure_migrations_table(connection)
        applied = await runner._get_applied_versions(connection)
        for version, name, sql in runner._discover_scripts():
            if version > target_version:
                break
            if version in applied:
                continue
            await runner._execute_sql(connection, sql)
            await runner._record_migration(connection, version, name)
        await connection.commit()


async def _create_postgres_schema_at_migration(connection, target_version: int) -> None:
    runner = MigrationRunner("postgres")
    await runner._ensure_migrations_table(connection)
    applied = await runner._get_applied_versions(connection)
    for version, name, sql in runner._discover_scripts():
        if version > target_version:
            break
        if version in applied:
            continue
        await runner._execute_sql(connection, sql)
        await runner._record_migration(connection, version, name)


async def _setup_sqlite_database(path: str, *, repeat: bool = False) -> None:
    manager = DatabaseManager()
    try:
        await manager.setup_sqlite(db_path=path)
        if repeat:
            await manager.setup_sqlite(db_path=path)
    finally:
        await manager.close()


def _insert_legacy_recommendations_and_prs(path: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.executemany(
            """
            INSERT INTO recommendation_history
                (id, type, description, created_at, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                (101, "rightsizing", "Legacy PR recommendation", "2026-01-01T00:00:00Z", "pr_open"),
                (102, "rightsizing", "Active recommendation", "2026-01-02T00:00:00Z", "active"),
            ),
        )
        connection.executemany(
            """
            INSERT INTO recommendation_pull_requests
                (id, recommendation_id, provider, repo, base_branch, head_branch,
                 pr_number, pr_url, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    701,
                    101,
                    "github",
                    "acme/gitops",
                    "main",
                    "greenkube/legacy-one",
                    17,
                    "https://github.com/acme/gitops/pull/17",
                    "open",
                    "2026-01-01T01:00:00Z",
                ),
                (
                    702,
                    101,
                    "github",
                    "acme/gitops",
                    "main",
                    "greenkube/legacy-two",
                    18,
                    "https://github.com/acme/gitops/pull/18",
                    "closed",
                    "2026-01-01T02:00:00Z",
                ),
            ),
        )


def _legacy_pr_rows(path: str) -> list[tuple]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            """
            SELECT id, recommendation_id, provider, repo, base_branch, head_branch,
                   pr_number, pr_url, status, created_at
            FROM recommendation_pull_requests
            ORDER BY id
            """
        ).fetchall()


def _insert_legacy_operations(path: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.executemany(
            """
            INSERT INTO automation_operations
                (id, recommendation_id, idempotency_key, fingerprint, request_json,
                 actor, status, attempts, available_at, locked_at, error, result_json,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    801,
                    101,
                    "legacy-queued",
                    "queued-fingerprint",
                    '{"base_branch":"main"}',
                    "user",
                    "queued",
                    0,
                    "2026-01-01T00:00:00Z",
                    None,
                    None,
                    None,
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
                (
                    802,
                    102,
                    "legacy-running",
                    "running-fingerprint",
                    '{"base_branch":"release"}',
                    "user",
                    "running",
                    1,
                    "2026-01-02T00:00:00Z",
                    "2026-01-02T00:00:00Z",
                    "worker interrupted",
                    None,
                    "2026-01-02T00:00:00Z",
                    "2026-01-02T00:00:00Z",
                ),
                (
                    803,
                    102,
                    "legacy-succeeded",
                    "succeeded-fingerprint",
                    '{"base_branch":"main"}',
                    "user",
                    "succeeded",
                    1,
                    "2026-01-03T00:00:00Z",
                    None,
                    None,
                    '{"pr_url":"https://github.com/acme/gitops/pull/2"}',
                    "2026-01-03T00:00:00Z",
                    "2026-01-03T00:00:00Z",
                ),
            ),
        )


def _insert_legacy_link_fixtures(path: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.executemany(
            """
            INSERT INTO recommendation_history
                (id, type, description, created_at, status, action_status)
            VALUES (?, ?, ?, ?, ?, 'pr_open')
            """,
            (
                (103, "rightsizing", "Ambiguous plan recommendation", "2026-01-03T00:00:00Z", "pr_open"),
                (104, "rightsizing", "Ambiguous provider recommendation", "2026-01-04T00:00:00Z", "pr_open"),
                (105, "rightsizing", "Unlinked closed PR recommendation", "2026-01-05T00:00:00Z", "pr_open"),
            ),
        )
        connection.executemany(
            """
            INSERT INTO recommendation_pull_requests
                (id, recommendation_id, provider, repo, base_branch, head_branch,
                 pr_number, pr_url, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    703,
                    103,
                    "github",
                    "acme/ambiguous",
                    "main",
                    "greenkube/ambiguous",
                    19,
                    "https://github.com/acme/ambiguous/pull/19",
                    "open",
                    "2026-01-03T01:00:00Z",
                ),
                (
                    704,
                    104,
                    "github",
                    "acme/shared",
                    "main",
                    "greenkube/shared",
                    20,
                    "https://github.com/acme/shared/pull/20",
                    "open",
                    "2026-01-04T01:00:00Z",
                ),
                (
                    705,
                    105,
                    "github",
                    "acme/closed",
                    "main",
                    "greenkube/closed",
                    21,
                    "https://github.com/acme/closed/pull/21",
                    "closed",
                    "2026-01-05T01:00:00Z",
                ),
            ),
        )
        connection.executemany(
            "INSERT INTO gitops_providers (id, provider_type) VALUES (?, ?)",
            (("github-main", "github"), ("github-secondary", "github")),
        )
        connection.executemany(
            """
            INSERT INTO workload_sources
                (id, provider_id, repo_url, ref, path, controller_kind,
                 controller_name, controller_namespace, renderer)
            VALUES (?, ?, ?, 'main', 'apps', 'argocd', 'app', 'default', 'raw')
            """,
            (
                ("source-unique", "github-main", "https://github.com/acme/gitops.git"),
                ("source-ambiguous", "github-main", "https://github.com/acme/ambiguous.git"),
                ("source-shared-main", "github-main", "https://github.com/acme/shared.git"),
                ("source-shared-secondary", "github-secondary", "https://github.com/acme/shared.git"),
            ),
        )
        connection.executemany(
            """
            INSERT INTO automation_plans (plan_id, source_id, provider_id, status)
            VALUES (?, ?, ?, 'pr_open')
            """,
            (
                ("plan-unique", "source-unique", "github-main"),
                ("plan-ambiguous-a", "source-ambiguous", "github-main"),
                ("plan-ambiguous-b", "source-ambiguous", "github-main"),
            ),
        )
        connection.executemany(
            """
            INSERT INTO automation_plan_recommendations (plan_id, recommendation_id)
            VALUES (?, ?)
            """,
            (
                ("plan-unique", 101),
                ("plan-ambiguous-a", 103),
                ("plan-ambiguous-b", 103),
            ),
        )


def _insert_legacy_repository_bindings(path: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.executemany(
            """
            INSERT INTO repository_bindings
                (id, cluster, namespace, workload_kind, workload_name, repo_url,
                 path, branch, source, priority, status, confidence, evidence,
                 discovered_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    901,
                    "cluster-a",
                    "production",
                    "Deployment",
                    "api",
                    "https://github.com/acme/platform",
                    "apps/api",
                    "main",
                    "argocd",
                    100,
                    "resolved",
                    0.95,
                    '{"source":"application"}',
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
                (
                    902,
                    "cluster-a",
                    "production",
                    "Deployment",
                    "worker",
                    "https://github.com/acme/platform",
                    "apps/worker",
                    "main",
                    "flux",
                    80,
                    "ambiguous",
                    0.5,
                    '{"candidate_count":2}',
                    "2026-01-02T00:00:00Z",
                    "2026-01-02T00:00:00Z",
                ),
                (
                    903,
                    "cluster-a",
                    "production",
                    "Deployment",
                    "frontend",
                    "https://github.com/acme/web",
                    "apps/frontend",
                    "main",
                    "annotation",
                    10,
                    "unresolved",
                    0.0,
                    "{}",
                    "2026-01-03T00:00:00Z",
                    "2026-01-03T00:00:00Z",
                ),
            ),
        )


def _repository_binding_rows(path: str) -> list[tuple]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            """
            SELECT id, cluster, namespace, workload_kind, workload_name, repo_url,
                   path, branch, source, priority, status, confidence, evidence,
                   discovered_at, updated_at
            FROM repository_bindings
            ORDER BY id
            """
        ).fetchall()


@pytest.mark.asyncio
async def test_gitops_migrations_create_schema_on_empty_database(tmp_path):
    db_file = str(tmp_path / "gitops-empty.db")
    await _setup_sqlite_database(db_file)

    expected_tables = {
        "gitops_providers",
        "gitops_provider_allowlists",
        "workload_sources",
        "workload_source_bindings",
        "workload_binding_candidates",
        "workload_binding_events",
        "automation_plans",
        "automation_plan_files",
        "automation_plan_recommendations",
        "automation_plan_operations",
        "automation_state_migration_audit",
        "automation_branch_cleanup_completed",
        "webhook_deliveries",
        "automation_worker_heartbeats",
        "gitops_pr_polling_lease",
        "container_metrics",
        "container_metrics_hourly",
    }
    assert expected_tables <= _table_names(db_file)
    assert _column_info(db_file, "gitops_pr_polling_lease")["fencing_token"][0] == "INTEGER"

    expected_columns = {
        "gitops_providers": {
            "id",
            "provider_type",
            "api_base_url",
            "secret_namespace",
            "secret_name",
            "secret_key",
            "capabilities",
            "created_at",
            "updated_at",
        },
        "gitops_provider_allowlists": {"provider_id", "repository_pattern", "created_at", "updated_at"},
        "workload_sources": {
            "id",
            "provider_id",
            "repo_url",
            "ref",
            "revision",
            "path",
            "controller_kind",
            "controller_name",
            "controller_namespace",
            "controller_uid",
            "controller_generation",
            "source_uid",
            "source_generation",
            "source_revision",
            "renderer",
            "renderer_config",
            "observed_at",
            "expires_at",
            "created_at",
            "updated_at",
        },
        "workload_source_bindings": {
            "cluster",
            "namespace",
            "workload_kind",
            "workload_name",
            "source_id",
            "status",
            "confidence",
            "evidence",
            "reason",
            "expires_at",
            "created_at",
            "updated_at",
        },
        "workload_binding_candidates": {
            "binding_id",
            "source_id",
            "status",
            "confidence",
            "evidence",
            "expires_at",
            "created_at",
            "updated_at",
        },
        "workload_binding_events": {
            "id",
            "event_key",
            "cluster",
            "namespace",
            "workload_kind",
            "workload_name",
            "event_type",
            "actor",
            "source_id",
            "source_generation",
            "expires_at",
            "details",
            "created_at",
        },
        "automation_plans": {
            "plan_id",
            "source_id",
            "provider_id",
            "source_snapshot",
            "action_json",
            "rollback_of_plan_id",
            "base_revision",
            "source_revision",
            "renderer",
            "diff",
            "rendered_diff",
            "digest",
            "validation",
            "warnings",
            "policy_result",
            "status",
            "expires_at",
            "created_at",
            "updated_at",
        },
        "automation_plan_files": {
            "plan_id",
            "path",
            "operation",
            "original_sha",
            "content",
            "created_at",
            "updated_at",
        },
        "automation_plan_recommendations": {
            "plan_id",
            "recommendation_id",
            "created_at",
            "updated_at",
        },
        "automation_plan_operations": {
            "operation_id",
            "plan_id",
            "plan_digest",
            "idempotency_key",
            "status",
            "attempts",
            "max_attempts",
            "backoff_base_seconds",
            "backoff_max_seconds",
            "available_at",
            "locked_at",
            "worker_id",
            "lease_id",
            "lease_expires_at",
            "last_error",
            "provider_id",
            "commit_sha",
            "pull_request_number",
            "created_at",
            "updated_at",
        },
        "recommendation_pull_requests": {
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
        },
        "automation_state_migration_audit": {
            "migration_version",
            "entity_type",
            "entity_id",
            "recommendation_id",
            "disposition",
            "reason_code",
            "reason",
            "previous_status",
            "resulting_status",
            "previous_error",
            "previous_updated_at",
            "previous_plan_id",
            "previous_provider_id",
            "legacy_provider",
            "legacy_repo",
            "resolved_plan_id",
            "resolved_provider_id",
            "created_at",
        },
        "container_metrics": {
            "pod_name",
            "namespace",
            "container_name",
            "memory_usage_bytes",
            "timestamp",
        },
        "container_metrics_hourly": {
            "pod_name",
            "namespace",
            "container_name",
            "sample_count",
            "hour_bucket",
        },
        "webhook_deliveries": {
            "provider_id",
            "delivery_id",
            "event_type",
            "repo",
            "pr_number",
            "action",
            "merged",
            "merge_commit_sha",
            "head_sha",
            "base_sha",
            "occurred_at",
            "status",
            "received_at",
            "processed_at",
            "created_at",
            "updated_at",
        },
        "automation_worker_heartbeats": {
            "worker_id",
            "status",
            "heartbeat_at",
            "lease_id",
            "lease_owner",
            "lease_expires_at",
            "lease_operation_id",
            "lease_plan_operation_id",
            "metadata",
            "created_at",
            "updated_at",
        },
        "gitops_pr_polling_lease": {
            "id",
            "owner_id",
            "fencing_token",
            "expires_at",
            "created_at",
            "updated_at",
        },
        "recommendation_history": {"status", "action_status"},
    }
    for table, columns in expected_columns.items():
        actual = set(_column_info(db_file, table))
        assert columns <= actual, f"{table} is missing columns: {columns - actual}"

    json_columns = {
        ("gitops_providers", "capabilities"),
        ("workload_sources", "renderer_config"),
        ("workload_source_bindings", "evidence"),
        ("workload_binding_candidates", "evidence"),
        ("workload_binding_events", "details"),
        ("automation_plans", "source_snapshot"),
        ("automation_plans", "action_json"),
        ("automation_plans", "validation"),
        ("automation_plans", "warnings"),
        ("automation_plans", "policy_result"),
        ("automation_worker_heartbeats", "metadata"),
    }
    for table, column in json_columns:
        assert _column_info(db_file, table)[column][0] == "TEXT"

    provider_columns = set(_column_info(db_file, "gitops_providers"))
    assert not {"token", "token_value", "credential", "credential_value"} & provider_columns

    with sqlite3.connect(db_file) as connection:
        migration_names = dict(
            connection.execute("SELECT version, name FROM schema_migrations WHERE version BETWEEN 22 AND 34").fetchall()
        )
    assert migration_names == {
        22: "gitops_providers",
        23: "workload_sources",
        24: "automation_plans",
        25: "pr_webhook_state",
        26: "automation_worker_health",
        27: "action_status",
        28: "plan_operations",
        29: "pr_poll_queue",
        30: "rollback_plan_links",
        31: "pr_polling_singleton_lease",
        32: "manual_binding_resolution",
        33: "container_metrics",
        34: "legacy_automation_state",
    }


@pytest.mark.asyncio
async def test_gitops_migrations_preserve_legacy_recommendation_and_pr_history(tmp_path):
    db_file = str(tmp_path / "gitops-existing.db")
    await _create_database_at_migration(db_file, target_version=21)
    _insert_legacy_recommendations_and_prs(db_file)
    _insert_legacy_operations(db_file)

    assert "action_status" not in _column_info(db_file, "recommendation_history")
    original_pr_rows = _legacy_pr_rows(db_file)
    assert len(original_pr_rows) == 2

    await _setup_sqlite_database(db_file, repeat=True)

    with sqlite3.connect(db_file) as connection:
        recommendations = dict(
            connection.execute(
                "SELECT id, status || ':' || action_status FROM recommendation_history ORDER BY id"
            ).fetchall()
        )
        migration_versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations").fetchall()}
        operation_rows = connection.execute(
            """
            SELECT id, recommendation_id, idempotency_key, request_json, status, error, result_json
            FROM automation_operations ORDER BY id
            """
        ).fetchall()
        pr_audit = connection.execute(
            """
            SELECT entity_id, disposition, reason_code, previous_status, resulting_status
            FROM automation_state_migration_audit
            WHERE migration_version = 34 AND entity_type = 'pull_request'
            ORDER BY entity_id
            """
        ).fetchall()
        operation_audit = connection.execute(
            """
            SELECT entity_id, disposition, reason_code, previous_status, resulting_status,
                   previous_updated_at
            FROM automation_state_migration_audit
            WHERE migration_version = 34 AND entity_type = 'operation'
            ORDER BY entity_id
            """
        ).fetchall()

    assert recommendations == {101: "active:failed", 102: "active:none"}
    assert set(range(22, 36)) <= migration_versions
    migrated_pr_rows = _legacy_pr_rows(db_file)
    assert migrated_pr_rows[0][:8] == original_pr_rows[0][:8]
    assert migrated_pr_rows[0][8] == "error"
    assert migrated_pr_rows[0][9] == original_pr_rows[0][9]
    assert migrated_pr_rows[1] == original_pr_rows[1]
    assert operation_rows == [
        (
            801,
            101,
            "legacy-queued",
            '{"base_branch":"main"}',
            "failed",
            "Legacy operation quarantined by migration 0034, see automation_state_migration_audit.",
            None,
        ),
        (
            802,
            102,
            "legacy-running",
            '{"base_branch":"release"}',
            "failed",
            "worker interrupted | legacy operation quarantined by migration 0034, "
            "see automation_state_migration_audit.",
            None,
        ),
        (
            803,
            102,
            "legacy-succeeded",
            '{"base_branch":"main"}',
            "succeeded",
            None,
            '{"pr_url":"https://github.com/acme/gitops/pull/2"}',
        ),
    ]
    assert pr_audit == [
        ("701", "quarantined", "provider_not_safely_inferred", "open", "error"),
        ("702", "quarantined", "provider_not_safely_inferred", "closed", "closed"),
    ]
    assert operation_audit == [
        (
            "801",
            "quarantined",
            "legacy_operation_requires_replanning",
            "queued",
            "failed",
            "2026-01-01T00:00:00Z",
        ),
        (
            "802",
            "quarantined",
            "legacy_operation_requires_replanning",
            "running",
            "failed",
            "2026-01-02T00:00:00Z",
        ),
        (
            "803",
            "archived",
            "legacy_terminal_operation_retained",
            "succeeded",
            "succeeded",
            "2026-01-03T00:00:00Z",
        ),
    ]
    assert _table_names(db_file) >= {
        "gitops_providers",
        "workload_sources",
        "automation_plans",
        "webhook_deliveries",
        "automation_worker_heartbeats",
        "automation_state_migration_audit",
    }


@pytest.mark.asyncio
async def test_legacy_automation_state_backfill_and_quarantine_are_idempotent(tmp_path):
    db_file = str(tmp_path / "legacy-automation-state.db")
    await _create_database_at_migration(db_file, target_version=21)
    _insert_legacy_recommendations_and_prs(db_file)
    _insert_legacy_operations(db_file)
    await _create_database_at_migration(db_file, target_version=33)
    _insert_legacy_link_fixtures(db_file)

    runner = MigrationRunner("sqlite")
    migration_sql = next(sql for version, _, sql in runner._discover_scripts() if version == 34)
    async with aiosqlite.connect(db_file) as connection:
        await runner._execute_sql(connection, migration_sql)
        await runner._execute_sql(connection, migration_sql)
        await runner._record_migration(connection, 34, "legacy_automation_state")
        await connection.commit()
        assert await runner.run(connection) == 3
        assert await runner.run(connection) == 0

    with sqlite3.connect(db_file) as connection:
        pull_requests = {
            row[0]: row[1:]
            for row in connection.execute(
                """
                SELECT id, plan_id, provider_id, status, repo, pr_number, pr_url, error
                FROM recommendation_pull_requests ORDER BY id
                """
            )
        }
        recommendations = dict(
            connection.execute(
                "SELECT id, action_status FROM recommendation_history WHERE id BETWEEN 101 AND 105"
            ).fetchall()
        )
        audit_rows = connection.execute(
            """
            SELECT entity_type, entity_id, disposition, reason_code, previous_status,
                   resulting_status, previous_error, resolved_plan_id, resolved_provider_id
            FROM automation_state_migration_audit
            WHERE migration_version = 34
            ORDER BY entity_type, entity_id
            """
        ).fetchall()
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM automation_state_migration_audit WHERE migration_version = 34"
        ).fetchone()[0]

    assert pull_requests[701][:3] == ("plan-unique", "github-main", "open")
    assert pull_requests[701][3:6] == ("acme/gitops", 17, "https://github.com/acme/gitops/pull/17")
    assert pull_requests[702][:3] == (None, "github-main", "closed")
    assert pull_requests[703][:3] == (None, "github-main", "error")
    assert pull_requests[704][:3] == (None, None, "error")
    assert pull_requests[705][:3] == (None, None, "closed")
    assert "ambiguous_plan" in pull_requests[703][6]
    assert "ambiguous_provider" in pull_requests[704][6]
    assert recommendations == {101: "pr_open", 102: "none", 103: "pr_open", 104: "failed", 105: "failed"}

    audit_by_entity = {(row[0], row[1]): row for row in audit_rows}
    assert audit_by_entity[("pull_request", "701")][2:6] == (
        "backfilled",
        "unique_plan_provider_match",
        "open",
        "open",
    )
    assert audit_by_entity[("pull_request", "701")][7:] == ("plan-unique", "github-main")
    assert audit_by_entity[("pull_request", "702")][2:6] == (
        "quarantined",
        "plan_not_safely_inferred",
        "closed",
        "closed",
    )
    assert audit_by_entity[("pull_request", "703")][2:6] == (
        "quarantined",
        "ambiguous_plan",
        "open",
        "error",
    )
    assert audit_by_entity[("pull_request", "704")][2:6] == (
        "quarantined",
        "ambiguous_provider",
        "open",
        "error",
    )
    assert audit_by_entity[("pull_request", "705")][2:6] == (
        "quarantined",
        "provider_not_safely_inferred",
        "closed",
        "closed",
    )
    assert audit_count == len(audit_rows) == 10


@pytest.mark.asyncio
async def test_repository_binding_status_constraint_expands_without_losing_rows(tmp_path):
    db_file = str(tmp_path / "binding-statuses.db")
    await _create_database_at_migration(db_file, target_version=21)
    _insert_legacy_repository_bindings(db_file)
    original_rows = _repository_binding_rows(db_file)

    await _setup_sqlite_database(db_file)

    assert _repository_binding_rows(db_file) == original_rows
    with sqlite3.connect(db_file) as connection:
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(repository_bindings)").fetchall()}
        connection.executemany(
            """
            INSERT INTO repository_bindings
                (cluster, namespace, workload_kind, workload_name, repo_url,
                 source, priority, status, confidence, evidence,
                 discovered_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    "cluster-a",
                    "production",
                    "Deployment",
                    "batch",
                    "https://github.com/acme/platform",
                    "argocd",
                    100,
                    "blocked",
                    0.2,
                    "{}",
                    "2026-02-01T00:00:00Z",
                    "2026-02-01T00:00:00Z",
                ),
                (
                    "cluster-a",
                    "production",
                    "Deployment",
                    "cron",
                    "https://github.com/acme/platform",
                    "flux",
                    80,
                    "stale",
                    0.4,
                    "{}",
                    "2026-02-02T00:00:00Z",
                    "2026-02-02T00:00:00Z",
                ),
            ),
        )
        accepted_statuses = {row[0] for row in connection.execute("SELECT DISTINCT status FROM repository_bindings")}

    assert accepted_statuses == {status.value for status in BindingStatus}
    assert "idx_repository_bindings_workload" in indexes


@pytest.mark.asyncio
async def test_recommendation_identity_v3_migration_scopes_same_pod_to_its_owner(tmp_path):
    db_file = str(tmp_path / "recommendation-owner-identity.db")
    await _create_database_at_migration(db_file, target_version=35)
    with sqlite3.connect(db_file) as connection:
        connection.executemany(
            """
            INSERT INTO recommendation_history (
                id, type, description, created_at, status, scope, namespace, pod_name,
                container_name, target_node, owner_kind, owner_name, identity_version, fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    9301,
                    "RIGHTSIZING_CPU",
                    "Deployment request",
                    "2026-10-20T00:00:00Z",
                    "active",
                    "workload",
                    "production",
                    "api",
                    "web",
                    None,
                    "Deployment",
                    "api",
                    2,
                    "v2:workload:production:api:web::RIGHTSIZING_CPU",
                ),
                (
                    9302,
                    "RIGHTSIZING_CPU",
                    "StatefulSet request",
                    "2026-10-20T00:00:00Z",
                    "active",
                    "workload",
                    "production",
                    "api",
                    "web",
                    None,
                    "StatefulSet",
                    "api",
                    2,
                    None,
                ),
            ),
        )

    await _setup_sqlite_database(db_file)

    with sqlite3.connect(db_file) as connection:
        rows = connection.execute(
            """
            SELECT id, identity_version, fingerprint, owner_kind, status
            FROM recommendation_history
            WHERE id IN (9301, 9302)
            ORDER BY id
            """
        ).fetchall()
    assert rows == [
        (9301, 3, "v3:workload:production:api:web::Deployment:api:RIGHTSIZING_CPU", "Deployment", "active"),
        (9302, 3, "v3:workload:production:api:web::StatefulSet:api:RIGHTSIZING_CPU", "StatefulSet", "active"),
    ]


@pytest.mark.asyncio
async def test_postgres_recommendation_identity_v3_migration_scopes_same_pod_to_its_owner():
    dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL recommendation identity migration tests.")

    schema = f"gk_reco_identity_v3_{uuid4().hex}"
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET search_path TO "{schema}"')
        await _create_postgres_schema_at_migration(connection, target_version=35)
        await connection.executemany(
            """
            INSERT INTO recommendation_history (
                id, type, description, created_at, status, scope, namespace, pod_name,
                container_name, target_node, owner_kind, owner_name, identity_version, fingerprint
            ) VALUES (
                $1, $2, $3, NOW(), $4, $5, $6, $7, $8, NULL, $9, $10, 2, $11
            )
            """,
            [
                (
                    9301,
                    "RIGHTSIZING_CPU",
                    "Deployment request",
                    "active",
                    "workload",
                    "production",
                    "api",
                    "web",
                    "Deployment",
                    "api",
                    "v2:workload:production:api:web::RIGHTSIZING_CPU",
                ),
                (
                    9302,
                    "RIGHTSIZING_CPU",
                    "StatefulSet request",
                    "active",
                    "workload",
                    "production",
                    "api",
                    "web",
                    "StatefulSet",
                    "api",
                    None,
                ),
            ],
        )

        await MigrationRunner("postgres").run(connection)

        rows = await connection.fetch(
            """
            SELECT id, identity_version, fingerprint, owner_kind, status
            FROM recommendation_history
            WHERE id IN (9301, 9302)
            ORDER BY id
            """
        )
        assert [tuple(row.values()) for row in rows] == [
            (9301, 3, "v3:workload:production:api:web::Deployment:api:RIGHTSIZING_CPU", "Deployment", "active"),
            (9302, 3, "v3:workload:production:api:web::StatefulSet:api:RIGHTSIZING_CPU", "StatefulSet", "active"),
        ]
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()


@pytest.mark.asyncio
async def test_postgres_gitops_migrations_apply_to_empty_and_existing_schemas():
    dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL migration integration tests.")

    empty_schema = f"gk_gitops_empty_{uuid4().hex}"
    existing_schema = f"gk_gitops_existing_{uuid4().hex}"
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f'CREATE SCHEMA "{empty_schema}"')
        await connection.execute(f'CREATE SCHEMA "{existing_schema}"')

        await connection.execute(f'SET search_path TO "{empty_schema}"')
        runner = MigrationRunner("postgres")
        await runner.run(connection)
        json_type = await connection.fetchval(
            """
            SELECT data_type FROM information_schema.columns
            WHERE table_schema = $1 AND table_name = 'gitops_providers'
              AND column_name = 'capabilities'
            """,
            empty_schema,
        )
        assert json_type == "jsonb"
        rollback_link_column = await connection.fetchval(
            """
            SELECT data_type FROM information_schema.columns
            WHERE table_schema = $1 AND table_name = 'automation_plans'
              AND column_name = 'rollback_of_plan_id'
            """,
            empty_schema,
        )
        assert rollback_link_column == "text"
        for table_name in (
            "automation_state_migration_audit",
            "container_metrics",
            "container_metrics_hourly",
        ):
            assert await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = $1 AND table_name = $2
                )
                """,
                empty_schema,
                table_name,
            )
        await connection.execute(
            """
            INSERT INTO repository_bindings
                (cluster, namespace, workload_kind, workload_name, repo_url,
                 source, priority, status, confidence, evidence, discovered_at, updated_at)
            VALUES
                ('cluster-a', 'production', 'Deployment', 'blocked', 'https://example.test/repo',
                 'argocd', 100, 'blocked', 0.2, '{}'::jsonb, NOW(), NOW()),
                ('cluster-a', 'production', 'Deployment', 'stale', 'https://example.test/repo',
                 'flux', 80, 'stale', 0.4, '{}'::jsonb, NOW(), NOW())
            """
        )
        accepted_statuses = {row["status"] for row in await connection.fetch("SELECT status FROM repository_bindings")}
        assert accepted_statuses == {"blocked", "stale"}

        await connection.execute(f'SET search_path TO "{existing_schema}"')
        await _create_postgres_schema_at_migration(connection, target_version=21)
        await connection.execute(
            """
            INSERT INTO recommendation_history (id, type, description, created_at, status)
            VALUES (101, 'rightsizing', 'Legacy PR recommendation', NOW(), 'pr_open'),
                   (102, 'rightsizing', 'Active recommendation', NOW(), 'active')
            """
        )
        await connection.execute(
            """
            INSERT INTO recommendation_pull_requests
                (id, recommendation_id, provider, repo, pr_number, pr_url, status)
            VALUES (701, 101, 'github', 'acme/gitops', 17,
                    'https://github.com/acme/gitops/pull/17', 'open'),
                   (702, 101, 'github', 'acme/gitops', 18,
                    'https://github.com/acme/gitops/pull/18', 'closed')
            """
        )
        await connection.execute(
            """
            INSERT INTO automation_operations
                (id, recommendation_id, idempotency_key, fingerprint, request_json,
                 actor, status, attempts, available_at, created_at, updated_at)
            VALUES (801, 101, 'legacy-queued', 'queued-fingerprint',
                    '{"base_branch":"main"}'::jsonb, 'user', 'queued', 0, NOW(), NOW(), NOW())
            """
        )
        await connection.execute(
            """
            INSERT INTO repository_bindings
                (id, cluster, namespace, workload_kind, workload_name, repo_url,
                 source, priority, status, confidence, evidence, discovered_at, updated_at)
            VALUES (901, 'cluster-a', 'production', 'Deployment', 'api',
                    'https://example.test/repo', 'argocd', 100, 'resolved',
                    0.95, '{}'::jsonb, NOW(), NOW())
            """
        )
        await _create_postgres_schema_at_migration(connection, target_version=33)
        await connection.execute(
            """
            INSERT INTO gitops_providers (id, provider_type, api_base_url)
            VALUES ('github-main', 'github', 'https://api.github.com')
            """
        )
        await connection.execute(
            """
            INSERT INTO workload_sources
                    (id, provider_id, repo_url, ref, path, controller_kind,
                     controller_name, controller_namespace, renderer)
            VALUES ('source-unique', 'github-main', 'https://github.com/acme/gitops.git',
                        'main', 'apps', 'argocd', 'app', 'default', 'raw')
            """
        )
        await connection.execute(
            """
            INSERT INTO automation_plans (plan_id, source_id, provider_id, status)
            VALUES ('plan-unique', 'source-unique', 'github-main', 'pr_open')
            """
        )
        await connection.execute(
            """
            INSERT INTO automation_plan_recommendations (plan_id, recommendation_id)
            VALUES ('plan-unique', 101)
            """
        )
        original_pr_rows = await connection.fetch(
            """
            SELECT id, recommendation_id, provider, repo, pr_number, pr_url, status
            FROM recommendation_pull_requests ORDER BY id
            """
        )
        original_binding = await connection.fetchrow(
            """
            SELECT id, cluster, namespace, workload_kind, workload_name, repo_url,
                   source, priority, status, confidence, evidence, discovered_at, updated_at
            FROM repository_bindings WHERE id = 901
            """
        )

        migration_sql = next(sql for version, _, sql in runner._discover_scripts() if version == 34)
        await runner._execute_sql(connection, migration_sql)
        await runner._execute_sql(connection, migration_sql)
        await runner.run(connection)
        lease_expiry_type = await connection.fetchval(
            """
            SELECT data_type FROM information_schema.columns
            WHERE table_schema = $1 AND table_name = 'gitops_pr_polling_lease'
              AND column_name = 'expires_at'
            """,
            empty_schema,
        )
        assert lease_expiry_type == "timestamp with time zone"
        lease_fencing_type = await connection.fetchval(
            """
            SELECT data_type FROM information_schema.columns
            WHERE table_schema = $1 AND table_name = 'gitops_pr_polling_lease'
              AND column_name = 'fencing_token'
            """,
            empty_schema,
        )
        assert lease_fencing_type == "bigint"
        assert await runner.run(connection) == 0
        migrated_recommendations = await connection.fetch(
            "SELECT id, status, action_status FROM recommendation_history ORDER BY id"
        )
        assert [(row["id"], row["status"], row["action_status"]) for row in migrated_recommendations] == [
            (101, "active", "pr_open"),
            (102, "active", "none"),
        ]
        migrated_pr_rows = await connection.fetch(
            """
            SELECT id, recommendation_id, provider, repo, pr_number, pr_url, status
            FROM recommendation_pull_requests ORDER BY id
            """
        )
        assert migrated_pr_rows == original_pr_rows
        migrated_pr_links = await connection.fetch(
            "SELECT id, plan_id, provider_id, status FROM recommendation_pull_requests ORDER BY id"
        )
        assert [(row["id"], row["plan_id"], row["provider_id"], row["status"]) for row in migrated_pr_links] == [
            (701, "plan-unique", "github-main", "open"),
            (702, None, "github-main", "closed"),
        ]
        legacy_operation = await connection.fetchrow(
            "SELECT status, request_json, error FROM automation_operations WHERE id = 801"
        )
        assert legacy_operation["status"] == "failed"
        request_json = legacy_operation["request_json"]
        if isinstance(request_json, str):
            request_json = json.loads(request_json)
        assert request_json == {"base_branch": "main"}
        assert "migration 0034" in legacy_operation["error"]
        migration_audit = await connection.fetch(
            """
            SELECT entity_type, entity_id, disposition, reason_code, previous_status, resulting_status
            FROM automation_state_migration_audit
            WHERE migration_version = 34
            ORDER BY entity_type, entity_id
            """
        )
        assert [
            (row["entity_type"], row["entity_id"], row["disposition"], row["reason_code"]) for row in migration_audit
        ] == [
            ("operation", "801", "quarantined", "legacy_operation_requires_replanning"),
            ("pull_request", "701", "backfilled", "unique_plan_provider_match"),
            ("pull_request", "702", "quarantined", "plan_not_safely_inferred"),
        ]
        migrated_binding = await connection.fetchrow(
            """
            SELECT id, cluster, namespace, workload_kind, workload_name, repo_url,
                   source, priority, status, confidence, evidence, discovered_at, updated_at
            FROM repository_bindings WHERE id = 901
            """
        )
        assert dict(migrated_binding) == dict(original_binding)

        await connection.execute(
            """
            INSERT INTO repository_bindings
                (cluster, namespace, workload_kind, workload_name, repo_url,
                 source, priority, status, confidence, evidence, discovered_at, updated_at)
            VALUES ('cluster-a', 'production', 'Deployment', 'stale',
                    'https://example.test/repo', 'flux', 80, 'stale',
                    0.4, '{}'::jsonb, NOW(), NOW())
            """
        )
        assert await connection.fetchval("SELECT COUNT(*) FROM repository_bindings WHERE status = 'stale'") == 1
    finally:
        try:
            await connection.execute("SET search_path TO public")
            await connection.execute(f'DROP SCHEMA IF EXISTS "{empty_schema}" CASCADE')
            await connection.execute(f'DROP SCHEMA IF EXISTS "{existing_schema}" CASCADE')
        finally:
            await connection.close()
