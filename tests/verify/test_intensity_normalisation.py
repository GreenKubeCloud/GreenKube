"""The CO2e saving is the energy saving valued at the post-window carbon factor (MEA-03).

A windier or calmer post week must not be booked as a carbon saving or loss of
the change (sim_co2e.py G0-G2).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from greenkube.verify.confounders import ConfounderDetector
from greenkube.verify.model import ChangeEvent, ChangeOutcome, HourBucket, WorkloadRef
from greenkube.verify.strategy import MeasurementSettings, RequestRightsizingStrategy

UTC = timezone.utc
MERGE = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)


def _buckets(start: datetime, kwh: float, intensity: float, cores: float, embodied: float = 0.0) -> List[HourBucket]:
    return [
        HourBucket(
            hour=start + timedelta(hours=index),
            coverage=1.0,
            replicas=1.0,
            core_hours=cores,
            gib_hours=1.0,
            cost=cores * 0.03,
            cost_cpu=cores * 0.03,
            cost_ram=0.0,
            cost_coverage=1.0,
            energy_kwh=kwh,
            co2e_g=kwh * intensity,
            co2e_coverage=1.0,
            embodied_co2e_g=embodied or None,
            embodied_coverage=1.0 if embodied else None,
            instance_types={"m5": 1.0},
        )
        for index in range(168)
    ]


def _run(pre_args, post_args):
    change = ChangeEvent(
        change_id="c1",
        target=WorkloadRef(namespace="ns", kind="Deployment", name="web"),
        rollout_started_at=MERGE,
        rollout_completed_at=MERGE,
        baseline_window_end=MERGE,
    )
    strategy = RequestRightsizingStrategy()
    settings = MeasurementSettings()
    plan = strategy.required_windows(change, settings)
    pre = _buckets(datetime(2026, 11, 2, 10, 0, tzinfo=UTC), *pre_args)
    post = _buckets(MERGE + timedelta(hours=1), *post_args)
    confounders = ConfounderDetector().detect(change, plan, pre=pre, post=post)
    result = strategy.estimate(
        change,
        plan,
        pre,
        post,
        load=None,
        confounders=confounders,
        projections={},
        settings=settings,
        seed_parts=("c", 1),
    )
    return result, {item.quantity: item for item in result.estimates}, confounders


def test_same_intensity_keeps_the_true_saving():
    result, estimates, confounders = _run((0.05, 300, 1.0), (0.04, 300, 0.5))
    assert estimates["co2e_g"].rate_per_hour == pytest.approx(3.0, rel=1e-6)
    assert estimates["energy_kwh"].rate_per_hour == pytest.approx(0.01, rel=1e-6)
    assert result.counterfactual_params["co2e_basis"] == "energy_delta_at_post_intensity"
    assert result.counterfactual_params["co2e_factor_post"] == pytest.approx(300.0)
    assert not [item for item in confounders if item.kind == "intensity_shift"]


@pytest.mark.parametrize(("intensity", "expected"), [(200, 2.0), (400, 4.0)])
def test_an_intensity_move_is_not_booked_as_saving(intensity, expected):
    result, estimates, confounders = _run((0.05, 300, 1.0), (0.04, intensity, 0.5))
    co2e = estimates["co2e_g"]
    assert co2e.rate_per_hour == pytest.approx(expected, rel=1e-6)
    assert co2e.outcome is ChangeOutcome.REALIZED
    (shift,) = [item for item in confounders if item.kind == "intensity_shift"]
    assert shift.severity == "moderate"
    assert shift.evidence["before"] == pytest.approx(300.0)
    assert shift.evidence["after"] == pytest.approx(intensity)
    # The raw difference of the two weeks' emissions is reported next to the claim.
    assert result.counterfactual_params["co2e_raw_delta"] == pytest.approx(15.0 - 0.04 * intensity, rel=1e-6)


def test_embodied_emissions_keep_their_own_delta():
    _, estimates, _ = _run((0.05, 300, 1.0, 2.0), (0.04, 200, 0.5, 1.0))
    assert estimates["co2e_g"].rate_per_hour == pytest.approx(0.01 * 200 + 1.0, rel=1e-6)


def test_without_energy_the_raw_delta_is_measured_and_flagged():
    pre = (0.0, 300, 1.0)
    result, estimates, confounders = _run(pre, (0.0, 300, 0.5))
    assert result.counterfactual_params["co2e_basis"] == "raw_delta"
    assert ("co2e_raw_delta", "moderate") in [(item.kind, item.severity) for item in confounders]
    assert estimates["co2e_g"].quantity == "co2e_g"
