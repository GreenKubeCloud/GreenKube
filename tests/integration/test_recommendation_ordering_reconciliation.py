"""Default active ordering and per-source reconciliation on SQLite and PostgreSQL.

Regression tests for BUG-20 (E2): active lists were sorted ``priority DESC`` on
a text column (medium, low, high) instead of by ranking and savings. And for
BUG-22 (E8): reconciliation must never stale a recommendation supported by a
source that did not complete. PostgreSQL cases run when
``GREENKUBE_TEST_POSTGRES_DSN`` is set.
"""

import os
from dataclasses import dataclass
from typing import AsyncGenerator, Optional
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.models.metrics import (
    RecommendationRecord,
    RecommendationStatus,
    RecommendationType,
)
from greenkube.storage.postgres.recommendation_repository import PostgresRecommendationRepository
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository


@dataclass
class Backend:
    name: str
    managers: list
    schema: Optional[str] = None

    def recommendations(self):
        if self.name == "postgres":
            return PostgresRecommendationRepository(self.managers[0])
        return SQLiteRecommendationRepository(self.managers[0])


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def backend(request, tmp_path, monkeypatch) -> AsyncGenerator[Backend, None]:
    name = request.param
    schema = None
    dsn = None
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    if name == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL recommendation contract tests.")
        schema = f"gk_reco_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "reco.sqlite"))
        monkeypatch.setenv("DB_SCHEMA", "public")
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield Backend(name=name, managers=[manager], schema=schema)
    finally:
        await manager.close()
        if name == "postgres" and dsn and schema:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


# --------------------------------------------------------------------------- #
# Default ordering (E2, BUG-20)                                                #
# --------------------------------------------------------------------------- #


def _record(name, *, priority="medium", ranking=None, cost=None, source="greenkube", sources=None, namespace="prod"):
    return RecommendationRecord(
        pod_name=name,
        namespace=namespace,
        type=RecommendationType.RIGHTSIZING_CPU,
        description=f"Reduce {name}",
        priority=priority,
        ranking_score=ranking,
        potential_savings_cost=cost,
        source=source,
        sources=sources if sources is not None else [source],
        status=RecommendationStatus.ACTIVE,
    )


async def test_active_list_orders_by_ranking_then_cost_nulls_last_then_id(backend):
    repo = backend.recommendations()
    # Inserted in an order where TEXT priority ordering (BUG-20) would put
    # medium before low before high.
    records = [
        _record("medium-unranked-cheap", priority="medium", ranking=None, cost=1.0),
        _record("low-top-ranked", priority="low", ranking=0.9, cost=5.0),
        _record("high-mid-ranked", priority="high", ranking=0.5, cost=50.0),
        _record("medium-mid-ranked-pricier", priority="medium", ranking=0.5, cost=80.0),
        _record("high-unranked-no-cost", priority="high", ranking=None, cost=None),
        _record("low-unranked-pricey", priority="low", ranking=None, cost=40.0),
        _record("medium-mid-ranked-no-cost", priority="medium", ranking=0.5, cost=None),
    ]
    await repo.upsert_recommendations(records)

    active = await repo.get_active_recommendations(namespace="prod")

    assert [record.pod_name for record in active] == [
        "low-top-ranked",
        "medium-mid-ranked-pricier",
        "high-mid-ranked",
        "medium-mid-ranked-no-cost",
        "low-unranked-pricey",
        "medium-unranked-cheap",
        "high-unranked-no-cost",
    ]


async def test_active_list_breaks_full_ties_by_id(backend):
    repo = backend.recommendations()
    await repo.upsert_recommendations([_record(f"tie-{index}", ranking=0.4, cost=3.0) for index in range(4)])

    active = await repo.get_active_recommendations()

    ids = [record.id for record in active]
    assert ids == sorted(ids)


# --------------------------------------------------------------------------- #
# Per-source reconciliation (E8, BUG-22)                                       #
# --------------------------------------------------------------------------- #


async def test_reconcile_never_stales_recommendations_of_incomplete_sources(backend):
    repo = backend.recommendations()
    await repo.upsert_recommendations(
        [
            _record("vpa-only", source="vpa", sources=["vpa"]),
            _record("vpa-and-native", source="vpa", sources=["vpa", "greenkube"]),
            _record("native-only", source="greenkube", sources=["greenkube"]),
            _record("krr-only", source="krr", sources=["krr"]),
            _record("still-emitted", source="greenkube", sources=["greenkube"]),
        ]
    )
    emitted = [_record("still-emitted")]

    stale_count = await repo.reconcile_active_recommendations(emitted, incomplete_sources=["vpa"])

    active = {record.pod_name for record in await repo.get_active_recommendations()}
    assert stale_count == 2
    assert active == {"vpa-only", "vpa-and-native", "still-emitted"}


async def test_reconcile_uses_primary_source_for_legacy_rows_without_provenance(backend):
    repo = backend.recommendations()
    await repo.upsert_recommendations([_record("legacy-vpa", source="vpa", sources=[])])
    async with backend.managers[0].connection_scope() as conn:
        await conn.execute("UPDATE recommendation_history SET sources = NULL")
        if backend.name == "sqlite":
            await conn.commit()

    assert await repo.reconcile_active_recommendations([], incomplete_sources=["vpa"]) == 0
    assert await repo.reconcile_active_recommendations([], incomplete_sources=[]) == 1


async def test_reconcile_without_incomplete_sources_keeps_previous_behavior(backend):
    repo = backend.recommendations()
    await repo.upsert_recommendations([_record("a"), _record("b", namespace="other")])

    assert await repo.reconcile_active_recommendations([], namespace="prod") == 1
    assert [record.pod_name for record in await repo.get_active_recommendations()] == ["b"]
