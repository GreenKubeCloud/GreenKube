"""Typed actions without a legacy patch view survive persistence (H6, e2e wave 4).

The typed action of a recommendation is stored inside the ``patch`` column.
A ``delete_resource`` action has no legacy patch, so it used to be dropped on
write: every orphaned LoadBalancer/PersistentVolume recommendation then reported
``action_unavailable`` and could never open its deletion pull request.
"""

from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest

from greenkube.automation.actions import ACTION_KINDS, DELETE_RESOURCE
from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.eligibility import validate_deletion_action
from greenkube.models.metrics import Recommendation, RecommendationRecord, RecommendationType
from greenkube.storage.postgres.recommendation_repository import PostgresRecommendationRepository
from greenkube.storage.recommendation_mapper import record_values
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def db(request, tmp_path, monkeypatch):
    backend = request.param
    schema = None
    dsn = None
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL storage tests.")
        schema = f"gk_actions_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "actions.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield manager
    finally:
        await manager.close()
        if backend == "postgres" and dsn and schema:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


def _repo(manager: DatabaseManager):
    if manager.db_type == "postgres":
        return PostgresRecommendationRepository(manager)
    return SQLiteRecommendationRepository(manager)


def _orphaned_lb(**overrides) -> Recommendation:
    values = dict(
        pod_name="old-lb",
        namespace="shop",
        type=RecommendationType.ORPHANED_LOAD_BALANCER,
        scope="cluster",
        description="LoadBalancer Service 'old-lb' appears unused",
        reason="no endpoints",
    )
    values.update(overrides)
    return Recommendation(**values)


def test_a_deletion_action_is_written_inside_an_operationless_patch() -> None:
    recommendation = _orphaned_lb(id=7)
    recommendation.action = ACTION_KINDS.build_action_plans([recommendation])[7]
    record = RecommendationRecord.from_recommendation(recommendation)
    assert record.patch is None

    stored = record_values(record)["patch"]

    assert stored is not None
    assert '"delete_resource"' in stored
    assert '"operations": []' in stored


def test_a_record_without_action_or_patch_stores_no_patch() -> None:
    assert record_values(RecommendationRecord.from_recommendation(_orphaned_lb()))["patch"] is None


async def test_an_orphaned_load_balancer_keeps_its_delete_resource_action(db) -> None:
    repo = _repo(db)
    recommendation = _orphaned_lb()
    await repo.upsert_recommendations([RecommendationRecord.from_recommendation(recommendation)])
    [persisted] = await repo.get_active_recommendations(namespace="shop")
    assert persisted.id is not None

    # The scan attaches the action once persistence assigned the id, then writes it again.
    recommendation.id = persisted.id
    recommendation.action = ACTION_KINDS.build_action_plans([recommendation])[persisted.id]
    await repo.upsert_recommendations([RecommendationRecord.from_recommendation(recommendation)])

    record = await repo.get_recommendation_by_id(persisted.id)
    assert record is not None
    assert record.action is not None
    assert ACTION_KINDS.spec_for_action(record.action) is DELETE_RESOURCE
    assert validate_deletion_action(record) == ([persisted.id], None)
