# src/greenkube/core/sustainability_score.py
"""
Comprehensive Sustainability Score engine for GreenKube.

Computes a composite score from 0 to 100 (100 = best) by aggregating
seven independent dimensions that together capture the full picture of a
cluster's environmental and operational efficiency.

See docs/sustainability-score.md for the full methodology.
"""

import logging
import math
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from greenkube.core.config import Config, get_config
from greenkube.models.metrics import CombinedMetric

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dimension weights — fixed so the score is comparable across clusters.
# ---------------------------------------------------------------------------
DIMENSION_WEIGHTS: Dict[str, float] = {
    "resource_efficiency": 0.25,
    "carbon_efficiency": 0.20,
    "waste_elimination": 0.15,
    "node_efficiency": 0.15,
    "scaling_practices": 0.10,
    "carbon_aware_scheduling": 0.10,
    "stability": 0.05,
}

# Neutral fallback when a dimension has no data.
_NEUTRAL = 50.0

# Effective carbon intensity ceiling for score mapping (gCO2e/kWh of compute).
# Represents grid_intensity × PUE; 800 = worst-case dirty grid (PUE≈1.0).
# A PUE of 2.0 with a 400 gCO2/kWh grid yields the same effective cost as
# a PUE of 1.0 with an 800 gCO2/kWh grid.
_CARBON_EFFICIENCY_CEILING = 800.0

# Stability: each average restart costs this many points.
_RESTART_PENALTY_PER_UNIT = 10.0


class SustainabilityResult(BaseModel):
    """Result of a sustainability score computation."""

    overall_score: float = Field(..., ge=0, le=100, description="Composite score 0–100.")
    dimension_scores: Dict[str, float] = Field(
        default_factory=dict,
        description="Per-dimension scores (0–100).",
    )


class SustainabilityScorer:
    """Computes the 7-dimension sustainability score from raw metrics."""

    def __init__(self, config: Optional[Config] = None):
        """Initializes the scorer with thresholds from config.

        Args:
            config: Optional Config instance. Falls back to the module-level singleton.
        """
        cfg = config if config is not None else get_config()
        self.zombie_cost_threshold = cfg.ZOMBIE_COST_THRESHOLD
        self.zombie_energy_threshold = cfg.ZOMBIE_ENERGY_THRESHOLD
        self.idle_namespace_energy_threshold = cfg.IDLE_NAMESPACE_ENERGY_THRESHOLD
        self.carbon_aware_threshold = cfg.CARBON_AWARE_THRESHOLD
        self.node_utilization_threshold = cfg.NODE_UTILIZATION_THRESHOLD
        self.autoscaling_cv_threshold = cfg.AUTOSCALING_CV_THRESHOLD
        self.autoscaling_spike_ratio = cfg.AUTOSCALING_SPIKE_RATIO
        self.off_peak_idle_threshold = cfg.OFF_PEAK_IDLE_THRESHOLD
        self.off_peak_min_idle_hours = cfg.OFF_PEAK_MIN_IDLE_HOURS
        self.recommend_system_namespaces = cfg.RECOMMEND_SYSTEM_NAMESPACES

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute(
        self,
        metrics: List[CombinedMetric],
        node_infos: Optional[List] = None,
        hpa_targets: Optional[Set[Tuple[str, str, str]]] = None,
    ) -> SustainabilityResult:
        """Compute the sustainability score from the current metrics snapshot.

        Args:
            metrics: Combined metrics for all pods in the cluster.
            node_infos: Optional list of NodeInfo objects for node efficiency.
            hpa_targets: Optional set of (ns, kind, name) already governed by HPA.

        Returns:
            A SustainabilityResult with overall and per-dimension scores.
        """
        if not metrics:
            dim_scores = {dim: _NEUTRAL for dim in DIMENSION_WEIGHTS}
            return SustainabilityResult(overall_score=_NEUTRAL, dimension_scores=dim_scores)

        dim_scores: Dict[str, float] = {
            "resource_efficiency": self._score_resource_efficiency(metrics),
            "carbon_efficiency": self._score_carbon_efficiency(metrics),
            "waste_elimination": self._score_waste_elimination(metrics),
            "node_efficiency": self._score_node_efficiency(metrics, node_infos),
            "scaling_practices": self._score_scaling_practices(metrics, hpa_targets),
            "carbon_aware_scheduling": self._score_carbon_aware(metrics),
            "stability": self._score_stability(metrics),
        }

        overall = sum(dim_scores[dim] * DIMENSION_WEIGHTS[dim] for dim in DIMENSION_WEIGHTS)
        overall = max(0.0, min(100.0, round(overall, 1)))

        return SustainabilityResult(overall_score=overall, dimension_scores=dim_scores)

    # ------------------------------------------------------------------
    # Dimension 1: Resource Efficiency (25%)
    # ------------------------------------------------------------------

    @staticmethod
    def _score_resource_efficiency(metrics: List[CombinedMetric]) -> float:
        """Energy-weighted average of CPU+memory utilization ratios × 100."""
        total_weighted = 0.0
        total_energy = 0.0

        for m in metrics:
            cpu_req = m.cpu_request or 0
            mem_req = m.memory_request or 0
            cpu_use = m.cpu_usage_millicores
            mem_use = m.memory_usage_bytes

            if cpu_req == 0 and mem_req == 0:
                continue
            if cpu_use is None and mem_use is None:
                continue

            ratios = []
            if cpu_req > 0 and cpu_use is not None:
                ratios.append(min(cpu_use / cpu_req, 1.0))
            if mem_req > 0 and mem_use is not None:
                ratios.append(min(mem_use / mem_req, 1.0))

            if not ratios:
                continue

            pod_eff = sum(ratios) / len(ratios)
            weight = max(m.joules, 0.0) if m.joules > 0 else 1.0
            total_weighted += pod_eff * weight
            total_energy += weight

        if total_energy == 0:
            return _NEUTRAL

        return round(total_weighted / total_energy * 100.0, 1)

    # ------------------------------------------------------------------
    # Dimension 2: Carbon Efficiency (20%)
    # ------------------------------------------------------------------

    @staticmethod
    def _score_carbon_efficiency(metrics: List[CombinedMetric]) -> float:
        """Energy-weighted effective carbon cost (grid_intensity × PUE) mapped to 0–100.

        Answers: "how much CO₂ do you actually emit per kWh of compute, compared to
        the ideal setup (PUE=1.0, zero-carbon grid = 0 gCO₂e/kWh)?"

        effective_intensity = grid_intensity × pue
        Score = max(0, 100 × (1 − effective_intensity / 800))

        A clean renewable grid at PUE=1.0 → score 100.
        A coal grid at 800 gCO₂/kWh with PUE=1.0 → score 0.
        A clean grid at 200 gCO₂/kWh but PUE=2.0 → effective 400 gCO₂/kWh → same
        score as a 400 gCO₂/kWh grid at PUE=1.0 (~50).
        """
        total_weighted = 0.0
        total_energy = 0.0

        for m in metrics:
            if m.joules <= 0:
                continue
            pue = m.pue if m.pue and m.pue >= 1.0 else 1.0
            effective_intensity = m.grid_intensity * pue
            if effective_intensity > 0:
                total_weighted += effective_intensity * m.joules
                total_energy += m.joules
            elif m.grid_intensity == 0:
                # Zero-carbon grid: track energy for denominator but add 0 intensity
                total_energy += m.joules

        if total_energy == 0:
            return _NEUTRAL

        weighted_avg = total_weighted / total_energy if total_energy > 0 else 0.0
        score = max(0.0, 100.0 * (1.0 - weighted_avg / _CARBON_EFFICIENCY_CEILING))
        return round(score, 1)

    # ------------------------------------------------------------------
    # Dimension 3: Waste Elimination (15%)
    # ------------------------------------------------------------------

    _SYSTEM_NAMESPACES = frozenset(
        {
            "kube-system",
            "kube-public",
            "kube-node-lease",
            "coredns",
            "istio-system",
            "kubernetes-dashboard",
        }
    )

    def _score_waste_elimination(self, metrics: List[CombinedMetric]) -> float:
        """Penalizes zombie pods and idle namespaces."""
        # --- Zombie detection ---
        pod_agg: Dict[Tuple[str, str], Dict[str, float]] = defaultdict(lambda: {"total_cost": 0.0, "joules": 0.0})
        for m in metrics:
            key = (m.namespace, m.pod_name)
            pod_agg[key]["total_cost"] += m.total_cost
            pod_agg[key]["joules"] += m.joules

        total_pods = len(pod_agg)
        if total_pods == 0:
            return 100.0

        zombie_count = sum(
            1
            for agg in pod_agg.values()
            if agg["total_cost"] > self.zombie_cost_threshold and agg["joules"] < self.zombie_energy_threshold
        )
        zombie_ratio = zombie_count / total_pods

        # --- Idle namespace detection ---
        ns_energy: Dict[str, float] = defaultdict(float)
        ns_cost: Dict[str, float] = defaultdict(float)
        for m in metrics:
            ns_energy[m.namespace] += m.joules
            ns_cost[m.namespace] += m.total_cost

        total_ns = len(ns_energy)
        idle_ns_count = 0
        for ns, energy in ns_energy.items():
            if not self.recommend_system_namespaces and ns in self._SYSTEM_NAMESPACES:
                continue
            if energy < self.idle_namespace_energy_threshold and ns_cost[ns] > 0:
                idle_ns_count += 1

        idle_ns_ratio = idle_ns_count / total_ns if total_ns > 0 else 0.0

        score = ((1.0 - zombie_ratio) * 0.7 + (1.0 - idle_ns_ratio) * 0.3) * 100.0
        return round(max(0.0, min(100.0, score)), 1)

    # ------------------------------------------------------------------
    # Dimension 4: Node Efficiency (15%)
    # ------------------------------------------------------------------

    @staticmethod
    def _score_node_efficiency(
        metrics: List[CombinedMetric],
        node_infos: Optional[List] = None,
    ) -> float:
        """Average node utilization mapped to a 0–100 score."""
        if not node_infos:
            return _NEUTRAL

        node_capacity: Dict[str, float] = {}
        for ni in node_infos:
            if hasattr(ni, "name") and hasattr(ni, "cpu_capacity_cores"):
                cap = ni.cpu_capacity_cores or 0
                if cap > 0:
                    node_capacity[ni.name] = cap

        if not node_capacity:
            return _NEUTRAL

        # Group pod CPU usage per node per timestamp, so we can sum across pods
        node_usage_by_ts: Dict[str, Dict] = defaultdict(lambda: defaultdict(float))
        for m in metrics:
            if m.node and m.cpu_usage_millicores is not None:
                ts_key = m.timestamp if m.timestamp is not None else 0
                node_usage_by_ts[m.node][ts_key] += m.cpu_usage_millicores

        scores = []
        for node_name, cap_cores in node_capacity.items():
            ts_totals = list(node_usage_by_ts.get(node_name, {}).values())
            if not ts_totals:
                scores.append(0.0)
                continue

            cap_milli = cap_cores * 1000
            avg_usage = sum(ts_totals) / len(ts_totals)
            utilization = avg_usage / cap_milli
            # Score: linear 0–100 up to 70% utilization, then stays at 100
            node_score = min(utilization / 0.7, 1.0) * 100.0
            scores.append(node_score)

        if not scores:
            return _NEUTRAL

        return round(sum(scores) / len(scores), 1)

    # ------------------------------------------------------------------
    # Dimension 5: Scaling Practices (10%)
    # ------------------------------------------------------------------

    def _score_scaling_practices(
        self,
        metrics: List[CombinedMetric],
        hpa_targets: Optional[Set[Tuple[str, str, str]]] = None,
    ) -> float:
        """Penalizes pods that should have autoscaling or off-peak scheduling."""
        pod_series = self._group_by_pod(metrics)

        if not pod_series:
            return _NEUTRAL

        # Count pods with enough data
        pods_with_data = 0
        autoscale_candidates = 0
        offpeak_candidates = 0

        for (ns, pod), series in pod_series.items():
            usages = [m.cpu_usage_millicores for m in series if m.cpu_usage_millicores is not None]
            if len(usages) < 3:
                continue

            cpu_request = max((m.cpu_request or 0) for m in series)
            if cpu_request == 0:
                continue

            pods_with_data += 1

            # Autoscaling candidate check
            mean_usage = sum(usages) / len(usages)
            if mean_usage > 0:
                variance = sum((u - mean_usage) ** 2 for u in usages) / len(usages)
                stddev = math.sqrt(variance)
                cv = stddev / mean_usage
                max_usage = max(usages)
                spike_ratio = max_usage / mean_usage

                if cv > self.autoscaling_cv_threshold and spike_ratio > self.autoscaling_spike_ratio:
                    # Check HPA
                    skip = False
                    if hpa_targets:
                        owner_kinds = {m.owner_kind for m in series if m.owner_kind}
                        owner_names = {m.owner_name for m in series if m.owner_name}
                        if owner_kinds and owner_names:
                            ok = next(iter(owner_kinds))
                            on = next(iter(owner_names))
                            if (ns, ok, on) in hpa_targets:
                                skip = True
                    if not skip:
                        autoscale_candidates += 1

            # Off-peak candidate check
            timed = [
                (m.timestamp, m.cpu_usage_millicores)
                for m in series
                if m.timestamp is not None and m.cpu_usage_millicores is not None
            ]
            if len(timed) >= 6:
                hourly_usage: Dict[int, List[float]] = defaultdict(list)
                for ts, usage in timed:
                    hourly_usage[ts.hour].append(float(usage))

                if hourly_usage:
                    hourly_avg = {h: sum(v) / len(v) for h, v in hourly_usage.items()}
                    peak = max(hourly_avg.values()) if hourly_avg else 0
                    if peak > 0:
                        idle_threshold = peak * self.off_peak_idle_threshold
                        idle_hours = [h for h, avg in hourly_avg.items() if avg < idle_threshold]
                        consecutive = self._find_longest_consecutive_hours(idle_hours)
                        if len(consecutive) >= self.off_peak_min_idle_hours:
                            offpeak_candidates += 1

        if pods_with_data == 0:
            return _NEUTRAL

        auto_penalty = autoscale_candidates / pods_with_data
        offpeak_penalty = offpeak_candidates / pods_with_data
        score = (1.0 - (auto_penalty * 0.6 + offpeak_penalty * 0.4)) * 100.0
        return round(max(0.0, min(100.0, score)), 1)

    # ------------------------------------------------------------------
    # Dimension 6: Carbon-Aware Scheduling (10%)
    # ------------------------------------------------------------------

    def _score_carbon_aware(self, metrics: List[CombinedMetric]) -> float:
        """Penalizes pods running during high-carbon-intensity windows."""
        zone_intensities: Dict[str, List[float]] = defaultdict(list)
        for m in metrics:
            if m.emaps_zone and m.grid_intensity and m.grid_intensity > 0:
                zone_intensities[m.emaps_zone].append(m.grid_intensity)

        if not zone_intensities:
            return _NEUTRAL

        zone_avg: Dict[str, float] = {zone: sum(vals) / len(vals) for zone, vals in zone_intensities.items()}

        pod_agg: Dict[Tuple[str, str], Dict] = defaultdict(lambda: {"intensities": [], "zone": None})
        for m in metrics:
            key = (m.namespace, m.pod_name)
            if m.grid_intensity and m.grid_intensity > 0:
                pod_agg[key]["intensities"].append(m.grid_intensity)
            if m.emaps_zone:
                pod_agg[key]["zone"] = m.emaps_zone

        total_pods_with_zone = 0
        high_intensity_pods = 0

        for (ns, pod), agg in pod_agg.items():
            zone = agg.get("zone")
            if not zone or zone not in zone_avg:
                continue
            intensities = agg["intensities"]
            if not intensities:
                continue

            total_pods_with_zone += 1
            pod_avg = sum(intensities) / len(intensities)
            z_avg = zone_avg[zone]

            if z_avg > 0 and pod_avg / z_avg > self.carbon_aware_threshold:
                high_intensity_pods += 1

        if total_pods_with_zone == 0:
            return _NEUTRAL

        ratio = high_intensity_pods / total_pods_with_zone
        score = (1.0 - ratio) * 100.0
        return round(max(0.0, min(100.0, score)), 1)

    # ------------------------------------------------------------------
    # Dimension 7: Stability (5%)
    # ------------------------------------------------------------------

    @staticmethod
    def _score_stability(metrics: List[CombinedMetric]) -> float:
        """Penalizes pods with high restart counts."""
        restart_values = [m.restart_count for m in metrics if m.restart_count is not None]

        if not restart_values:
            return _NEUTRAL

        avg_restarts = sum(restart_values) / len(restart_values)
        score = max(0.0, 100.0 - avg_restarts * _RESTART_PENALTY_PER_UNIT)
        return round(score, 1)

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _group_by_pod(
        metrics: List[CombinedMetric],
    ) -> Dict[Tuple[str, str], List[CombinedMetric]]:
        """Groups metrics by (namespace, pod_name)."""
        groups: Dict[Tuple[str, str], List[CombinedMetric]] = defaultdict(list)
        for m in metrics:
            groups[(m.namespace, m.pod_name)].append(m)
        return groups

    @staticmethod
    def _find_longest_consecutive_hours(hours: List[int]) -> List[int]:
        """Finds the longest run of consecutive hours (wrapping around midnight)."""
        if not hours:
            return []

        hours_set = set(hours)
        best: List[int] = []

        for start in hours:
            run: List[int] = []
            h = start
            while h in hours_set:
                run.append(h)
                h = (h + 1) % 24
                if h not in hours_set or h == start:
                    break
            if len(run) > len(best):
                best = run

        return best
