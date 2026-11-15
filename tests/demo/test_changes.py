"""Demo changes: measured changes with the production capacity verdicts (WP-08.1)."""

import pytest

from greenkube.core.db import db_manager
from greenkube.demo.changes import DEMO_CHANGES, build_demo_change, populate_demo_changes
from greenkube.demo.data_generator import generate_recommendations
from greenkube.models.metrics import RecommendationStatus
from greenkube.storage.change_repository import ChangeRepository
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository
from greenkube.verify.model import ChangeOutcome, MeasurementState


def _applied_with_ids():
    landed = {RecommendationStatus.APPLIED, RecommendationStatus.VERIFIED}
    applied = [rec for rec in generate_recommendations() if rec.status in landed]
    return [rec.model_copy(update={"id": index + 1}) for index, rec in enumerate(applied)]


def _member(spec, applied):
    return next(rec for rec in applied if rec.pod_name == spec.pod_name and rec.namespace == spec.namespace)


def test_every_demo_change_has_an_applied_recommendation():
    applied = _applied_with_ids()
    for spec in DEMO_CHANGES:
        assert _member(spec, applied).applied_at is not None


def test_capacity_verdicts_come_from_the_specs():
    applied = _applied_with_ids()
    verdicts = {}
    for spec in DEMO_CHANGES:
        change, measurement, state = build_demo_change(spec, _member(spec, applied))
        assert measurement.capacity is not None
        assert measurement.capacity["method"] == "measured"
        assert measurement.evidence_sha256
        assert change.recommendation_ids == [_member(spec, applied).id]
        verdicts[spec.owner_name] = (measurement.capacity["verdict"], measurement.outcome, state)
    assert verdicts["pricing-service"] == ("released", ChangeOutcome.REALIZED, MeasurementState.REALIZED)
    # Merged 900m instead of the proposed 800m: 75% of the proposed release, while the cost is inconclusive.
    assert verdicts["api-gateway"] == ("partially_released", ChangeOutcome.INCONCLUSIVE, MeasurementState.INCONCLUSIVE)
    assert verdicts["lens-configurator"][0] == "released"


def test_released_cores_are_measured_per_replica():
    applied = _applied_with_ids()
    spec = next(spec for spec in DEMO_CHANGES if spec.owner_name == "pricing-service")
    _change, measurement, _state = build_demo_change(spec, _member(spec, applied))
    capacity = measurement.capacity
    assert capacity is not None
    assert capacity["per_replica"]["cpu_cores"] == pytest.approx(0.35)


@pytest.mark.asyncio
async def test_populate_records_decided_changes_in_sqlite():
    await db_manager.setup_sqlite(db_path=":memory:")
    try:
        reco_repo = SQLiteRecommendationRepository(db_manager)
        changes = ChangeRepository(db_manager)
        await reco_repo.save_recommendations(generate_recommendations())

        assert await populate_demo_changes(reco_repo, changes) == len(DEMO_CHANGES)
        # Idempotent: a second run records nothing new.
        assert await populate_demo_changes(reco_repo, changes) == 0

        rows = await changes.list_changes(limit=10)
        assert len(rows) == len(DEMO_CHANGES)
        states = {change.target.name: status.state for change, status in rows}
        assert states["api-gateway"] == MeasurementState.INCONCLUSIVE
        assert states["pricing-service"] == MeasurementState.REALIZED
        for change, _status in rows:
            measurements = await changes.list_measurements(change.change_id)
            assert len(measurements) == 1
            assert measurements[0].capacity is not None
    finally:
        await db_manager.close()
