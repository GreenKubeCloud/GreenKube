# tests/core/optimization/test_track_record.py
"""Source track record: Beta(alpha, beta) per source family and capability (roadmap I3)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.core.optimization.track_record import (
    METHOD_MEASUREMENTS,
    METHOD_RECOMMENDATION_STATUS,
    TrackRecord,
    book_from_counts,
    load_track_record,
    outcome_counts_from_recommendations,
    outcome_of,
    source_family,
)
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType


def _record(status, verification=None, source="vpa", rec_type=RecommendationType.RIGHTSIZING_CPU):
    return RecommendationRecord(
        pod_name="api",
        namespace="prod",
        type=rec_type,
        description="x",
        status=status,
        verification_status=verification,
        source=source,
    )


class TestOutcomes:
    @pytest.mark.parametrize(
        "status,verification,outcome",
        [
            (RecommendationStatus.VERIFIED, "passed", "realized"),
            (RecommendationStatus.APPLIED, "failed", "not_realized"),
            (RecommendationStatus.APPLIED, "inconclusive", "inconclusive"),
            (RecommendationStatus.ROLLBACK_REVIEW, "failed", "rollback_review"),
            (RecommendationStatus.REVERTED, None, "rollback_review"),
            (RecommendationStatus.APPLIED, "in_progress", None),
            (RecommendationStatus.ACTIVE, None, None),
        ],
    )
    def test_outcome_of_a_record(self, status, verification, outcome):
        assert outcome_of(_record(status, verification)) == outcome

    def test_goldilocks_counts_as_vpa(self):
        assert source_family("goldilocks") == "vpa"
        assert source_family("push:ci") == "push"
        assert source_family(None) == "greenkube"


class TestBeta:
    def test_uniform_prior_and_updates(self):
        record = TrackRecord("vpa", "cpu_rightsizing", realized=14, not_realized=1, rollback=1, inconclusive=3)
        assert (record.alpha, record.beta) == (15.0, 3.0)
        assert record.decided == 16
        assert record.mean == pytest.approx(15 / 18)
        assert record.summary() == "vpa: 14/16 realized"
        assert record.to_dict()["inconclusive"] == 3

    def test_counts_from_recommendations(self):
        records = [
            _record(RecommendationStatus.VERIFIED, "passed"),
            _record(RecommendationStatus.VERIFIED, "passed"),
            _record(RecommendationStatus.APPLIED, "failed"),
            _record(RecommendationStatus.ROLLBACK_REVIEW),
            _record(RecommendationStatus.APPLIED, "inconclusive", source="greenkube"),
            _record(RecommendationStatus.APPLIED, "pending"),
        ]
        book = book_from_counts(outcome_counts_from_recommendations(records), method=METHOD_RECOMMENDATION_STATUS)
        vpa = book.get("vpa", "cpu_rightsizing")
        assert (vpa.realized, vpa.not_realized, vpa.rollback) == (2, 1, 1)
        assert book.get("greenkube", "cpu_rightsizing").inconclusive == 1
        assert book.get("karpenter", "node_pool").decided == 0

    def test_counts_keyed_by_tuple_and_contaminated_outcomes(self):
        book = book_from_counts(
            {("vpa", "memory_rightsizing"): {"realized": 3, "not_realized": 1, "contaminated": 2}},
        )
        record = book.get("vpa", "memory_rightsizing")
        assert (record.realized, record.not_realized, record.inconclusive) == (3, 1, 2)


class TestLoading:
    async def test_prefers_measurement_outcomes(self):
        repo = MagicMock()
        repo.get_outcome_counts_by_source_capability = AsyncMock(
            return_value=[{"source": "vpa", "capability": "cpu_rightsizing", "realized": 5, "rollback_review": 1}]
        )
        repo.get_recommendations_by_statuses = AsyncMock()
        book = await load_track_record(repo)
        assert book.method == METHOD_MEASUREMENTS
        assert book.get("vpa", "cpu_rightsizing").realized == 5
        repo.get_recommendations_by_statuses.assert_not_called()

    async def test_falls_back_to_recommendation_status(self):
        repo = MagicMock(spec=["get_recommendations_by_statuses"])
        repo.get_recommendations_by_statuses = AsyncMock(
            return_value=[_record(RecommendationStatus.VERIFIED, "passed")]
        )
        book = await load_track_record(repo)
        assert book.method == METHOD_RECOMMENDATION_STATUS
        assert book.get("vpa", "cpu_rightsizing").realized == 1

    async def test_unreadable_store_yields_priors(self):
        repo = MagicMock(spec=["get_recommendations_by_statuses"])
        repo.get_recommendations_by_statuses = AsyncMock(side_effect=RuntimeError("down"))
        book = await load_track_record(repo)
        assert book.records == {}
