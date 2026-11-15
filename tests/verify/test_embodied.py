"""Embodied CO2e in change measurement (component matching with older snapshots).

New baseline snapshots carry the embodied CO2e the Impact Model allocates by
reservation; snapshots frozen before it was measured do not, and their
measurement keeps comparing operational CO2e with the operational projection.
"""

from datetime import timedelta

import pytest

from greenkube.impact.types import CO2E_COMPONENTS, MEASURED_CO2E_COMPONENTS
from greenkube.verify.model import BaselineSnapshot, ChangeOutcome, sha256_hex
from greenkube.verify.series import hourly_buckets
from greenkube.verify.strategy import CO2E_WITH_EMBODIED, MeasurementSettings, RequestRightsizingStrategy
from tests.verify.fixtures import change_event, hour_now, workload_rows  # pyrefly: ignore[missing-import]

STEP = 300
SETTINGS = MeasurementSettings(bootstrap_samples=300)
STRATEGY = RequestRightsizingStrategy()
PROJECTIONS = {"allocated_cost": 100.0, "co2e_g": 40_000.0, CO2E_WITH_EMBODIED: 90_000.0}


def _without_embodied(buckets):
    """Buckets of a snapshot frozen before embodied CO2e, charged quantities and carbon factors were measured."""
    return [
        bucket.model_copy(
            update={
                "embodied_co2e_g": None,
                "embodied_coverage": None,
                "charged_core_hours": None,
                "charged_gib_hours": None,
                "carbon_factor": None,
                "cost_bases": None,
            }
        )
        for bucket in buckets
    ]


def _measure(*, pre_embodied: bool):
    change = change_event(hour_now() - timedelta(days=9))
    plan = STRATEGY.required_windows(change, SETTINGS)
    pre = hourly_buckets(
        workload_rows(plan.pre, cost=0.01, step=timedelta(minutes=5), co2e=12.0, embodied=8.0), plan.pre, STEP
    )
    if not pre_embodied:
        pre = _without_embodied(pre)
    post = hourly_buckets(
        workload_rows(plan.post, cost=0.008, cpu_request=300, step=timedelta(minutes=5), co2e=10.0, embodied=6.0),
        plan.post,
        STEP,
    )
    result = STRATEGY.estimate(
        change,
        plan,
        pre,
        post,
        load=None,
        confounders=[],
        projections=PROJECTIONS,
        settings=SETTINGS,
        seed_parts=(change.change_id, 1),
    )
    return result, {item.quantity: item for item in result.estimates}


def test_new_snapshots_measure_embodied_co2e_with_the_full_projection():
    result, estimates = _measure(pre_embodied=True)
    co2e = estimates["co2e_g"]
    assert co2e.components == list(CO2E_COMPONENTS)
    # Two pods: (12 + 8) - (10 + 6) g/h each.
    assert co2e.rate_per_hour == pytest.approx(8.0, rel=1e-6)
    assert co2e.projected_annual == pytest.approx(90_000.0)
    assert estimates["embodied_co2e_g"].rate_per_hour == pytest.approx(4.0, rel=1e-6)
    assert estimates["embodied_co2e_g"].claimed is False
    assert result.counterfactual_params["co2e_components"] == list(CO2E_COMPONENTS)


def test_older_snapshots_compare_operational_co2e_with_the_operational_projection():
    result, estimates = _measure(pre_embodied=False)
    co2e = estimates["co2e_g"]
    assert co2e.components == list(MEASURED_CO2E_COMPONENTS)
    assert co2e.rate_per_hour == pytest.approx(4.0, rel=1e-6)
    assert co2e.projected_annual == pytest.approx(40_000.0)
    assert estimates["embodied_co2e_g"].outcome is ChangeOutcome.INCONCLUSIVE
    assert result.counterfactual_params["co2e_components"] == list(MEASURED_CO2E_COMPONENTS)


def test_signed_embodied_increase_is_reported_not_floored():
    change = change_event(hour_now() - timedelta(days=9))
    plan = STRATEGY.required_windows(change, SETTINGS)
    pre = hourly_buckets(workload_rows(plan.pre, step=timedelta(minutes=5), embodied=4.0), plan.pre, STEP)
    post = hourly_buckets(workload_rows(plan.post, step=timedelta(minutes=5), embodied=6.0), plan.post, STEP)
    result = STRATEGY.estimate(
        change, plan, pre, post, load=None, confounders=[], projections={}, settings=SETTINGS, seed_parts=("c", 2)
    )
    embodied = next(item for item in result.estimates if item.quantity == "embodied_co2e_g")
    assert embodied.rate_per_hour == pytest.approx(-4.0, rel=1e-6)


def test_digest_of_a_snapshot_frozen_before_embodied_co2e_still_verifies():
    change = change_event(hour_now() - timedelta(days=9))
    plan = STRATEGY.required_windows(change, SETTINGS)
    hours = _without_embodied(hourly_buckets(workload_rows(plan.pre), plan.pre, STEP))
    snapshot = BaselineSnapshot(snapshot_id="s", change_id=change.change_id, window=plan.pre, hours=hours)
    # The payload an older release hashed: the bucket fields as they were, without the embodied ones.
    legacy = snapshot.model_dump(mode="json", exclude={"content_sha256", "created_at"})
    legacy.pop("health")  # added later (MEA-07), absent from older payloads
    for bucket in legacy["hours"]:
        bucket.pop("embodied_co2e_g")
        bucket.pop("embodied_coverage")
        bucket.pop("charged_core_hours")
        bucket.pop("charged_gib_hours")
        bucket.pop("carbon_factor")
        bucket.pop("cost_bases")
    assert snapshot.with_digest().content_sha256 == sha256_hex(legacy)


def test_new_snapshot_digest_covers_embodied_co2e():
    change = change_event(hour_now() - timedelta(days=9))
    plan = STRATEGY.required_windows(change, SETTINGS)
    hours = hourly_buckets(workload_rows(plan.pre, embodied=5.0), plan.pre, STEP)
    snapshot = BaselineSnapshot(snapshot_id="s", change_id=change.change_id, window=plan.pre, hours=hours)
    tampered = snapshot.model_copy(update={"hours": [hours[0].model_copy(update={"embodied_co2e_g": 1.0}), *hours[1:]]})
    assert snapshot.with_digest().content_sha256 != tampered.with_digest().content_sha256
