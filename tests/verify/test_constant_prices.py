"""Price moves between the windows are not savings (MEA-02).

The request effect is measured at the baseline unit prices of the frozen price
card, on the charged quantity ``max(request, usage)``; the observed delta is
reported as ``allocated_cost_observed`` and the difference as ``price_effect``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, List, Optional, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.models.metrics import CombinedMetric
from greenkube.verify.confounders import ConfounderDetector, confidence_score, is_contaminated
from greenkube.verify.model import ChangeEvent, ChangeOutcome, HourBucket, TimeWindow, WorkloadRef
from greenkube.verify.series import hourly_buckets, price_card
from greenkube.verify.strategy import MeasurementSettings, RequestRightsizingStrategy

UTC = timezone.utc
PRE_START = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
MERGE = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
GIB = 1024**3
#: 1.0 -> 0.5 core and 2 -> 1 GiB at 0.03 per core-hour and 0.005 per GiB-hour.
TRUE_SAVING = 0.0200


def _buckets(
    start: datetime,
    cores: float,
    gib: float,
    cpu_price: float,
    ram_price: float,
    *,
    charged_cores: Optional[float] = None,
    charged: bool = True,
    hours: int = 168,
) -> List[HourBucket]:
    """Hour buckets of an allocator that charges ``max(request, usage)`` at the given prices."""
    billed_cores = cores if charged_cores is None else charged_cores
    out = []
    for index in range(hours):
        cpu, ram = billed_cores * cpu_price, gib * ram_price
        out.append(
            HourBucket(
                hour=start + timedelta(hours=index),
                coverage=1.0,
                replicas=1.0,
                core_hours=cores,
                gib_hours=gib,
                charged_core_hours=billed_cores if charged else None,
                charged_gib_hours=gib if charged else None,
                cpu_usage_cores=0.01,
                cost=cpu + ram,
                cost_cpu=cpu,
                cost_ram=ram,
                cost_coverage=1.0,
                instance_types={"m5": 1.0},
            )
        )
    return out


def _measure(pre: List[HourBucket], post: List[HourBucket], *, card: Optional[dict] = None):
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
    confounders = ConfounderDetector().detect(change, plan, pre=pre, post=post)
    result = strategy.estimate(
        change,
        plan,
        pre,
        post,
        load=None,
        confounders=confounders,
        projections={"allocated_cost": None},
        settings=settings,
        seed_parts=("c1", 1),
        price_card=card if card is not None else price_card(pre, "EUR"),
    )
    estimates = {item.quantity: item for item in result.estimates}
    return result, estimates, confounders


def _post(cpu_price: float, ram_price: float, **kwargs: Any) -> List[HourBucket]:
    return _buckets(MERGE + timedelta(hours=1), 0.5, 1.0, cpu_price, ram_price, **kwargs)


def _pre(**kwargs: Any) -> List[HourBucket]:
    return _buckets(PRE_START, 1.0, 2.0, 0.03, 0.005, **kwargs)


def test_same_prices_measure_the_true_saving_with_no_price_effect():
    result, estimates, confounders = _measure(_pre(), _post(0.03, 0.005))
    assert estimates["allocated_cost"].rate_per_hour == pytest.approx(TRUE_SAVING, rel=1e-6)
    assert estimates["allocated_cost_observed"].rate_per_hour == pytest.approx(TRUE_SAVING, rel=1e-6)
    assert result.counterfactual_params["price_effect"] == pytest.approx(0.0, abs=1e-9)
    assert result.outcome is ChangeOutcome.REALIZED
    assert confounders == []


@pytest.mark.parametrize(("move", "observed"), [(-0.30, 0.0260), (0.30, 0.0140)])
def test_a_price_move_is_not_booked_as_saving(move, observed):
    """sim_measure.py C1/C2: the claimed saving stays 175.32/yr within 1%, with a price_card_change."""
    result, estimates, confounders = _measure(_pre(), _post(0.03 * (1 + move), 0.005 * (1 + move)))
    claimed = estimates["allocated_cost"]
    assert claimed.rate_per_hour == pytest.approx(TRUE_SAVING, rel=0.01)
    assert claimed.annualized == pytest.approx(175.32, rel=0.01)
    assert estimates["allocated_cost_observed"].rate_per_hour == pytest.approx(observed, rel=1e-6)
    assert not estimates["allocated_cost_observed"].claimed
    assert result.counterfactual_params["price_effect"] == pytest.approx(observed - TRUE_SAVING, rel=1e-6)
    prices = {item.evidence["resource"]: item for item in confounders if item.kind == "price_card_change"}
    assert set(prices) == {"cpu", "memory"}
    assert all(item.severity == "severe" for item in prices.values())
    assert f"{move:+.0%}" in prices["cpu"].detail
    assert is_contaminated(confounders) and result.outcome is ChangeOutcome.CONTAMINATED


def test_a_moderate_price_move_lowers_confidence_without_moving_the_saving():
    result, estimates, confounders = _measure(_pre(), _post(0.03 * 1.1, 0.005 * 1.1))
    assert estimates["allocated_cost"].rate_per_hour == pytest.approx(TRUE_SAVING, rel=0.01)
    assert sorted((item.kind, item.severity) for item in confounders) == [
        ("price_card_change", "moderate"),
        ("price_card_change", "moderate"),
    ]
    assert confidence_score(confounders, result.coverage, None) < 0.9


def test_usage_above_the_request_is_charged_and_not_a_saving():
    """OpenCost charges max(request, usage): a CPU request cut below a 1.5-core usage saves no CPU cost."""
    pre = _pre(charged_cores=1.5)
    post = _post(0.03, 0.005, charged_cores=1.5)
    result, estimates, confounders = _measure(pre, post)
    assert estimates["cost_cpu"].rate_per_hour == pytest.approx(0.0, abs=1e-9)
    assert estimates["allocated_cost"].rate_per_hour == pytest.approx(0.005, rel=1e-6)  # the 1 GiB cut only
    assert result.counterfactual_params["constant_price_basis"] == "charged"
    assert confounders == []


def test_a_snapshot_without_charged_quantities_uses_requests_and_says_so():
    pre = _pre(charged=False)
    post = _post(0.021, 0.0035, charged=False)
    result, estimates, confounders = _measure(pre, post)
    assert estimates["allocated_cost"].rate_per_hour == pytest.approx(TRUE_SAVING, rel=0.01)
    # Charged quantities derived as max(request, mean usage) and gib_hours (review wp07-2 S3).
    assert result.counterfactual_params["constant_price_basis"] == "charged_derived"
    # sim_measure.py C1: usage (0.01 core) stays below the request, so the per-request price is a price.
    assert sorted((item.evidence["resource"], item.severity) for item in confounders) == [
        ("cpu", "severe"),
        ("memory", "severe"),
    ]


def test_an_older_snapshot_cut_below_usage_books_only_the_real_saving():
    """Review wp07-2 S3: 1.0 -> 0.5 core with 0.8 core used after, snapshot without charged quantities.

    The per-request basis claimed 0.0200/h; the allocator charges 0.8 core
    after, so the true saving is 0.2 x 0.03 + 1 GiB x 0.005 = 0.0110/h.
    """
    pre = _pre(charged=False)
    legacy_card = price_card(pre, "EUR")
    assert "cpu_charged_core_hour" not in legacy_card
    post = [bucket.model_copy(update={"cpu_usage_cores": 0.8}) for bucket in _post(0.03, 0.005, charged_cores=0.8)]
    result, estimates, confounders = _measure(pre, post, card=legacy_card)
    assert estimates["allocated_cost"].rate_per_hour == pytest.approx(0.0110, rel=1e-6)
    assert result.counterfactual_params["constant_price_basis"] == "charged_derived"
    assert confounders == []


def test_without_cost_components_the_observed_basis_is_a_confounder():
    """Review wp07-2 S4: no CPU/RAM components, so a -30% price move cannot be removed: say so."""

    def bare(buckets: List[HourBucket]) -> List[HourBucket]:
        return [bucket.model_copy(update={"cost_cpu": None, "cost_ram": None}) for bucket in buckets]

    pre, post = bare(_pre()), bare(_post(0.021, 0.0035))
    change = ChangeEvent(
        change_id="c1",
        target=WorkloadRef(namespace="ns", kind="Deployment", name="web"),
        rollout_started_at=MERGE,
        rollout_completed_at=MERGE,
        baseline_window_end=MERGE,
    )
    plan = RequestRightsizingStrategy().required_windows(change, MeasurementSettings())
    confounders = ConfounderDetector().detect(change, plan, pre=pre, post=post, price_card=None)
    assert [(item.kind, item.severity) for item in confounders] == [("constant_prices_unavailable", "moderate")]
    # With components, no such finding.
    assert ConfounderDetector().detect(change, plan, pre=_pre(), post=_post(0.03, 0.005), price_card=None) == []


def test_the_frozen_price_card_values_the_saving():
    card = {"currency": "EUR", "cpu_charged_core_hour": 0.06, "memory_charged_gib_hour": 0.01}
    result, estimates, _ = _measure(_pre(), _post(0.03, 0.005), card=card)
    assert estimates["allocated_cost"].rate_per_hour == pytest.approx(2 * TRUE_SAVING, rel=1e-6)
    assert result.counterfactual_params["constant_prices"] == {"cpu": 0.06, "memory": 0.01}


def test_the_strategy_version_is_bumped():
    assert RequestRightsizingStrategy.version == "2.5"


# --------------------------------------------------------------------------- #
# Hour buckets and price card                                                  #
# --------------------------------------------------------------------------- #


def _row(moment: datetime, *, cpu_request: int, cpu_usage: Optional[int], memory_usage: Optional[int]):
    return CombinedMetric(
        pod_name="web-a",
        namespace="ns",
        owner_kind="Deployment",
        owner_name="web",
        timestamp=moment,
        total_cost=0.01,
        cost_cpu=0.008,
        cost_ram=0.002,
        cost_source="opencost",
        co2e_grams=0.0,
        joules=0.0,
        cpu_request=cpu_request,
        memory_request=GIB,
        cpu_usage_millicores=cpu_usage,
        memory_usage_bytes=memory_usage,
        usage_source="prometheus",
        duration_seconds=3600,
        sample_count=1,
        node="n1",
    )


def test_hour_buckets_carry_the_charged_quantity():
    window = TimeWindow(start=PRE_START, end=PRE_START + timedelta(hours=2))
    rows = [
        _row(PRE_START, cpu_request=500, cpu_usage=800, memory_usage=GIB // 2),
        _row(PRE_START + timedelta(hours=1), cpu_request=500, cpu_usage=None, memory_usage=2 * GIB),
    ]
    first, second = hourly_buckets(rows, window, 3600)
    assert first.core_hours == pytest.approx(0.5) and first.charged_core_hours == pytest.approx(0.8)
    assert first.charged_gib_hours == pytest.approx(1.0)
    assert second.charged_core_hours == pytest.approx(0.5)  # no trusted usage: the request
    assert second.charged_gib_hours == pytest.approx(2.0)
    card = price_card([first, second], "EUR")
    assert card["cpu_charged_core_hour"] == pytest.approx(0.016 / 1.3)
    assert card["cpu_core_hour"] == pytest.approx(0.016)


# --------------------------------------------------------------------------- #
# Price control without node-pool labels                                       #
# --------------------------------------------------------------------------- #


def _node(name: str, pool: Optional[str] = None):
    return SimpleNamespace(name=name, node_pool=pool, instance_type="m5", cpu_capacity_cores=4.0)


def _neighbour_rows(start: datetime, end: datetime, cpu_price: float) -> List[CombinedMetric]:
    return [
        CombinedMetric(
            pod_name="other-a",
            namespace="ns",
            owner_kind="Deployment",
            owner_name="other",
            timestamp=start,
            total_cost=cpu_price + 0.005,
            cost_cpu=cpu_price,
            cost_ram=0.005,
            cost_source="opencost",
            co2e_grams=0.0,
            joules=0.0,
            cpu_request=1000,
            memory_request=GIB,
            duration_seconds=3600,
            sample_count=1,
            node="n1",
        )
    ]


@pytest.mark.asyncio
async def test_nodes_without_a_pool_label_still_get_a_price_control():
    from greenkube.verify.service import ChangeMeasurementService

    change = ChangeEvent(
        change_id="c1",
        target=WorkloadRef(namespace="ns", kind="Deployment", name="web"),
        rollout_started_at=MERGE,
        rollout_completed_at=MERGE,
        baseline_window_end=MERGE,
    )
    plan = RequestRightsizingStrategy().required_windows(change, MeasurementSettings())
    node_repo = MagicMock()
    node_repo.get_latest_snapshots_before = AsyncMock(return_value=[_node("n1")])
    combined = MagicMock()

    async def read(start_time, end_time, namespace=None):
        price = 0.03 if end_time <= MERGE else 0.039
        return _neighbour_rows(start_time, end_time, price)

    combined.read_combined_metrics_smart = read
    changes = MagicMock()
    changes.changes_landed_between = AsyncMock(return_value=[])
    config = MagicMock()
    config.PROMETHEUS_QUERY_RANGE_STEP = "1h"
    service = ChangeMeasurementService(
        MagicMock(), combined, changes, verifier=cast(Any, MagicMock()), config=config, node_repo=node_repo
    )
    post_rows = [SimpleNamespace(node="n1")]
    context = await service._pool_context(change, plan, post_rows)
    assert context is not None
    assert context.scope == "workload_nodes"
    findings = ConfounderDetector().detect(change, plan, pool=context)
    cpu = [item for item in findings if item.evidence.get("resource") == "cpu"]
    assert len(cpu) == 1 and cpu[0].severity == "severe" and "+30%" in cpu[0].detail
    assert "no node-pool label" in cpu[0].detail
