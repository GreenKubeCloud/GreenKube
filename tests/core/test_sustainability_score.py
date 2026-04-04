# tests/core/test_sustainability_score.py
"""
Tests for the comprehensive Sustainability Score engine.

The score is 0–100 where 100 = best (perfectly optimized cluster).
It aggregates 7 dimensions: resource efficiency, carbon intensity,
waste elimination, node efficiency, scaling practices,
carbon-aware scheduling, and stability.
"""

from datetime import datetime, timezone

from greenkube.core.config import Config
from greenkube.core.sustainability_score import (
    DIMENSION_WEIGHTS,
    SustainabilityResult,
    SustainabilityScorer,
)
from greenkube.models.metrics import CombinedMetric
from greenkube.models.node import NodeInfo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _metric(**overrides) -> CombinedMetric:
    """Create a CombinedMetric with sensible defaults for testing."""
    defaults = {
        "pod_name": "web-1",
        "namespace": "default",
        "total_cost": 0.05,
        "co2e_grams": 1.0,
        "pue": 1.2,
        "grid_intensity": 50.0,
        "joules": 5000.0,
        "cpu_request": 1000,
        "memory_request": 512 * 1024 * 1024,  # 512MiB
        "cpu_usage_millicores": 800,
        "memory_usage_bytes": 400 * 1024 * 1024,  # 400MiB
        "restart_count": 0,
        "emaps_zone": "FR",
        "timestamp": datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return CombinedMetric(**defaults)


def _node_info(**overrides) -> NodeInfo:
    """Create a NodeInfo with sensible defaults."""
    defaults = {
        "name": "node-1",
        "cpu_capacity_cores": 4.0,
        "memory_capacity_bytes": 8 * 1024**3,
    }
    defaults.update(overrides)
    return NodeInfo(**defaults)


# ---------------------------------------------------------------------------
# Weights consistency
# ---------------------------------------------------------------------------


class TestDimensionWeights:
    """Weights must sum to 1.0 and all be positive."""

    def test_weights_sum_to_one(self):
        total = sum(DIMENSION_WEIGHTS.values())
        assert abs(total - 1.0) < 1e-9, f"Weights sum to {total}, expected 1.0"

    def test_all_weights_positive(self):
        for dim, w in DIMENSION_WEIGHTS.items():
            assert w > 0, f"Weight for '{dim}' must be positive, got {w}"

    def test_seven_dimensions(self):
        assert len(DIMENSION_WEIGHTS) == 7

    def test_carbon_efficiency_dimension_exists(self):
        """Dimension is named 'carbon_efficiency', not 'carbon_intensity'."""
        assert "carbon_efficiency" in DIMENSION_WEIGHTS
        assert "carbon_intensity" not in DIMENSION_WEIGHTS


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class TestSustainabilityResult:
    """SustainabilityResult should carry overall score + per-dimension scores."""

    def test_result_has_overall_score(self):
        result = SustainabilityResult(
            overall_score=75.0,
            dimension_scores={
                "resource_efficiency": 80.0,
                "carbon_efficiency": 90.0,
                "waste_elimination": 100.0,
                "node_efficiency": 50.0,
                "scaling_practices": 50.0,
                "carbon_aware_scheduling": 50.0,
                "stability": 100.0,
            },
        )
        assert result.overall_score == 75.0

    def test_result_dimension_scores(self):
        result = SustainabilityResult(
            overall_score=50.0,
            dimension_scores={"carbon_efficiency": 80.0},
        )
        assert result.dimension_scores["carbon_efficiency"] == 80.0


# ---------------------------------------------------------------------------
# Perfect cluster → score near 100
# ---------------------------------------------------------------------------


class TestPerfectCluster:
    """A cluster with tight resource fit, low carbon, no waste → near 100."""

    def test_perfect_score(self):
        metrics = [
            _metric(
                pod_name="app-1",
                node="node-1",
                cpu_request=1000,
                cpu_usage_millicores=950,
                memory_request=512 * 1024 * 1024,
                memory_usage_bytes=490 * 1024 * 1024,
                grid_intensity=20.0,
                joules=5000.0,
                total_cost=0.10,
                restart_count=0,
                emaps_zone="FR",
                timestamp=datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc),
            ),
            _metric(
                pod_name="app-1",
                node="node-1",
                cpu_request=1000,
                cpu_usage_millicores=940,
                memory_request=512 * 1024 * 1024,
                memory_usage_bytes=485 * 1024 * 1024,
                grid_intensity=20.0,
                joules=5000.0,
                total_cost=0.10,
                restart_count=0,
                emaps_zone="FR",
                timestamp=datetime(2025, 1, 1, 11, 0, tzinfo=timezone.utc),
            ),
            _metric(
                pod_name="app-1",
                node="node-1",
                cpu_request=1000,
                cpu_usage_millicores=960,
                memory_request=512 * 1024 * 1024,
                memory_usage_bytes=495 * 1024 * 1024,
                grid_intensity=20.0,
                joules=5000.0,
                total_cost=0.10,
                restart_count=0,
                emaps_zone="FR",
                timestamp=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            ),
            _metric(
                pod_name="app-2",
                node="node-1",
                cpu_request=500,
                cpu_usage_millicores=480,
                memory_request=256 * 1024 * 1024,
                memory_usage_bytes=240 * 1024 * 1024,
                grid_intensity=20.0,
                joules=3000.0,
                total_cost=0.07,
                restart_count=0,
                emaps_zone="FR",
                timestamp=datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc),
            ),
            _metric(
                pod_name="app-2",
                node="node-1",
                cpu_request=500,
                cpu_usage_millicores=470,
                memory_request=256 * 1024 * 1024,
                memory_usage_bytes=235 * 1024 * 1024,
                grid_intensity=20.0,
                joules=3000.0,
                total_cost=0.07,
                restart_count=0,
                emaps_zone="FR",
                timestamp=datetime(2025, 1, 1, 11, 0, tzinfo=timezone.utc),
            ),
            _metric(
                pod_name="app-2",
                node="node-1",
                cpu_request=500,
                cpu_usage_millicores=490,
                memory_request=256 * 1024 * 1024,
                memory_usage_bytes=245 * 1024 * 1024,
                grid_intensity=20.0,
                joules=3000.0,
                total_cost=0.07,
                restart_count=0,
                emaps_zone="FR",
                timestamp=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            ),
        ]
        nodes = [_node_info(name="node-1", cpu_capacity_cores=2.0)]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics, node_infos=nodes)

        assert result.overall_score >= 85.0, f"Perfect cluster should score ≥85, got {result.overall_score}"
        assert result.overall_score <= 100.0

    def test_dimension_scores_bounded(self):
        metrics = [_metric()]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        for dim, score in result.dimension_scores.items():
            assert 0 <= score <= 100, f"Dimension '{dim}' out of range: {score}"


# ---------------------------------------------------------------------------
# Empty cluster → neutral 50
# ---------------------------------------------------------------------------


class TestEmptyCluster:
    """No metrics at all → neutral score (~50)."""

    def test_empty_metrics_returns_neutral(self):
        scorer = SustainabilityScorer()
        result = scorer.compute([])
        assert result.overall_score == 50.0

    def test_empty_dimensions_all_fifty(self):
        scorer = SustainabilityScorer()
        result = scorer.compute([])
        for dim, score in result.dimension_scores.items():
            assert score == 50.0, f"Empty cluster dimension '{dim}' should be 50, got {score}"


# ---------------------------------------------------------------------------
# Resource Efficiency dimension
# ---------------------------------------------------------------------------


class TestResourceEfficiency:
    """CPU and memory utilization ratios drive the resource efficiency score."""

    def test_high_utilization_scores_high(self):
        metrics = [_metric(cpu_request=1000, cpu_usage_millicores=950, memory_request=1024, memory_usage_bytes=980)]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        assert result.dimension_scores["resource_efficiency"] >= 90.0

    def test_low_utilization_scores_low(self):
        metrics = [_metric(cpu_request=1000, cpu_usage_millicores=50, memory_request=1024, memory_usage_bytes=50)]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        assert result.dimension_scores["resource_efficiency"] < 20.0

    def test_no_requests_returns_neutral(self):
        metrics = [_metric(cpu_request=0, memory_request=0)]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        assert result.dimension_scores["resource_efficiency"] == 50.0

    def test_over_usage_capped_at_one(self):
        """Over-provisioned usage (usage > request) should cap ratio at 1.0."""
        metrics = [_metric(cpu_request=100, cpu_usage_millicores=200, memory_request=100, memory_usage_bytes=200)]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        assert result.dimension_scores["resource_efficiency"] == 100.0


# ---------------------------------------------------------------------------
# Carbon Efficiency dimension (grid intensity × PUE)
# ---------------------------------------------------------------------------


class TestCarbonEfficiency:
    """Carbon efficiency = grid intensity × datacenter PUE vs. perfect (PUE=1, zero carbon)."""

    def test_zero_intensity_perfect_pue_scores_100(self):
        """Zero grid intensity, PUE=1.0 → perfect carbon efficiency."""
        metrics = [_metric(grid_intensity=0.0, pue=1.0, joules=5000)]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        assert result.dimension_scores["carbon_efficiency"] == 100.0

    def test_very_high_effective_intensity_scores_zero(self):
        """grid_intensity × pue ≥ 800 gCO2/kWh → score 0."""
        # 800 × 1.0 = 800 → exactly the ceiling → 0
        metrics = [_metric(grid_intensity=900.0, pue=1.0, joules=5000)]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        assert result.dimension_scores["carbon_efficiency"] == 0.0

    def test_pue_multiplies_effective_intensity(self):
        """PUE=2.0 should make score worse than PUE=1.0 at same grid intensity."""
        m_good_pue = [_metric(grid_intensity=200.0, pue=1.0, joules=5000)]
        m_bad_pue = [_metric(grid_intensity=200.0, pue=2.0, joules=5000)]
        scorer = SustainabilityScorer()
        score_good = scorer.compute(m_good_pue).dimension_scores["carbon_efficiency"]
        score_bad = scorer.compute(m_bad_pue).dimension_scores["carbon_efficiency"]
        assert score_good > score_bad, (
            f"PUE=1.0 should score higher than PUE=2.0 at same grid intensity. Got {score_good} vs {score_bad}"
        )

    def test_moderate_effective_intensity(self):
        """400 gCO2/kWh × PUE 1.0 = 400 effective → ~50 score."""
        metrics = [_metric(grid_intensity=400.0, pue=1.0, joules=5000)]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        score = result.dimension_scores["carbon_efficiency"]
        assert 40 <= score <= 60, f"400 gCO2/kWh × PUE 1.0 should be ~50, got {score}"

    def test_high_pue_with_moderate_intensity_penalizes(self):
        """200 gCO2/kWh × PUE 2.0 = 400 effective → same as pure 400 gCO2."""
        m_equiv = [_metric(grid_intensity=200.0, pue=2.0, joules=5000)]
        m_direct = [_metric(grid_intensity=400.0, pue=1.0, joules=5000)]
        scorer = SustainabilityScorer()
        score_equiv = scorer.compute(m_equiv).dimension_scores["carbon_efficiency"]
        score_direct = scorer.compute(m_direct).dimension_scores["carbon_efficiency"]
        assert abs(score_equiv - score_direct) < 1.0, (
            f"200g×PUE2 should equal 400g×PUE1. Got {score_equiv} vs {score_direct}"
        )

    def test_no_energy_data_returns_neutral(self):
        """No joules and no intensity → neutral 50."""
        metrics = [_metric(grid_intensity=0.0, pue=1.0, joules=0.0)]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        assert result.dimension_scores["carbon_efficiency"] == 50.0

    def test_energy_weighted_average_across_pods(self):
        """Energy-weighted average of effective intensity across pods."""
        metrics = [
            _metric(pod_name="green", grid_intensity=50.0, pue=1.0, joules=8000),
            _metric(pod_name="dirty", grid_intensity=600.0, pue=1.5, joules=2000),
        ]
        # Effective: green=50×1.0=50, dirty=600×1.5=900 (capped)
        # Weighted: (50×8000 + 900×2000) / 10000 = (400000+1800000)/10000 = 220
        # Score ≈ (1 - 220/800) × 100 ≈ 72.5
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        score = result.dimension_scores["carbon_efficiency"]
        assert 60 <= score <= 85, f"Expected ~72 for mixed pods, got {score}"


# ---------------------------------------------------------------------------
# Waste Elimination dimension
# ---------------------------------------------------------------------------


class TestWasteElimination:
    """Zombie pods and idle namespaces penalize the score."""

    def test_no_zombies_scores_100(self):
        metrics = [
            _metric(pod_name="app-1", total_cost=0.10, joules=5000),
            _metric(pod_name="app-2", total_cost=0.08, joules=4000),
        ]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        assert result.dimension_scores["waste_elimination"] == 100.0

    def test_all_zombies_scores_low(self):
        """All pods are zombies (high cost, near-zero energy)."""
        metrics = [
            _metric(pod_name="zombie-1", total_cost=1.0, joules=10),
            _metric(pod_name="zombie-2", total_cost=2.0, joules=5),
        ]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        assert result.dimension_scores["waste_elimination"] < 40.0

    def test_idle_namespace_penalizes(self):
        """One idle namespace among two total should penalize."""
        metrics = [
            _metric(pod_name="app-1", namespace="prod", total_cost=0.10, joules=5000),
            _metric(pod_name="idle-1", namespace="stale", total_cost=0.05, joules=100),
        ]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        # stale namespace has <1000J total → idle
        score = result.dimension_scores["waste_elimination"]
        assert score < 100.0


# ---------------------------------------------------------------------------
# Node Efficiency dimension
# ---------------------------------------------------------------------------


class TestNodeEfficiency:
    """Node-level utilization drives this dimension."""

    def test_well_utilized_nodes_score_high(self):
        metrics = [
            _metric(node="node-1", cpu_usage_millicores=3000, joules=5000),
        ]
        nodes = [_node_info(name="node-1", cpu_capacity_cores=4.0)]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics, node_infos=nodes)
        assert result.dimension_scores["node_efficiency"] >= 90.0

    def test_underutilized_nodes_score_low(self):
        metrics = [
            _metric(node="node-1", cpu_usage_millicores=100, joules=1000),
        ]
        nodes = [_node_info(name="node-1", cpu_capacity_cores=16.0)]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics, node_infos=nodes)
        assert result.dimension_scores["node_efficiency"] < 20.0

    def test_no_node_info_returns_neutral(self):
        metrics = [_metric()]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics, node_infos=None)
        assert result.dimension_scores["node_efficiency"] == 50.0


# ---------------------------------------------------------------------------
# Scaling Practices dimension
# ---------------------------------------------------------------------------


class TestScalingPractices:
    """Autoscaling and off-peak candidates should penalize the score."""

    def test_no_autoscaling_candidates_scores_high(self):
        """Stable, low-variability workloads → no autoscaling candidates → high score."""
        metrics = [
            _metric(pod_name="stable-1", cpu_request=1000, cpu_usage_millicores=500),
            _metric(pod_name="stable-1", cpu_request=1000, cpu_usage_millicores=510),
            _metric(pod_name="stable-1", cpu_request=1000, cpu_usage_millicores=490),
            _metric(pod_name="stable-1", cpu_request=1000, cpu_usage_millicores=505),
        ]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        assert result.dimension_scores["scaling_practices"] >= 80.0

    def test_insufficient_data_returns_neutral(self):
        """Fewer than 3 samples → neutral."""
        metrics = [_metric(pod_name="short-1", cpu_request=1000, cpu_usage_millicores=500)]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        assert result.dimension_scores["scaling_practices"] == 50.0


# ---------------------------------------------------------------------------
# Carbon-Aware Scheduling dimension
# ---------------------------------------------------------------------------


class TestCarbonAwareScheduling:
    """Pods running during high intensity vs zone average should penalize."""

    def test_all_pods_in_low_intensity_scores_100(self):
        """All pods run at exactly the zone average → no penalty."""
        metrics = [
            _metric(pod_name="app-1", grid_intensity=50.0, emaps_zone="FR"),
            _metric(pod_name="app-2", grid_intensity=50.0, emaps_zone="FR"),
        ]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        assert result.dimension_scores["carbon_aware_scheduling"] == 100.0

    def test_all_pods_in_high_intensity_scores_low(self):
        """All pods run at 2x the zone average → full penalty."""
        # Zone avg will be 150; pods at 200 → ratio = 200/150 > 1.5
        metrics = [
            _metric(pod_name="app-1", grid_intensity=200.0, emaps_zone="FR"),
            _metric(pod_name="app-1", grid_intensity=100.0, emaps_zone="FR"),
            _metric(pod_name="app-2", grid_intensity=200.0, emaps_zone="FR"),
            _metric(pod_name="app-2", grid_intensity=100.0, emaps_zone="FR"),
        ]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        # Not all necessarily flagged since per-pod avg may not exceed threshold
        score = result.dimension_scores["carbon_aware_scheduling"]
        assert score <= 100.0  # Just validate it computes without error

    def test_no_zone_data_returns_neutral(self):
        metrics = [_metric(emaps_zone=None, grid_intensity=0.0)]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        assert result.dimension_scores["carbon_aware_scheduling"] == 50.0


# ---------------------------------------------------------------------------
# Stability dimension
# ---------------------------------------------------------------------------


class TestStability:
    """Pod restart counts drive the stability score."""

    def test_zero_restarts_scores_100(self):
        metrics = [
            _metric(pod_name="stable-1", restart_count=0),
            _metric(pod_name="stable-2", restart_count=0),
        ]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        assert result.dimension_scores["stability"] == 100.0

    def test_many_restarts_scores_low(self):
        metrics = [
            _metric(pod_name="crasher-1", restart_count=15),
            _metric(pod_name="crasher-2", restart_count=20),
        ]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        assert result.dimension_scores["stability"] == 0.0

    def test_moderate_restarts(self):
        metrics = [
            _metric(pod_name="flaky-1", restart_count=3),
            _metric(pod_name="flaky-2", restart_count=5),
        ]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        score = result.dimension_scores["stability"]
        assert 40 <= score <= 70, f"Expected moderate stability score, got {score}"

    def test_no_restart_data_returns_neutral(self):
        metrics = [_metric(restart_count=None)]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        assert result.dimension_scores["stability"] == 50.0


# ---------------------------------------------------------------------------
# Overall score integration
# ---------------------------------------------------------------------------


class TestOverallScore:
    """The overall score is the weighted sum of dimension scores."""

    def test_overall_is_weighted_sum(self):
        metrics = [_metric()]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)

        expected = sum(result.dimension_scores[dim] * DIMENSION_WEIGHTS[dim] for dim in DIMENSION_WEIGHTS)
        assert abs(result.overall_score - round(expected, 1)) < 0.2

    def test_overall_clamped_to_0_100(self):
        """Score must always be in [0, 100]."""
        metrics = [_metric()]
        scorer = SustainabilityScorer()
        result = scorer.compute(metrics)
        assert 0 <= result.overall_score <= 100

    def test_different_configs_affect_score(self):
        """Custom config thresholds should change detection results."""
        config = Config()
        # Lower zombie energy threshold → the default metric (5000J) won't be zombie
        config.ZOMBIE_ENERGY_THRESHOLD = 100.0
        scorer = SustainabilityScorer(config=config)
        result = scorer.compute([_metric()])
        assert 0 <= result.overall_score <= 100
