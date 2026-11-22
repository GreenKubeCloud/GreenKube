"""Projected released capacity of recommendations (WP-08.1)."""

from __future__ import annotations

import pytest

from greenkube.impact.types import AssetDeletion, CapacityDelta
from greenkube.models.metrics import RecommendationRecord, RecommendationType

GIB = 1024**3


def _record(**overrides) -> RecommendationRecord:
    values = {
        "id": 1,
        "type": RecommendationType.RIGHTSIZING_CPU,
        "description": "cut",
        "current_cpu_request_millicores": 1000,
        "recommended_cpu_request_millicores": 100,
        "potential_savings_cost": 0.0,
        "impact": {
            "kind": "container_resources",
            "allocated_cost_annual": 236.52,
            "expected_billable_cost_annual": 0.0,
            "capacity_released": {
                "cpu_core_hours": 0.9 * 2 * 8760,
                "mem_gib_hours": 0.0,
                "replica_hours": 2 * 8760,
                "node_hours": 0.0,
            },
        },
    }
    values.update(overrides)
    return RecommendationRecord(**values)


def test_api_record_carries_projected_released_capacity_with_units_and_period():
    record = _record()
    payload = record.model_dump(mode="json")
    capacity = payload["released_capacity"]
    assert capacity["method"] == "projected"
    assert capacity["period"] == "year"
    assert capacity["annual"]["cpu_core_hours"] == pytest.approx(15768.0)
    assert capacity["units"]["cpu_core_hours"] == "core-hours per year"
    assert capacity["units"]["cpu_cores"] == "cores"
    instantaneous = capacity["instantaneous"]
    assert instantaneous["cpu_cores_per_replica"] == pytest.approx(0.9)
    assert instantaneous["replicas"] == pytest.approx(2.0)
    assert instantaneous["cpu_cores"] == pytest.approx(1.8)
    assert capacity["direction"] == "release"
    # Never money: no currency, never folded into the cost savings.
    assert "currency" not in capacity
    assert payload["potential_savings_cost"] == 0.0


def test_increase_is_a_negative_release():
    record = _record(
        type=RecommendationType.RIGHTSIZING_MEMORY,
        current_cpu_request_millicores=None,
        recommended_cpu_request_millicores=None,
        current_memory_request_bytes=GIB,
        recommended_memory_request_bytes=2 * GIB,
        impact={"capacity_released": {"cpu_core_hours": 0.0, "mem_gib_hours": -8760.0, "replica_hours": 8760.0}},
    )
    capacity = record.released_capacity
    assert capacity is not None
    assert capacity["instantaneous"]["mem_gib"] == pytest.approx(-1.0)
    assert capacity["direction"] == "increase"


def test_no_impact_means_no_released_capacity():
    assert _record(impact=None).released_capacity is None


def test_asset_deletions_release_storage_gib_and_load_balancers():
    from greenkube.core.config import Config
    from greenkube.impact import CostSource, ImpactProjector, PricingCatalog

    catalog = PricingCatalog(documents={provider: {} for provider in ("aws", "gcp", "azure", "ovh", "scaleway")})
    projector = ImpactProjector(Config(), catalog=catalog)
    pv = projector.asset_deletion(
        asset_kind="persistent_volume",
        allocated_cost_annual=120.0,
        component="storage",
        cost_source=CostSource.CUSTOM_RATE,
        storage_gib=100.0,
    )
    assert pv.capacity_released.storage_gib == pytest.approx(100.0)
    assert pv.to_dict()["capacity_released"]["storage_gib"] == pytest.approx(100.0)
    lb = projector.asset_deletion(
        asset_kind="load_balancer",
        allocated_cost_annual=200.0,
        component="lb",
        cost_source=CostSource.CUSTOM_RATE,
        load_balancers=1,
    )
    assert lb.capacity_released.load_balancers == 1
    assert CapacityDelta().to_dict()["storage_gib"] == 0.0
    assert AssetDeletion.__dataclass_fields__["storage_gib"].default == 0.0
