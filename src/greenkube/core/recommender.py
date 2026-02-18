# src/greenkube/core/recommender.py
"""
Enhanced recommendation engine for GreenKube.

Analyzes historical CombinedMetric time-series data to generate
actionable optimization recommendations across 9 categories:
zombie pods, CPU/memory rightsizing, autoscaling candidates,
off-peak scaling, idle namespaces, carbon-aware scheduling,
and node-level optimizations.
"""

import logging
import math
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from greenkube.core.config import config
from greenkube.models.metrics import CombinedMetric, Recommendation, RecommendationType

LOG = logging.getLogger(__name__)


class Recommender:
    """Analyzes combined metrics to generate optimization recommendations."""

    def __init__(self):
        """Initializes the recommender with thresholds from config."""
        self.rightsizing_cpu_threshold = config.RIGHTSIZING_CPU_THRESHOLD
        self.rightsizing_memory_threshold = config.RIGHTSIZING_MEMORY_THRESHOLD
        self.rightsizing_headroom = config.RIGHTSIZING_HEADROOM
        self.zombie_cost_threshold = config.ZOMBIE_COST_THRESHOLD
        self.zombie_energy_threshold = config.ZOMBIE_ENERGY_THRESHOLD
        self.autoscaling_cv_threshold = config.AUTOSCALING_CV_THRESHOLD
        self.autoscaling_spike_ratio = config.AUTOSCALING_SPIKE_RATIO
        self.off_peak_idle_threshold = config.OFF_PEAK_IDLE_THRESHOLD
        self.off_peak_min_idle_hours = config.OFF_PEAK_MIN_IDLE_HOURS
        self.idle_namespace_energy_threshold = config.IDLE_NAMESPACE_ENERGY_THRESHOLD
        self.carbon_aware_threshold = config.CARBON_AWARE_THRESHOLD
        self.node_utilization_threshold = config.NODE_UTILIZATION_THRESHOLD

    def generate_recommendations(
        self,
        metrics: List[CombinedMetric],
        node_infos: Optional[List] = None,
    ) -> List[Recommendation]:
        """Generates all recommendation types from metrics.

        Args:
            metrics: List of CombinedMetric objects to analyze.
            node_infos: Optional list of NodeInfo objects for node-level analysis.

        Returns:
            A deduplicated list of Recommendation objects.
        """
        if not metrics:
            return []

        pod_series = self._group_by_pod(metrics)

        recs: List[Recommendation] = []
        recs.extend(self._analyze_zombies(metrics))
        recs.extend(self._analyze_cpu_rightsizing(pod_series))
        recs.extend(self._analyze_memory_rightsizing(pod_series))
        recs.extend(self._analyze_autoscaling(pod_series))
        recs.extend(self._analyze_off_peak(pod_series))
        recs.extend(self._analyze_idle_namespaces(metrics))
        recs.extend(self._analyze_carbon_aware(metrics))
        recs.extend(self._analyze_nodes(metrics, node_infos))

        return self._deduplicate(recs)

    # ------------------------------------------------------------------
    # Legacy API compatibility
    # ------------------------------------------------------------------

    def generate_zombie_recommendations(self, metrics: List[CombinedMetric]) -> List[Recommendation]:
        """Legacy method for zombie pod detection."""
        return self._analyze_zombies(metrics)

    def generate_rightsizing_recommendations(self, metrics: List[CombinedMetric]) -> List[Recommendation]:
        """Legacy method for CPU rightsizing via energy-based estimation."""
        return self._analyze_cpu_rightsizing_legacy(metrics)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _group_by_pod(metrics: List[CombinedMetric]) -> Dict[Tuple[str, str], List[CombinedMetric]]:
        """Groups metrics by (namespace, pod_name)."""
        groups: Dict[Tuple[str, str], List[CombinedMetric]] = defaultdict(list)
        for m in metrics:
            groups[(m.namespace, m.pod_name)].append(m)
        return groups

    @staticmethod
    def _percentile(values: List[float], p: float) -> float:
        """Computes the p-th percentile of a list of values (0-100 scale)."""
        if not values:
            return 0.0
        sorted_vals = sorted(values)
        k = (len(sorted_vals) - 1) * (p / 100.0)
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return sorted_vals[int(k)]
        return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)

    @staticmethod
    def _deduplicate(recs: List[Recommendation]) -> List[Recommendation]:
        """Removes duplicate recommendations (same pod + same type)."""
        seen = set()
        result = []
        for rec in recs:
            key = (rec.namespace, rec.pod_name, rec.type, rec.target_node or "")
            if key not in seen:
                seen.add(key)
                result.append(rec)
        return result

    # ------------------------------------------------------------------
    # ZOMBIE_POD
    # ------------------------------------------------------------------

    def _analyze_zombies(self, metrics: List[CombinedMetric]) -> List[Recommendation]:
        """Identifies pods with cost but near-zero energy consumption."""
        recs = []
        pod_aggregated: Dict[Tuple[str, str], Dict] = defaultdict(
            lambda: {"total_cost": 0.0, "joules": 0.0, "co2e_grams": 0.0}
        )

        for m in metrics:
            key = (m.namespace, m.pod_name)
            pod_aggregated[key]["total_cost"] += m.total_cost
            pod_aggregated[key]["joules"] += m.joules
            pod_aggregated[key]["co2e_grams"] += m.co2e_grams

        for (ns, pod), agg in pod_aggregated.items():
            if agg["total_cost"] > self.zombie_cost_threshold and agg["joules"] < self.zombie_energy_threshold:
                recs.append(
                    Recommendation(
                        pod_name=pod,
                        namespace=ns,
                        type=RecommendationType.ZOMBIE_POD,
                        description=(
                            f"Pod '{pod}' has cost ${agg['total_cost']:.4f} but consumed only "
                            f"{agg['joules']:.0f} Joules. It may be idle or a zombie."
                        ),
                        reason=(
                            f"Pod cost {agg['total_cost']:.4f} but "
                            f"consumed only {agg['joules']:.1f} Joules. "
                            f"This may be an idle or 'zombie' pod."
                        ),
                        priority="high",
                        potential_savings_cost=agg["total_cost"],
                        potential_savings_co2e_grams=agg["co2e_grams"],
                    )
                )
        return recs

    # ------------------------------------------------------------------
    # RIGHTSIZING_CPU (new, usage-based)
    # ------------------------------------------------------------------

    def _analyze_cpu_rightsizing(self, pod_series: Dict[Tuple[str, str], List[CombinedMetric]]) -> List[Recommendation]:
        """Identifies pods with CPU requests much larger than actual usage."""
        recs = []

        for (ns, pod), series in pod_series.items():
            cpu_request = max((m.cpu_request or 0) for m in series)
            if cpu_request == 0:
                continue

            usages = [m.cpu_usage_millicores for m in series if m.cpu_usage_millicores is not None]
            if not usages:
                continue

            avg_usage = sum(usages) / len(usages)
            usage_ratio = avg_usage / cpu_request

            if usage_ratio < self.rightsizing_cpu_threshold:
                p95 = self._percentile(usages, 95)
                recommended = max(int(p95 * self.rightsizing_headroom), 1)

                total_cost = sum(m.total_cost for m in series)
                total_co2 = sum(m.co2e_grams for m in series)
                savings_ratio = max(0, 1.0 - (recommended / cpu_request))

                recs.append(
                    Recommendation(
                        pod_name=pod,
                        namespace=ns,
                        type=RecommendationType.RIGHTSIZING_CPU,
                        description=(
                            f"Pod '{pod}' uses avg {avg_usage:.0f}m of {cpu_request}m CPU "
                            f"requested ({usage_ratio:.0%}). Recommend reducing to {recommended}m."
                        ),
                        reason=(f"Average CPU usage is {usage_ratio:.0%} of the request. P95 usage is {p95:.0f}m."),
                        priority="medium",
                        current_cpu_request_millicores=cpu_request,
                        recommended_cpu_request_millicores=recommended,
                        potential_savings_cost=total_cost * savings_ratio,
                        potential_savings_co2e_grams=total_co2 * savings_ratio,
                    )
                )
        return recs

    # ------------------------------------------------------------------
    # RIGHTSIZING_CPU legacy (energy-based, for backward compat)
    # ------------------------------------------------------------------

    def _analyze_cpu_rightsizing_legacy(self, metrics: List[CombinedMetric]) -> List[Recommendation]:
        """Legacy energy-based CPU rightsizing (for backward compatibility)."""
        from greenkube.data.instance_profiles import INSTANCE_PROFILES

        recs = []
        if not metrics:
            return recs

        for metric in metrics:
            if metric.cpu_request == 0:
                continue
            usage_percent = self._estimate_cpu_usage_percent_legacy(metric, INSTANCE_PROFILES)
            if 0 < usage_percent < 0.2:
                recs.append(
                    Recommendation(
                        pod_name=metric.pod_name,
                        namespace=metric.namespace,
                        type=RecommendationType.RIGHTSIZING_CPU,
                        description=(
                            f"Pod is only using {usage_percent:.1%} of its requested "
                            f"{metric.cpu_request}m CPU (based on energy consumption)."
                        ),
                        reason=f"Energy-based CPU usage estimate is {usage_percent:.1%}.",
                        priority="medium",
                    )
                )
        return recs

    @staticmethod
    def _estimate_cpu_usage_percent_legacy(metric: CombinedMetric, instance_profiles: dict) -> float:
        """Legacy energy-based CPU usage estimation."""
        if metric.cpu_request == 0:
            return 0.0
        duration = metric.duration_seconds or 1
        current_watts = metric.joules / duration
        min_watts = config.DEFAULT_INSTANCE_MIN_WATTS
        max_watts = config.DEFAULT_INSTANCE_MAX_WATTS
        vcores = config.DEFAULT_INSTANCE_VCORES
        if metric.node_instance_type:
            profile = instance_profiles.get(metric.node_instance_type)
            if profile:
                min_watts = profile.get("minWatts", min_watts)
                max_watts = profile.get("maxWatts", max_watts)
                vcores = profile.get("vcores", vcores)
        if max_watts == min_watts:
            return 0.0
        cpu_util = (current_watts - min_watts) / (max_watts - min_watts)
        cpu_util = max(0.0, min(cpu_util, 1.0))
        implied_cores = cpu_util * vcores
        request_cores = metric.cpu_request / 1000.0
        if request_cores == 0:
            return 0.0
        return implied_cores / request_cores

    # ------------------------------------------------------------------
    # RIGHTSIZING_MEMORY
    # ------------------------------------------------------------------

    def _analyze_memory_rightsizing(
        self, pod_series: Dict[Tuple[str, str], List[CombinedMetric]]
    ) -> List[Recommendation]:
        """Identifies pods with memory requests much larger than actual usage."""
        recs = []

        for (ns, pod), series in pod_series.items():
            mem_request = max((m.memory_request or 0) for m in series)
            if mem_request == 0:
                continue

            usages = [m.memory_usage_bytes for m in series if m.memory_usage_bytes is not None]
            if not usages:
                continue

            avg_usage = sum(usages) / len(usages)
            usage_ratio = avg_usage / mem_request

            if usage_ratio < self.rightsizing_memory_threshold:
                p95 = self._percentile(usages, 95)
                recommended = max(int(p95 * self.rightsizing_headroom), 1)

                total_cost = sum(m.total_cost for m in series)
                total_co2 = sum(m.co2e_grams for m in series)
                savings_ratio = max(0, 1.0 - (recommended / mem_request))

                recs.append(
                    Recommendation(
                        pod_name=pod,
                        namespace=ns,
                        type=RecommendationType.RIGHTSIZING_MEMORY,
                        description=(
                            f"Pod '{pod}' uses avg {avg_usage / (1024 * 1024):.0f}MiB of "
                            f"{mem_request / (1024 * 1024):.0f}MiB memory requested ({usage_ratio:.0%}). "
                            f"Recommend reducing to {recommended / (1024 * 1024):.0f}MiB."
                        ),
                        reason=f"Average memory usage is {usage_ratio:.0%} of the request.",
                        priority="medium",
                        current_memory_request_bytes=mem_request,
                        recommended_memory_request_bytes=recommended,
                        potential_savings_cost=total_cost * savings_ratio,
                        potential_savings_co2e_grams=total_co2 * savings_ratio,
                    )
                )
        return recs

    # ------------------------------------------------------------------
    # AUTOSCALING_CANDIDATE
    # ------------------------------------------------------------------

    def _analyze_autoscaling(self, pod_series: Dict[Tuple[str, str], List[CombinedMetric]]) -> List[Recommendation]:
        """Identifies pods with spiky load patterns that would benefit from HPA."""
        recs = []

        for (ns, pod), series in pod_series.items():
            usages = [m.cpu_usage_millicores for m in series if m.cpu_usage_millicores is not None]
            if len(usages) < 3:
                continue

            cpu_request = max((m.cpu_request or 0) for m in series)
            if cpu_request == 0:
                continue

            mean_usage = sum(usages) / len(usages)
            if mean_usage == 0:
                continue

            variance = sum((u - mean_usage) ** 2 for u in usages) / len(usages)
            stddev = math.sqrt(variance)
            cv = stddev / mean_usage
            max_usage = max(usages)
            spike_ratio = max_usage / mean_usage

            if cv > self.autoscaling_cv_threshold and spike_ratio > self.autoscaling_spike_ratio:
                recs.append(
                    Recommendation(
                        pod_name=pod,
                        namespace=ns,
                        type=RecommendationType.AUTOSCALING_CANDIDATE,
                        description=(
                            f"Pod '{pod}' has highly variable CPU usage (CV={cv:.2f}, "
                            f"spike ratio={spike_ratio:.1f}x). Consider using HPA "
                            f"instead of static resource allocation."
                        ),
                        reason=(
                            f"CPU usage coefficient of variation is {cv:.2f} (threshold: "
                            f"{self.autoscaling_cv_threshold}). Max/mean ratio is {spike_ratio:.1f}x."
                        ),
                        priority="medium",
                        current_cpu_request_millicores=cpu_request,
                    )
                )
        return recs

    # ------------------------------------------------------------------
    # OFF_PEAK_SCALING
    # ------------------------------------------------------------------

    def _analyze_off_peak(self, pod_series: Dict[Tuple[str, str], List[CombinedMetric]]) -> List[Recommendation]:
        """Identifies workloads active only during certain hours."""
        recs = []

        for (ns, pod), series in pod_series.items():
            timed = [
                (m.timestamp, m.cpu_usage_millicores)
                for m in series
                if m.timestamp is not None and m.cpu_usage_millicores is not None
            ]
            if len(timed) < 6:
                continue

            hourly_usage: Dict[int, List[float]] = defaultdict(list)
            for ts, usage in timed:
                hourly_usage[ts.hour].append(float(usage))

            if not hourly_usage:
                continue

            hourly_avg = {h: sum(v) / len(v) for h, v in hourly_usage.items()}
            peak_usage = max(hourly_avg.values()) if hourly_avg else 0
            if peak_usage == 0:
                continue

            idle_threshold = peak_usage * self.off_peak_idle_threshold

            idle_hours = sorted([h for h, avg in hourly_avg.items() if avg < idle_threshold])
            consecutive = self._find_longest_consecutive_hours(idle_hours)

            if len(consecutive) >= self.off_peak_min_idle_hours:
                start_h = consecutive[0]
                end_h = (consecutive[-1] + 1) % 24
                cron_schedule = f"Scale to 0: {start_h:02d}:00-{end_h:02d}:00 UTC"

                recs.append(
                    Recommendation(
                        pod_name=pod,
                        namespace=ns,
                        type=RecommendationType.OFF_PEAK_SCALING,
                        description=(
                            f"Pod '{pod}' is idle {len(consecutive)}h/day "
                            f"({start_h:02d}:00-{end_h:02d}:00 UTC). "
                            f"Consider scaling to zero during off-peak hours."
                        ),
                        reason=(
                            f"Usage drops below {self.off_peak_idle_threshold:.0%} of peak "
                            f"for {len(consecutive)} consecutive hours."
                        ),
                        priority="medium",
                        cron_schedule=cron_schedule,
                    )
                )
        return recs

    @staticmethod
    def _find_longest_consecutive_hours(hours: List[int]) -> List[int]:
        """Finds the longest run of consecutive hours (wrapping around midnight)."""
        if not hours:
            return []

        hours_set = set(hours)
        best = []

        for start in hours:
            run = []
            h = start
            while h in hours_set:
                run.append(h)
                h = (h + 1) % 24
                if h not in hours_set or h == start:
                    break
            if len(run) > len(best):
                best = run

        return best

    # ------------------------------------------------------------------
    # IDLE_NAMESPACE
    # ------------------------------------------------------------------

    def _analyze_idle_namespaces(self, metrics: List[CombinedMetric]) -> List[Recommendation]:
        """Identifies namespaces with minimal total activity."""
        recs = []
        ns_agg: Dict[str, Dict] = defaultdict(lambda: {"joules": 0.0, "cost": 0.0, "co2e": 0.0})

        for m in metrics:
            ns_agg[m.namespace]["joules"] += m.joules
            ns_agg[m.namespace]["cost"] += m.total_cost
            ns_agg[m.namespace]["co2e"] += m.co2e_grams

        for ns, agg in ns_agg.items():
            if agg["joules"] < self.idle_namespace_energy_threshold and agg["cost"] > 0:
                recs.append(
                    Recommendation(
                        pod_name="*",
                        namespace=ns,
                        type=RecommendationType.IDLE_NAMESPACE,
                        description=(
                            f"Namespace '{ns}' consumed only {agg['joules']:.0f}J total energy "
                            f"but costs ${agg['cost']:.4f}. Consider decommissioning."
                        ),
                        reason=(
                            f"Total namespace energy ({agg['joules']:.0f}J) is below the "
                            f"idle threshold ({self.idle_namespace_energy_threshold}J)."
                        ),
                        priority="low",
                        potential_savings_cost=agg["cost"],
                        potential_savings_co2e_grams=agg["co2e"],
                    )
                )
        return recs

    # ------------------------------------------------------------------
    # CARBON_AWARE_SCHEDULING
    # ------------------------------------------------------------------

    def _analyze_carbon_aware(self, metrics: List[CombinedMetric]) -> List[Recommendation]:
        """Identifies pods running during high carbon intensity periods."""
        recs = []

        zone_intensities: Dict[str, List[float]] = defaultdict(list)
        for m in metrics:
            if m.emaps_zone and m.grid_intensity:
                zone_intensities[m.emaps_zone].append(m.grid_intensity)

        zone_avg: Dict[str, float] = {}
        for zone, vals in zone_intensities.items():
            zone_avg[zone] = sum(vals) / len(vals)

        pod_agg: Dict[Tuple[str, str], Dict] = defaultdict(lambda: {"intensities": [], "zone": None, "co2e": 0.0})

        for m in metrics:
            key = (m.namespace, m.pod_name)
            if m.grid_intensity:
                pod_agg[key]["intensities"].append(m.grid_intensity)
            if m.emaps_zone:
                pod_agg[key]["zone"] = m.emaps_zone
            pod_agg[key]["co2e"] += m.co2e_grams

        for (ns, pod), agg in pod_agg.items():
            zone = agg.get("zone")
            if not zone or zone not in zone_avg:
                continue
            intensities = agg["intensities"]
            if not intensities:
                continue

            pod_avg_intensity = sum(intensities) / len(intensities)
            zone_average = zone_avg[zone]

            if zone_average == 0:
                continue

            ratio = pod_avg_intensity / zone_average
            if ratio > self.carbon_aware_threshold:
                recs.append(
                    Recommendation(
                        pod_name=pod,
                        namespace=ns,
                        type=RecommendationType.CARBON_AWARE_SCHEDULING,
                        description=(
                            f"Pod '{pod}' runs during high carbon intensity periods "
                            f"(avg {pod_avg_intensity:.0f} vs zone avg {zone_average:.0f} gCO2e/kWh, "
                            f"{ratio:.1f}x). Consider scheduling during low-carbon windows."
                        ),
                        reason=(
                            f"Pod grid intensity is {ratio:.1f}x the zone average "
                            f"(threshold: {self.carbon_aware_threshold}x)."
                        ),
                        priority="low",
                        potential_savings_co2e_grams=agg["co2e"] * (1 - 1 / ratio),
                    )
                )
        return recs

    # ------------------------------------------------------------------
    # NODE-LEVEL: OVERPROVISIONED_NODE + UNDERUTILIZED_NODE
    # ------------------------------------------------------------------

    def _analyze_nodes(
        self,
        metrics: List[CombinedMetric],
        node_infos: Optional[List] = None,
    ) -> List[Recommendation]:
        """Analyzes node-level utilization patterns."""
        recs = []
        if not node_infos:
            return recs

        node_capacity: Dict[str, float] = {}
        for ni in node_infos:
            if hasattr(ni, "name") and hasattr(ni, "cpu_capacity_cores"):
                node_capacity[ni.name] = ni.cpu_capacity_cores or 0

        node_usage: Dict[str, List[float]] = defaultdict(list)
        node_pods: Dict[str, set] = defaultdict(set)

        for m in metrics:
            if m.node and m.cpu_usage_millicores is not None:
                node_usage[m.node].append(m.cpu_usage_millicores)
                node_pods[m.node].add(m.pod_name)

        for node_name, capacity_cores in node_capacity.items():
            if capacity_cores <= 0:
                continue

            usages = node_usage.get(node_name, [])
            if not usages:
                continue

            capacity_millicores = capacity_cores * 1000
            avg_total_usage = sum(usages) / len(usages)
            utilization = avg_total_usage / capacity_millicores
            unique_pods = len(node_pods.get(node_name, set()))

            # OVERPROVISIONED_NODE
            if utilization < self.node_utilization_threshold:
                recs.append(
                    Recommendation(
                        pod_name="*",
                        namespace="*",
                        type=RecommendationType.OVERPROVISIONED_NODE,
                        description=(
                            f"Node '{node_name}' has {utilization:.0%} average CPU utilization "
                            f"({avg_total_usage:.0f}m / {capacity_millicores:.0f}m). "
                            f"Consider consolidating workloads or downsizing."
                        ),
                        reason=(
                            f"Node utilization ({utilization:.0%}) is below "
                            f"threshold ({self.node_utilization_threshold:.0%})."
                        ),
                        priority="medium",
                        target_node=node_name,
                    )
                )

            # UNDERUTILIZED_NODE: few pods + low utilization
            if unique_pods < 3 and utilization < 0.15:
                recs.append(
                    Recommendation(
                        pod_name="*",
                        namespace="*",
                        type=RecommendationType.UNDERUTILIZED_NODE,
                        description=(
                            f"Node '{node_name}' has only {unique_pods} pod(s) and "
                            f"{utilization:.0%} utilization. Consider draining and removing."
                        ),
                        reason=(f"Node has {unique_pods} pods (< 3) and {utilization:.0%} utilization (< 15%)."),
                        priority="low",
                        target_node=node_name,
                    )
                )

        return recs
