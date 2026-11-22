# tests/impact/test_allocation.py
"""Golden and conservation tests for ReservationAwareAllocation (roadmap B2)."""

import random

import pytest

from greenkube.impact import (
    IDLE_WORKLOAD_NAME,
    IMPACT_MODEL_VERSION,
    ClusterSnapshot,
    CostSource,
    NodeImpactProfile,
    ReservationAwareImpactModel,
    ResourceSample,
    UnitPrices,
    WorkloadRef,
    allocate_node,
    node_totals,
    reservation_shares,
)


def _node(name, cores, mem, idle, peak, embodied, cpu_price, ram_price, pue, intensity):
    return NodeImpactProfile(
        name=name,
        cpu_capacity_cores=cores,
        mem_capacity_gib=mem,
        idle_watts=idle,
        max_watts=peak,
        embodied_g_per_hour=embodied,
        prices=UnitPrices(cpu_price, ram_price, "USD", CostSource.PRICING_CATALOG, "fixture"),
        pue=pue,
        grid_intensity_g_per_kwh=intensity,
    )


def _sample(pod, node, cpu_req, cpu_use, mem_req, mem_use, hours=1.0):
    return ResourceSample(
        ref=WorkloadRef("c1", "default", "Pod", pod),
        hours=hours,
        cpu_request_cores=cpu_req,
        cpu_usage_cores=cpu_use,
        mem_request_gib=mem_req,
        mem_usage_gib=mem_use,
        node=node,
    )


NODES = {
    # Under-reserved node: unreserved capacity goes to __idle__.
    "a": _node("a", 4, 16, 40, 120, 10, 0.04, 0.005, 1.0, 100),
    # Over-committed node: usage above requests, shares normalized, no idle.
    "b": _node("b", 2, 8, 20, 60, 5, 0.05, 0.01, 1.5, 200),
    # Mostly idle node with a pod present for half the window and a BestEffort pod.
    "c": _node("c", 8, 32, 80, 200, 20, 0.03, 0.004, 1.2, 50),
}

SAMPLES = [
    _sample("a1", "a", 1.0, 0.5, 4, 2),
    _sample("a2", "a", 0.5, 1.0, 2, 3),
    _sample("a3", "a", 0.0, 0.5, 0, 1),
    _sample("a4", "a", 1.0, 0.0, 4, 0),
    _sample("b1", "b", 1.0, 1.5, 4, 2),
    _sample("b2", "b", 1.0, 0.5, 4, 5),
    _sample("b3", "b", 0.5, 0.0, 1, 0.5),
    _sample("c1", "c", 2.0, None, 8, None),
    _sample("c2", "c", 2.0, 0.0, 8, 0, hours=0.5),
    _sample("c3", "c", 0.0, None, 0, None),
]

# Hand-computed per-pod values: (cost, energy kWh, operational g, embodied g).
# See docs/methodology/impact-model.md for the formulas.
GOLDEN = {
    "a1": (0.06, 0.02, 2.0, 2.5),
    "a2": (0.055, 0.03, 3.0, 2.5),
    "a3": (0.025, 0.015, 1.5, 1.25),
    "a4": (0.06, 0.01, 1.0, 2.5),
    ("a", IDLE_WORKLOAD_NAME): (0.04, 0.005, 0.5, 1.25),
    "b1": (0.082, 0.04, 12.0, 2.5),
    "b2": (0.04 + 0.1 / 3, 0.01 + 0.02 / 3, 5.0, 5 / 3),
    "b3": (0.008 + 0.1 / 6, 0.02 / 6, 1.0, 5 / 6),
    ("b", IDLE_WORKLOAD_NAME): (0.0, 0.0, 0.0, 0.0),
    "c1": (0.092, 0.02, 1.2, 5.0),
    "c2": (0.046, 0.01, 0.6, 2.5),
    "c3": (0.0, 0.0, 0.0, 0.0),
    ("c", IDLE_WORKLOAD_NAME): (0.23, 0.05, 3.0, 12.5),
}


def _key(impact):
    if impact.scope == "idle":
        return (impact.node, IDLE_WORKLOAD_NAME)
    return impact.ref.name


class TestGoldenFixture:
    def test_three_nodes_ten_pods_match_hand_computed_values(self):
        result = ReservationAwareImpactModel().allocate(ClusterSnapshot("c1", 1.0, NODES, SAMPLES))

        assert result.unallocated == []
        by_key = {_key(impact): impact for impact in result.impacts}
        assert set(by_key) == set(GOLDEN)
        for key, (cost, energy, operational, embodied) in GOLDEN.items():
            impact = by_key[key]
            assert impact.cost_allocated.total == pytest.approx(cost, abs=1e-9), key
            assert impact.energy_kwh.total == pytest.approx(energy, abs=1e-9), key
            assert impact.co2e_operational_g == pytest.approx(operational, abs=1e-9), key
            assert impact.co2e_embodied_g == pytest.approx(embodied, abs=1e-9), key

    def test_request_cuts_lower_allocated_co2e(self):
        """Idle and embodied follow reservations, so cutting a request lowers allocated CO2e."""
        before = {i.ref.name: i for i in allocate_node(NODES["a"], SAMPLES[:4], 1.0)}
        cut = [_sample("a4", "a", 0.25, 0.0, 4, 0) if s.ref.name == "a4" else s for s in SAMPLES[:4]]
        after = {i.ref.name: i for i in allocate_node(NODES["a"], cut, 1.0)}

        a4_before = before["a4"].co2e_operational_g + before["a4"].co2e_embodied_g
        a4_after = after["a4"].co2e_operational_g + after["a4"].co2e_embodied_g
        assert a4_after < a4_before
        assert after["a4"].cost_allocated.cpu < before["a4"].cost_allocated.cpu
        # Usage did not change, so neither did the dynamic energy of the other pods.
        assert after["a1"].energy_kwh.dynamic_cpu == pytest.approx(before["a1"].energy_kwh.dynamic_cpu)

    def test_samples_on_unknown_nodes_are_reported_not_dropped(self):
        orphan = _sample("x", "missing", 1, 1, 1, 1)
        result = ReservationAwareImpactModel().allocate(ClusterSnapshot("c1", 1.0, NODES, [*SAMPLES, orphan]))
        assert result.unallocated == [orphan]

    def test_idle_rows_are_flagged(self):
        result = ReservationAwareImpactModel().allocate(ClusterSnapshot("c1", 1.0, NODES, SAMPLES))
        idle = [impact for impact in result.impacts if impact.scope == "idle"]
        assert len(idle) == 3
        assert all(impact.ref.name == IDLE_WORKLOAD_NAME for impact in idle)
        assert all(impact.model_version == IMPACT_MODEL_VERSION for impact in result.impacts)


class TestConservation:
    @pytest.mark.parametrize("seed", range(25))
    def test_allocated_plus_idle_equals_node_totals(self, seed):
        rng = random.Random(seed)
        node = _node(
            "n",
            rng.choice([2, 4, 8, 16]),
            rng.choice([4, 16, 64]),
            rng.uniform(5, 100),
            rng.uniform(100, 400),
            rng.uniform(1, 50),
            rng.uniform(0.01, 0.1),
            rng.uniform(0.001, 0.02),
            rng.uniform(1.0, 1.6),
            rng.uniform(10, 800),
        )
        hours = rng.choice([1 / 12, 1.0, 24.0])
        samples = [
            _sample(
                f"p{index}",
                "n",
                rng.choice([0.0, rng.uniform(0, 4)]),
                rng.choice([None, rng.uniform(0, 4)]),
                rng.choice([0.0, rng.uniform(0, 16)]),
                rng.choice([None, rng.uniform(0, 16)]),
                hours=hours * rng.choice([0.25, 0.5, 1.0]),
            )
            for index in range(rng.randint(0, 12))
        ]

        impacts = allocate_node(node, samples, hours)
        totals = node_totals(node, samples, hours)

        assert sum(i.cost_allocated.total for i in impacts) == pytest.approx(totals.cost.total, rel=1e-9)
        assert sum(i.energy_kwh.total for i in impacts) == pytest.approx(totals.energy_kwh, rel=1e-9)
        assert sum(i.co2e_operational_g for i in impacts) == pytest.approx(totals.co2e_operational_g, rel=1e-9)
        assert sum(i.co2e_embodied_g for i in impacts) == pytest.approx(totals.co2e_embodied_g, rel=1e-9)
        assert all(i.cost_allocated.total >= 0 for i in impacts)


class TestReservationShares:
    def test_unreserved_capacity_is_idle(self):
        shares, idle = reservation_shares([1, 1], 4)
        assert shares == [0.25, 0.25]
        assert idle == pytest.approx(0.5)

    def test_overcommit_normalizes_by_total_reservation(self):
        shares, idle = reservation_shares([3, 1], 2)
        assert shares == [0.75, 0.25]
        assert idle == 0.0

    def test_empty_node_is_fully_idle(self):
        assert reservation_shares([], 0) == ([], 1.0)
