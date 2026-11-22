"""A regenerated rightsizing that restates a change already recorded is absorbed (bug 1).

Analyzers read requests from the hourly usage rollup, which keeps the replaced
request for up to an hour after a rollout: a scan right after a merged change
regenerates the pre-change advice. It must not be offered again.
"""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.recommendation_realization import (
    ROLLUP_LAG_WINDOW,
    absorbs_regenerated_recommendation,
)
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType

NOW = datetime.now(timezone.utc)
MIB = 1024 * 1024


def _cpu(current: int, recommended: int, **updates) -> RecommendationRecord:
    record = RecommendationRecord(
        pod_name="web",
        namespace="apps",
        container_name="web",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="cpu",
        scope="workload",
        owner_kind="Deployment",
        owner_name="web",
        current_cpu_request_millicores=current,
        recommended_cpu_request_millicores=recommended,
    )
    return record.model_copy(update=updates)


def _recorded(**updates) -> RecommendationRecord:
    fields = {
        "status": RecommendationStatus.VERIFYING,
        "actual_cpu_request_millicores": 10,
        "applied_at": NOW - timedelta(minutes=5),
    }
    fields.update(updates)
    return _cpu(400, 10, **fields)


@pytest.mark.parametrize(
    ("current", "recommended"),
    [(400, 10), (400, 12), (400, 50), (35, 10)],
    ids=["same-advice", "same-baseline", "same-baseline-other-proposal", "proposal-already-live"],
)
def test_lagging_regeneration_is_absorbed(current, recommended):
    assert absorbs_regenerated_recommendation(_recorded(), _cpu(current, recommended), NOW)


def test_new_advice_from_fresh_data_is_not_absorbed():
    # The rollup caught up: current is the live 10m and the advice differs.
    assert not absorbs_regenerated_recommendation(_recorded(), _cpu(10, 25), NOW)


def test_regeneration_after_the_lag_window_is_new_advice():
    recorded = _recorded(applied_at=NOW - ROLLUP_LAG_WINDOW - timedelta(minutes=1))
    assert not absorbs_regenerated_recommendation(recorded, _cpu(400, 10), NOW)


def test_unknown_actual_falls_back_to_the_proposal():
    recorded = _recorded(actual_cpu_request_millicores=None)
    assert absorbs_regenerated_recommendation(recorded, _cpu(35, 10), NOW)


def test_memory_baseline_is_matched_per_resource():
    recorded = RecommendationRecord(
        pod_name="web",
        namespace="apps",
        type=RecommendationType.RIGHTSIZING_MEMORY,
        description="memory",
        status=RecommendationStatus.VERIFIED,
        current_memory_request_bytes=384 * MIB,
        recommended_memory_request_bytes=16 * MIB,
        actual_memory_request_bytes=16 * MIB,
        applied_at=NOW - timedelta(minutes=30),
    )
    regenerated = recorded.model_copy(
        update={"status": RecommendationStatus.ACTIVE, "actual_memory_request_bytes": None, "applied_at": None}
    )
    assert absorbs_regenerated_recommendation(recorded, regenerated, NOW)
    # Different resource: never absorbed by a CPU change.
    assert not absorbs_regenerated_recommendation(_recorded(), regenerated, NOW)


def test_other_recommendation_types_are_never_absorbed():
    recorded = RecommendationRecord(
        pod_name="web",
        namespace="apps",
        type=RecommendationType.ZOMBIE_POD,
        description="zombie",
        status=RecommendationStatus.VERIFIED,
        applied_at=NOW,
    )
    assert not absorbs_regenerated_recommendation(recorded, recorded, NOW)


@pytest.fixture(autouse=True)
def _wall_clock_at_test_time(monkeypatch):
    """Re-read ``NOW`` for each test: a wall-clock constant taken at import ages during a long suite."""
    monkeypatch.setitem(globals(), "NOW", datetime.now(timezone.utc))
