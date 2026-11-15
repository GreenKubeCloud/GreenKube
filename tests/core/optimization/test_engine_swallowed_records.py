"""REC-01: the run diff never counts a record the repository swallowed as new.

A scan that restates a change applied an hour ago (lagging rollup) is absorbed
by the applied row: no active recommendation exists for it, so the run must not
report it in ``new`` nor in the persisted recommendations.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.engine import OptimizationEngine
from greenkube.models.metrics import Recommendation, RecommendationType
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository

MIB = 1024 * 1024


@pytest.fixture
async def sqlite_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "swallow.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield manager
    finally:
        await manager.close()


def _memory(current: int, proposed: int) -> Recommendation:
    return Recommendation(
        pod_name="api",
        namespace="shop",
        container_name="app",
        type=RecommendationType.RIGHTSIZING_MEMORY,
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        description="memory rightsizing",
        current_memory_request_bytes=current * MIB,
        recommended_memory_request_bytes=proposed * MIB,
    )


async def test_a_restatement_of_an_applied_change_is_not_new(sqlite_db):
    repo = SQLiteRecommendationRepository(sqlite_db)
    engine = OptimizationEngine(sources=[])
    diff, kept = await engine._persist([_memory(1024, 308)], repo)
    assert len(diff.new) == 1 and len(kept) == 1
    [row] = await repo.get_active_recommendations(namespace="shop")
    assert row.id is not None
    await repo.update_recommendation_fields(
        row.id,
        {
            "status": "applied",
            "applied_at": datetime.now(timezone.utc) - timedelta(hours=1),
            "actual_memory_request_bytes": 308 * MIB,
        },
    )

    diff, kept = await engine._persist([_memory(1024, 308)], repo)

    assert diff.new == []
    assert kept == []
    assert await repo.get_active_recommendations(namespace="shop") == []
