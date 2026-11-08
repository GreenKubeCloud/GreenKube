# tests/core/optimization/test_engine_value_scoring.py
"""Scans persist the expected value with the measured source track record (I1, I3)."""

from unittest.mock import MagicMock

from greenkube.core.optimization.engine import OptimizationEngine
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository

from .test_engine_source_isolation import (  # pyrefly: ignore[missing-import]
    FakeSource,
    _engine,
    _rec,
    sqlite_db,  # noqa: F401
)


async def test_scan_persists_value_scored_with_the_track_record(sqlite_db):  # noqa: F811
    repo = SQLiteRecommendationRepository(sqlite_db)
    # History: the native source's CPU rightsizing failed to realize twice.
    failed = [
        RecommendationRecord.from_recommendation(_rec(f"old-{index}")).model_copy(
            update={"status": RecommendationStatus.APPLIED, "verification_status": "failed"}
        )
        for index in range(2)
    ]
    await repo.save_recommendations(failed)

    engine: OptimizationEngine = _engine([FakeSource("greenkube", [_rec("api")])])
    await engine.run_scan(MagicMock(), MagicMock(), repo)

    (stored,) = await repo.get_active_recommendations()
    assert stored.value is not None
    assert stored.ranking_score == stored.value["ev"]
    track = stored.value["components"]["track_record"]
    assert track == {"source_family": "greenkube", "capability": "cpu_rightsizing", "realized": 0, "failed": 2}
    assert stored.value["p_success"] < 0.5
