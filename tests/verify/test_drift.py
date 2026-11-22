"""Drift assessment of realized changes (C8) on synthetic series with a known drift."""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.verify.drift import MIN_DAY_HOURS, DriftSettings, assess_drift, detect_spec_drift
from greenkube.verify.model import TimeWindow, WorkloadSpecObservation
from greenkube.verify.series import hourly_buckets, price_card
from tests.verify.fixtures import change_event, hour_now, workload_rows  # pyrefly: ignore[missing-import]

STEP = 300
SETTINGS = DriftSettings(threshold=0.5, sustained_days=3)


def _today():
    return hour_now().replace(hour=0)


def _buckets(start, end, **values):
    window = TimeWindow(start=start, end=end)
    return hourly_buckets(workload_rows(window, **values), window, STEP)


def _windows():
    today = _today()
    baseline = _buckets(today - timedelta(days=30), today - timedelta(days=23))
    post = _buckets(today - timedelta(days=22), today - timedelta(days=15), cpu_request=300, memory_request=1024**3)
    return today, baseline, post


def test_requests_creeping_back_for_three_days_regress_the_change():
    today, baseline, post = _windows()
    steady = _buckets(today - timedelta(days=7), today - timedelta(days=4), cpu_request=300, memory_request=1024**3)
    # A VPA in auto mode puts the CPU request back to 1 core: 85% of the per-replica saving is lost.
    drifted = _buckets(today - timedelta(days=4), today, cpu_request=1000, memory_request=1024**3)
    assessment = assess_drift(baseline, post, steady + drifted, SETTINGS)
    assert assessment.regressed
    assert assessment.onset == today - timedelta(days=4)
    assert [round(day.lost_share, 2) for day in assessment.days] == [0.0, 0.0, 0.0, 0.85, 0.85, 0.85, 0.85]
    assert assessment.baseline_per_replica == pytest.approx(0.1)
    assert assessment.realized_per_replica == pytest.approx(0.034)
    payload = assessment.payload(SETTINGS)
    assert payload["onset"] == (today - timedelta(days=4)).isoformat() and len(payload["days"]) == 7


def test_a_short_drift_does_not_regress_the_change():
    today, baseline, post = _windows()
    steady = _buckets(today - timedelta(days=7), today - timedelta(days=2), cpu_request=300, memory_request=1024**3)
    drifted = _buckets(today - timedelta(days=2), today, cpu_request=1000, memory_request=1024**3)
    assessment = assess_drift(baseline, post, steady + drifted, SETTINGS)
    assert not assessment.regressed and assessment.onset is None


def test_a_drift_below_the_threshold_does_not_regress_the_change():
    today, baseline, post = _windows()
    # 500m: (0.04 + 0.01 - 0.034) / 0.066 = 24% of the saving lost.
    partial = _buckets(today - timedelta(days=7), today, cpu_request=500, memory_request=1024**3)
    assessment = assess_drift(baseline, post, partial, SETTINGS)
    assert not assessment.regressed
    assert all(day.lost_share == pytest.approx(0.2424, abs=1e-3) for day in assessment.days)


def test_more_replicas_under_load_are_not_a_drift():
    today, baseline, post = _windows()
    busy = _buckets(
        today - timedelta(days=7),
        today,
        pods=("a", "b", "c", "d", "e", "f"),
        cpu_request=300,
        memory_request=1024**3,
    )
    assessment = assess_drift(baseline, post, busy, SETTINGS)
    assert not assessment.regressed
    assert all(abs(day.lost_share) < 1e-9 for day in assessment.days)


def test_a_gap_breaks_the_sustained_run():
    today, baseline, post = _windows()
    first = _buckets(today - timedelta(days=5), today - timedelta(days=3), cpu_request=1000, memory_request=1024**3)
    # One day with too few hours of data does not count either way.
    sparse = _buckets(
        today - timedelta(days=3),
        today - timedelta(days=3) + timedelta(hours=MIN_DAY_HOURS - 1),
        cpu_request=1000,
        memory_request=1024**3,
    )
    last = _buckets(today - timedelta(days=2), today, cpu_request=1000, memory_request=1024**3)
    assessment = assess_drift(baseline, post, first + sparse + last, SETTINGS)
    assert not assessment.regressed


def test_a_later_price_rise_does_not_reverse_a_saving_claimed_at_constant_prices():
    """Review wp07-2: the saving was measured at the baseline prices, so the monitor prices at them too."""
    today, baseline, post = _windows()
    card = price_card(baseline, "EUR")
    # The CPU unit price triples after the post-window: observed per-replica cost loses 73% of the saving.
    pricier = _buckets(today - timedelta(days=7), today, cpu_request=300, memory_request=1024**3, cpu_price=0.24)
    assessment = assess_drift(baseline, post, pricier, SETTINGS, price_card=card)
    assert not assessment.regressed
    assert all(abs(day.lost_share) < 1e-9 for day in assessment.days)
    assert assessment.payload(SETTINGS)["constant_price_basis"] == "charged"
    # Requests creeping back are still a drift at constant prices.
    drifted = _buckets(today - timedelta(days=4), today, cpu_request=1000, memory_request=1024**3, cpu_price=0.24)
    assert assess_drift(baseline, post, pricier[: 3 * 24] + drifted, SETTINGS, price_card=card).regressed


def test_a_post_window_without_per_replica_saving_is_not_monitored():
    today, baseline, _ = _windows()
    post = _buckets(today - timedelta(days=22), today - timedelta(days=15))
    assessment = assess_drift(baseline, post, post, SETTINGS)
    assert not assessment.regressed and "no per-replica saving" in assessment.reason


def test_settings_follow_the_configuration():
    class Cfg:
        CHANGE_DRIFT_THRESHOLD = 0.3
        CHANGE_DRIFT_SUSTAINED_DAYS = 5

    assert DriftSettings.from_config(Cfg()) == DriftSettings(threshold=0.3, sustained_days=5)


def _observation(at, digest="digest-after", uid="uid-api"):
    return WorkloadSpecObservation(
        namespace="prod", kind="Deployment", name="api", workload_uid=uid, spec_digest=digest, observed_at=at
    )


def test_spec_drift_ends_at_the_first_observation_of_the_trailing_difference():
    completed = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
    change = change_event(completed)
    observations = [
        _observation(completed - timedelta(hours=1), digest="digest-before"),
        _observation(completed + timedelta(days=1), digest="digest-image-1"),
        _observation(completed + timedelta(days=2), digest="digest-after"),
        _observation(completed + timedelta(days=3), digest="digest-image-2"),
        _observation(completed + timedelta(days=4), digest="digest-image-3"),
    ]
    drift = detect_spec_drift(change, observations)
    assert drift is not None and drift.event_type == "spec_drift"
    assert drift.accrual_end == completed + timedelta(days=3)
    assert drift.latest.spec_digest == "digest-image-3"


def test_a_template_back_to_the_change_is_no_drift():
    completed = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
    observations = [
        _observation(completed + timedelta(days=1), digest="digest-image-1"),
        _observation(completed + timedelta(days=2), digest="digest-after"),
    ]
    assert detect_spec_drift(change_event(completed), observations) is None
    assert detect_spec_drift(change_event(completed), []) is None


def test_a_recreated_workload_is_reported_as_such():
    completed = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
    drift = detect_spec_drift(change_event(completed), [_observation(completed + timedelta(hours=5), uid="uid-api-2")])
    assert drift is not None and drift.event_type == "workload_recreated"
    assert drift.accrual_end == completed + timedelta(hours=5)
