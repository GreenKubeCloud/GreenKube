"""The CO2e saving is the energy saving valued at the post-window carbon factor (MEA-03).

A windier or calmer post week must not be booked as a carbon saving or loss of
the change (sim_co2e.py G0-G2).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from greenkube.verify.confounders import ConfounderDetector, confidence_score
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
    assert result.counterfactual_params["co2e_basis"] == "energy_delta_at_post_hourly_intensity"
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


def _diurnal(hour_of_day: int) -> float:
    return math.sin(2 * math.pi * hour_of_day / 24)


def _diurnal_window(start: datetime, kwh: float, amplitude: float, *, record_factor: bool = True) -> List[HourBucket]:
    """Energy ``kwh + amplitude x phase`` at intensity ``300 + 100 x phase`` (phase from the hour of day)."""
    out = []
    for bucket in _buckets(start, kwh, 300, 1.0):
        phase = _diurnal(bucket.hour.hour)
        intensity = 300 + 100 * phase
        update = {"energy_kwh": kwh + amplitude * phase, "co2e_g": (kwh + amplitude * phase) * intensity}
        if record_factor:
            update["carbon_factor"] = intensity
        out.append(bucket.model_copy(update=update))
    return out


def _estimate_diurnal(pre: List[HourBucket], post: List[HourBucket]):
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
    result = strategy.estimate(
        change, plan, pre, post, load=None, confounders=[], projections={}, settings=settings, seed_parts=("c", 1)
    )
    confounders = ConfounderDetector().detect(change, plan, pre=pre, post=post)
    return result, {item.quantity: item for item in result.estimates}["co2e_g"], confounders


@pytest.mark.parametrize("amplitude", [0.03, -0.03, 0.04, -0.04])
def test_each_hour_is_valued_at_its_own_carbon_factor(amplitude):
    """Review wp07-2 G-corr: a constant 0.01 kWh/h saving claims 3.0 g/h whether load follows intensity or not.

    One window factor weighted by post-window energy claimed 3.5 g/h (load in
    phase with intensity) or 2.5 g/h (out of phase). At amplitude 0.04 the post
    energy is zero at 18:00 (review wp07-3 F1): the hour keeps its recorded
    intensity x PUE instead of falling back to the window factor (3.0625).
    """
    pre = _diurnal_window(datetime(2026, 11, 2, 10, 0, tzinfo=UTC), 0.05, amplitude)
    post = _diurnal_window(MERGE + timedelta(hours=1), 0.04, amplitude)
    result, co2e, confounders = _estimate_diurnal(pre, post)
    truth = math.fsum(0.01 * (300 + 100 * _diurnal(bucket.hour.hour)) for bucket in post) / len(post)
    assert co2e.rate_per_hour == pytest.approx(truth, rel=1e-6)
    assert result.counterfactual_params["co2e_basis"] == "energy_delta_at_post_hourly_intensity"
    assert result.counterfactual_params["co2e_factor_fallback_share"] == 0.0
    assert not [item for item in confounders if item.kind == "co2e_factor_fallback"]


def test_hours_without_a_post_factor_use_the_time_averaged_factor_and_are_reported():
    """Scale-to-zero at night: no post rows 00:00-05:59, so those baseline hours have no post factor (wp07-3 F1)."""
    pre = _diurnal_window(datetime(2026, 11, 2, 10, 0, tzinfo=UTC), 0.05, 0.03)
    post = [bucket for bucket in _diurnal_window(MERGE + timedelta(hours=1), 0.04, 0.03) if bucket.hour.hour >= 6]
    result, _, confounders = _estimate_diurnal(pre, post)
    params = result.counterfactual_params
    factors = [300 + 100 * _diurnal(bucket.hour.hour) for bucket in post]
    # The plain time average of the post factors, not the energy-weighted window factor.
    assert params["co2e_factor_fallback"] == pytest.approx(math.fsum(factors) / len(factors), rel=1e-6)
    assert params["co2e_factor_fallback"] != pytest.approx(params["co2e_factor_post"], rel=1e-3)
    night = math.fsum(bucket.energy_kwh or 0.0 for bucket in pre if bucket.hour.hour < 6)
    total = math.fsum(bucket.energy_kwh or 0.0 for bucket in pre)
    assert params["co2e_factor_fallback_share"] == pytest.approx(night / total, rel=1e-5)
    (finding,) = [item for item in confounders if item.kind == "co2e_factor_fallback"]
    assert finding.severity == "mild"
    assert finding.evidence["share"] == pytest.approx(night / total, rel=1e-5)


def test_hour_buckets_record_the_hours_carbon_factor():
    """Grid intensity x PUE of the trusted-carbon rows, independent of energy (wp07-3 F1)."""
    from greenkube.verify.model import TimeWindow
    from greenkube.verify.series import hourly_buckets
    from tests.verify.fixtures import workload_rows  # pyrefly: ignore[missing-import]

    window = TimeWindow(start=MERGE, end=MERGE + timedelta(hours=2))
    rows = [row.model_copy(update={"pue": 1.5, "joules": 0.0}) for row in workload_rows(window)]
    buckets = hourly_buckets(rows, window, 3600)
    assert [bucket.carbon_factor for bucket in buckets] == [pytest.approx(450.0), pytest.approx(450.0)]
    assert all(bucket.energy_kwh == 0.0 for bucket in buckets)


def test_an_intensity_shift_does_not_lower_the_cost_confidence():
    """The CO2e saving is normalised for intensity: the shift is reported but costs no confidence (review wp07-2)."""
    _, _, confounders = _run((0.05, 300, 1.0), (0.04, 200, 0.5))
    assert [item.kind for item in confounders] == ["intensity_shift"]
    assert confidence_score(confounders, 1.0, None) == confidence_score([], 1.0, None)
