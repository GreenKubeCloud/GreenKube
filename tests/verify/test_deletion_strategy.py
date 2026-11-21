"""``ResourceDeletionStrategy`` (roadmap H6): a deletion realizes the released allocated rate."""

from __future__ import annotations

from datetime import timedelta

import pytest

from greenkube.verify.deletion import METHOD_ALLOCATED_RATE, METHOD_PRE_WINDOW, ResourceDeletionStrategy
from greenkube.verify.model import ChangeOutcome, Confounder, HourBucket
from greenkube.verify.strategy import HOURS_PER_YEAR, MeasurementSettings, strategy_for
from tests.verify.fixtures import change_event, hour_now  # pyrefly: ignore[missing-import]

SETTINGS = MeasurementSettings()
STRATEGY = ResourceDeletionStrategy()


def _estimate(
    pre=(), post=(), *, projected=None, confounders=(), completion_source="resource_absent", rate_sources=("opencost",)
):
    change = change_event(hour_now() - timedelta(days=9)).model_copy(
        update={"rollout_completed_source": completion_source}
    )
    plan = STRATEGY.required_windows(change, SETTINGS)
    return STRATEGY.estimate(
        change,
        plan,
        list(pre),
        list(post),
        load=None,
        confounders=list(confounders),
        projections={"allocated_cost": projected},
        settings=SETTINGS,
        seed_parts=(change.change_id, 1),
        rate_sources=rate_sources,
    )


def test_the_strategy_is_registered_for_the_delete_resource_kind() -> None:
    assert isinstance(strategy_for("delete_resource"), ResourceDeletionStrategy)


def test_an_absent_resource_realizes_its_allocated_rate_at_decision() -> None:
    result = _estimate(projected=219.15)

    (estimate,) = result.estimates
    assert result.outcome is ChangeOutcome.REALIZED
    assert estimate.method == METHOD_ALLOCATED_RATE
    assert estimate.annualized == pytest.approx(219.15)
    assert estimate.actual_per_hour == 0.0
    assert estimate.projection_accuracy == pytest.approx(1.0)
    assert estimate.rate_source == "opencost"
    assert result.strategy_version == "1.2:allocated_rate_at_decision:opencost"


@pytest.mark.parametrize("source", ["custom_rate", "none", "unknown"])
def test_a_configured_default_rate_is_never_booked_as_measured(source) -> None:
    """A LoadBalancer priced at LOAD_BALANCER_COST_PER_MONTH ($18.26/month) is deleted: the saving stays projected."""
    result = _estimate(projected=219.15, rate_sources=(source,))

    (estimate,) = result.estimates
    assert result.outcome is ChangeOutcome.INCONCLUSIVE
    assert estimate.annualized is None and estimate.rate_per_hour is None
    assert estimate.rate_source == source
    assert "stays a projection" in result.reasons[0]
    assert result.counterfactual_params["rate_source"] == source


def test_mixed_rate_sources_are_not_trusted() -> None:
    result = _estimate(projected=100.0, rate_sources=("opencost", "custom_rate"))
    assert result.outcome is ChangeOutcome.INCONCLUSIVE
    assert result.estimates[0].rate_source == "mixed"


def test_a_pricing_catalog_asset_price_is_trusted() -> None:
    result = _estimate(projected=100.0, rate_sources=("pricing_catalog",))
    assert result.outcome is ChangeOutcome.REALIZED


def test_the_cost_observed_before_the_change_wins_over_the_projection() -> None:
    start = hour_now() - timedelta(days=12)
    pre = [
        HourBucket(hour=start + timedelta(hours=index), coverage=1.0, cost=0.05, cost_coverage=1.0)
        for index in range(10)
    ]
    result = _estimate(pre, projected=100.0)

    (estimate,) = result.estimates
    assert estimate.method == METHOD_PRE_WINDOW and estimate.rate_source == "observed"
    assert estimate.rate_per_hour == pytest.approx(0.05)
    assert estimate.annualized == pytest.approx(0.05 * HOURS_PER_YEAR)


def test_a_resource_still_observed_after_the_change_is_not_realized() -> None:
    post = [HourBucket(hour=hour_now() - timedelta(days=1), coverage=0.5, cost=0.05, cost_coverage=1.0)]
    result = _estimate(post=post, projected=100.0)
    assert result.outcome is ChangeOutcome.NOT_REALIZED
    assert "still observed" in result.reasons[0]


def test_a_deletion_whose_absence_was_never_verified_claims_no_saving() -> None:
    result = _estimate(projected=219.15, completion_source="unknown")

    (estimate,) = result.estimates
    assert result.outcome is ChangeOutcome.INCONCLUSIVE
    assert estimate.annualized is None
    assert "never verified" in result.reasons[0]


def test_no_rate_is_inconclusive_and_a_severe_confounder_contaminates() -> None:
    assert _estimate().outcome is ChangeOutcome.INCONCLUSIVE
    severe = Confounder(kind="node_pool_change", severity="severe", detail="the node pool was replaced")
    contaminated = _estimate(projected=50.0, confounders=[severe])
    assert contaminated.outcome is ChangeOutcome.CONTAMINATED
    assert contaminated.reasons == ["the node pool was replaced"]


class _Changes:
    async def spec_observations(self, namespace, kind, name, since=None):
        return []


@pytest.mark.asyncio
async def test_a_deletion_marked_applied_by_hand_is_recorded_against_the_deleted_resource() -> None:
    from types import SimpleNamespace
    from typing import Any, cast

    from greenkube.models.metrics import RecommendationRecord, RecommendationType
    from greenkube.verify.service import ChangeMeasurementService

    service = ChangeMeasurementService(
        cast(Any, SimpleNamespace()),
        None,
        cast(Any, _Changes()),
        verifier=cast(Any, object()),
        config=SimpleNamespace(),
    )
    record = RecommendationRecord(
        id=3,
        pod_name="old-lb",
        namespace="shop",
        type=RecommendationType.ORPHANED_LOAD_BALANCER,
        description="x",
        scope="cluster",
        application_method="manual",
    )
    change = await service._build_change("chg-1", [record], snapshot=None, plan=None, now=hour_now())

    assert change.target.key == ("shop", "Service", "old-lb")
    assert change.action_kind == "delete_resource"
    assert change.application_method.value == "manual"


def test_the_rate_source_comes_from_the_projection_assumptions() -> None:
    from greenkube.models.metrics import RecommendationRecord, RecommendationType
    from greenkube.verify.service import _rate_source

    def record(impact):
        return RecommendationRecord(
            id=4, pod_name="old-pv", type=RecommendationType.ORPHANED_PERSISTENT_VOLUME, description="x", impact=impact
        )

    default = {"assumptions": {"cost_source": "custom_rate", "detail": "STORAGE_COST_PER_GIB_MONTH"}}
    assert _rate_source(record(default)) == "custom_rate"
    assert _rate_source(record({"assumptions": {"cost_source": "opencost"}})) == "opencost"
    assert _rate_source(record(None)) == "unknown"
