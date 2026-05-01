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
import re
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from greenkube.core.config import Config, get_config
from greenkube.models.metrics import CombinedMetric, Recommendation, RecommendationType

LOG = logging.getLogger(__name__)

DEPLOYMENT_POD_NAME_RE = re.compile(r"^(?P<deployment>.+)-[a-z0-9]{8,10}-[a-z0-9]{5}$")


class Recommender:
    """Analyzes combined metrics to generate optimization recommendations."""

    def __init__(self, config: Config | None = None):
        """Initializes the recommender with thresholds from config.

        Args:
            config: Optional Config instance for dependency injection.
                    Falls back to the module-level singleton.
        """
        cfg = config if config is not None else get_config()
        self.rightsizing_cpu_threshold = cfg.RIGHTSIZING_CPU_THRESHOLD
        self.rightsizing_memory_threshold = cfg.RIGHTSIZING_MEMORY_THRESHOLD
        self.rightsizing_headroom = cfg.RIGHTSIZING_HEADROOM
        self.zombie_cost_threshold = cfg.ZOMBIE_COST_THRESHOLD
        self.zombie_energy_threshold = cfg.ZOMBIE_ENERGY_THRESHOLD
        self.autoscaling_cv_threshold = cfg.AUTOSCALING_CV_THRESHOLD
        self.autoscaling_spike_ratio = cfg.AUTOSCALING_SPIKE_RATIO
        self.off_peak_idle_threshold = cfg.OFF_PEAK_IDLE_THRESHOLD
        self.off_peak_min_idle_hours = cfg.OFF_PEAK_MIN_IDLE_HOURS
        self.idle_namespace_energy_threshold = cfg.IDLE_NAMESPACE_ENERGY_THRESHOLD
        self.carbon_aware_threshold = cfg.CARBON_AWARE_THRESHOLD
        self.node_utilization_threshold = cfg.NODE_UTILIZATION_THRESHOLD
        self.recommend_system_namespaces = cfg.RECOMMEND_SYSTEM_NAMESPACES
        self.default_instance_min_watts = cfg.DEFAULT_INSTANCE_MIN_WATTS
        self.default_instance_max_watts = cfg.DEFAULT_INSTANCE_MAX_WATTS
        self.default_instance_vcores = cfg.DEFAULT_INSTANCE_VCORES
        self.min_cpu_millicores = cfg.RECOMMENDATION_MIN_CPU_MILLICORES
        self.min_memory_bytes = cfg.RECOMMENDATION_MIN_MEMORY_BYTES

    def generate_recommendations(
        self,
        metrics: List[CombinedMetric],
        node_infos: Optional[List] = None,
        hpa_targets: Optional[Set[Tuple[str, str, str]]] = None,
    ) -> List[Recommendation]:
        """Generates all recommendation types from metrics.

        Args:
            metrics: List of CombinedMetric objects to analyze.
            node_infos: Optional list of NodeInfo objects for node-level analysis.
            hpa_targets: Optional set of (namespace, kind, name) tuples for workloads
                         already governed by an HPA. Autoscaling recommendations are
                         skipped for these workloads.

        Returns:
            A deduplicated list of Recommendation objects.
        """
        if not metrics:
            return []

        target_series = self._group_by_recommendation_target(metrics)

        recs: List[Recommendation] = []
        recs.extend(self._analyze_zombies(target_series))
        recs.extend(self._analyze_cpu_rightsizing(target_series))
        recs.extend(self._analyze_memory_rightsizing(target_series))
        recs.extend(self._analyze_autoscaling(target_series, hpa_targets=hpa_targets))
        recs.extend(self._analyze_off_peak(target_series))
        recs.extend(self._analyze_idle_namespaces(metrics))
        recs.extend(self._analyze_carbon_aware(metrics, target_series))
        recs.extend(self._analyze_nodes(metrics, node_infos))

        deduped = self._deduplicate(recs)
        return [self._apply_minimum_thresholds(r) for r in deduped]

    # ------------------------------------------------------------------
    # Legacy API compatibility
    # ------------------------------------------------------------------

    def generate_zombie_recommendations(self, metrics: List[CombinedMetric]) -> List[Recommendation]:
        """Legacy method for zombie pod detection."""
        return self._analyze_zombies(self._group_by_recommendation_target(metrics))

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
    def _infer_deployment_target(pod_name: str) -> Tuple[str, str] | None:
        """Infer a Deployment target from the standard Deployment pod name format."""
        match = DEPLOYMENT_POD_NAME_RE.match(pod_name)
        if not match:
            return None
        return "Deployment", match.group("deployment")

    @classmethod
    def _target_key(cls, metric: CombinedMetric) -> Tuple[str, str, str]:
        """Returns the stable recommendation target for a metric."""
        if metric.owner_kind and metric.owner_name:
            return (metric.namespace, metric.owner_kind, metric.owner_name)

        inferred_target = cls._infer_deployment_target(metric.pod_name)
        if inferred_target:
            target_kind, target_name = inferred_target
            return (metric.namespace, target_kind, target_name)

        return (metric.namespace, "Pod", metric.pod_name)

    @classmethod
    def _group_by_recommendation_target(
        cls, metrics: List[CombinedMetric]
    ) -> Dict[Tuple[str, str, str], List[CombinedMetric]]:
        """Groups metrics by stable workload owner, falling back to pod name."""
        groups: Dict[Tuple[str, str, str], List[CombinedMetric]] = defaultdict(list)
        for metric in metrics:
            groups[cls._target_key(metric)].append(metric)
        return groups

    @staticmethod
    def _scope_for_target_kind(target_kind: str) -> str:
        """Maps a Kubernetes owner kind to the persisted recommendation scope."""
        return "pod" if target_kind == "Pod" else "workload"

    @staticmethod
    def _target_label(target_kind: str, target_name: str) -> str:
        """Returns a compact human label for a recommendation target."""
        return f"{target_kind} '{target_name}'"

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
    def _usage_stats(
        series: List[CombinedMetric],
        usage_attr: str,
        max_attr: str,
    ) -> Tuple[float, float, List[float]]:
        """Returns weighted average, observed maximum, and average points for a usage series."""
        weighted_total = 0.0
        sample_total = 0
        average_points: List[float] = []
        observed_max_values: List[float] = []

        for metric in series:
            usage = getattr(metric, usage_attr)
            if usage is None:
                continue

            sample_count = max(int(getattr(metric, "sample_count", 1) or 1), 1)
            usage_float = float(usage)
            weighted_total += usage_float * sample_count
            sample_total += sample_count
            average_points.append(usage_float)

            max_usage = getattr(metric, max_attr, None)
            observed_max_values.append(float(max_usage if max_usage is not None else usage_float))

        if sample_total == 0 or not observed_max_values:
            return 0.0, 0.0, []

        return weighted_total / sample_total, max(observed_max_values), average_points

    def _balanced_rightsizing_target(self, avg_usage: float, observed_max: float, p95_usage: float) -> int:
        """Calculates a rightsizing target that balances steady-state and peak demand."""
        balanced_peak = (avg_usage + observed_max) / 2.0
        return max(int(max(p95_usage, balanced_peak) * self.rightsizing_headroom), 1)

    @staticmethod
    def _deduplicate(recs: List[Recommendation]) -> List[Recommendation]:
        """Removes duplicate recommendations with the same target and type."""
        seen = set()
        result = []
        for rec in recs:
            key = (rec.scope, rec.namespace, rec.pod_name, rec.type, rec.target_node or "")
            if key not in seen:
                seen.add(key)
                result.append(rec)
        return result

    def _apply_minimum_thresholds(self, rec: Recommendation) -> Recommendation:
        """Clamps recommended resource values to configured minimum thresholds.

        Ensures that no recommendation asks for an impractically small resource
        request (e.g. 3m CPU). The description is updated to mention the floor
        when clamping occurs.

        Args:
            rec: The recommendation to validate and possibly clamp.

        Returns:
            The recommendation with values floored to configured minimums.
        """
        updates: dict = {}

        if (
            rec.recommended_cpu_request_millicores is not None
            and rec.recommended_cpu_request_millicores < self.min_cpu_millicores
        ):
            updates["recommended_cpu_request_millicores"] = self.min_cpu_millicores
            updates["description"] = rec.description + f" (Floored to minimum: {self.min_cpu_millicores}m CPU.)"
            updates["reason"] = rec.reason + (
                f" Recommended value was below the minimum of {self.min_cpu_millicores}m; "
                "floored to avoid impractically small requests."
            )

        if (
            rec.recommended_memory_request_bytes is not None
            and rec.recommended_memory_request_bytes < self.min_memory_bytes
        ):
            mb = self.min_memory_bytes // (1024 * 1024)
            updates["recommended_memory_request_bytes"] = self.min_memory_bytes
            desc = updates.get("description", rec.description)
            updates["description"] = desc + f" (Floored to minimum: {mb}MiB memory.)"
            reason = updates.get("reason", rec.reason)
            updates["reason"] = reason + (
                f" Recommended memory was below the minimum of {mb}MiB; floored to avoid impractically small requests."
            )

        if updates:
            rec = rec.model_copy(update=updates)

        return rec

    # ------------------------------------------------------------------
    # ZOMBIE_POD
    # ------------------------------------------------------------------

    def _analyze_zombies(
        self,
        target_series: Dict[Tuple[str, str, str], List[CombinedMetric]],
    ) -> List[Recommendation]:
        """Identifies targets with cost but near-zero energy consumption."""
        recs = []

        for (ns, target_kind, target_name), series in target_series.items():
            agg = {
                "total_cost": sum(metric.total_cost for metric in series),
                "joules": sum(metric.joules for metric in series),
                "co2e_grams": sum(metric.co2e_grams for metric in series),
            }
            if agg["total_cost"] > self.zombie_cost_threshold and agg["joules"] < self.zombie_energy_threshold:
                target_label = self._target_label(target_kind, target_name)
                recs.append(
                    Recommendation(
                        pod_name=target_name,
                        namespace=ns,
                        type=RecommendationType.ZOMBIE_POD,
                        scope=self._scope_for_target_kind(target_kind),
                        description=(
                            f"{target_label} has cost ${agg['total_cost']:.4f} but consumed only "
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

    def _analyze_cpu_rightsizing(
        self,
        target_series: Dict[Tuple[str, str, str], List[CombinedMetric]],
    ) -> List[Recommendation]:
        """Identifies targets with CPU requests much larger than actual usage."""
        recs = []

        for (ns, target_kind, target_name), series in target_series.items():
            cpu_request = max((m.cpu_request or 0) for m in series)
            if cpu_request == 0:
                continue

            avg_usage, observed_max, usages = self._usage_stats(
                series,
                "cpu_usage_millicores",
                "cpu_usage_max_millicores",
            )
            if not usages:
                continue

            usage_ratio = avg_usage / cpu_request

            if usage_ratio < self.rightsizing_cpu_threshold:
                p95 = self._percentile(usages, 95)
                recommended = self._balanced_rightsizing_target(avg_usage, observed_max, p95)

                total_cost = sum(m.total_cost for m in series)
                total_co2 = sum(m.co2e_grams for m in series)
                savings_ratio = max(0, 1.0 - (recommended / cpu_request))
                target_label = self._target_label(target_kind, target_name)

                recs.append(
                    Recommendation(
                        pod_name=target_name,
                        namespace=ns,
                        type=RecommendationType.RIGHTSIZING_CPU,
                        scope=self._scope_for_target_kind(target_kind),
                        description=(
                            f"{target_label} uses avg {avg_usage:.0f}m and max {observed_max:.0f}m of "
                            f"{cpu_request}m CPU "
                            f"requested ({usage_ratio:.0%}). Recommend reducing to {recommended}m."
                        ),
                        reason=(
                            f"Average CPU usage is {usage_ratio:.0%} of the request. "
                            f"P95 usage is {p95:.0f}m and observed max is {observed_max:.0f}m."
                        ),
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

    def _estimate_cpu_usage_percent_legacy(self, metric: CombinedMetric, instance_profiles: dict) -> float:
        """Legacy energy-based CPU usage estimation."""
        if metric.cpu_request == 0:
            return 0.0
        duration = metric.duration_seconds or 1
        current_watts = metric.joules / duration
        min_watts = self.default_instance_min_watts
        max_watts = self.default_instance_max_watts
        vcores = self.default_instance_vcores
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
        self, target_series: Dict[Tuple[str, str, str], List[CombinedMetric]]
    ) -> List[Recommendation]:
        """Identifies targets with memory requests much larger than actual usage."""
        recs = []

        for (ns, target_kind, target_name), series in target_series.items():
            mem_request = max((m.memory_request or 0) for m in series)
            if mem_request == 0:
                continue

            avg_usage, observed_max, usages = self._usage_stats(
                series,
                "memory_usage_bytes",
                "memory_usage_max_bytes",
            )
            if not usages:
                continue

            usage_ratio = avg_usage / mem_request

            if usage_ratio < self.rightsizing_memory_threshold:
                p95 = self._percentile(usages, 95)
                recommended = self._balanced_rightsizing_target(avg_usage, observed_max, p95)

                total_cost = sum(m.total_cost for m in series)
                total_co2 = sum(m.co2e_grams for m in series)
                savings_ratio = max(0, 1.0 - (recommended / mem_request))
                target_label = self._target_label(target_kind, target_name)

                recs.append(
                    Recommendation(
                        pod_name=target_name,
                        namespace=ns,
                        type=RecommendationType.RIGHTSIZING_MEMORY,
                        scope=self._scope_for_target_kind(target_kind),
                        description=(
                            f"{target_label} uses avg {avg_usage / (1024 * 1024):.0f}MiB and max "
                            f"{observed_max / (1024 * 1024):.0f}MiB of "
                            f"{mem_request / (1024 * 1024):.0f}MiB memory requested ({usage_ratio:.0%}). "
                            f"Recommend reducing to {recommended / (1024 * 1024):.0f}MiB."
                        ),
                        reason=(
                            f"Average memory usage is {usage_ratio:.0%} of the request. "
                            f"P95 usage is {p95 / (1024 * 1024):.0f}MiB and observed max is "
                            f"{observed_max / (1024 * 1024):.0f}MiB."
                        ),
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

    def _analyze_autoscaling(
        self,
        target_series: Dict[Tuple[str, str, str], List[CombinedMetric]],
        hpa_targets: Optional[Set[Tuple[str, str, str]]] = None,
    ) -> List[Recommendation]:
        """Identifies targets with spiky load patterns that would benefit from HPA.

        Skips pods whose owner (Deployment/StatefulSet) is already managed by
        an existing HorizontalPodAutoscaler.
        """
        recs = []

        for (ns, target_kind, target_name), series in target_series.items():
            mean_usage, max_usage, usages = self._usage_stats(
                series,
                "cpu_usage_millicores",
                "cpu_usage_max_millicores",
            )
            if len(usages) < 3:
                continue

            cpu_request = max((m.cpu_request or 0) for m in series)
            if cpu_request == 0:
                continue

            if mean_usage == 0:
                continue

            variance = sum((u - mean_usage) ** 2 for u in usages) / len(usages)
            stddev = math.sqrt(variance)
            cv = stddev / mean_usage
            spike_ratio = max_usage / mean_usage

            if cv > self.autoscaling_cv_threshold and spike_ratio > self.autoscaling_spike_ratio:
                # Check if the pod's owner already has an HPA
                if hpa_targets and target_kind != "Pod" and (ns, target_kind, target_name) in hpa_targets:
                    LOG.debug(
                        "Skipping autoscaling recommendation for %s/%s: HPA already exists for %s/%s",
                        ns,
                        target_name,
                        target_kind,
                        target_name,
                    )
                    continue

                target_label = self._target_label(target_kind, target_name)

                recs.append(
                    Recommendation(
                        pod_name=target_name,
                        namespace=ns,
                        type=RecommendationType.AUTOSCALING_CANDIDATE,
                        scope=self._scope_for_target_kind(target_kind),
                        description=(
                            f"{target_label} has highly variable CPU usage (CV={cv:.2f}, "
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

    def _analyze_off_peak(
        self, target_series: Dict[Tuple[str, str, str], List[CombinedMetric]]
    ) -> List[Recommendation]:
        """Identifies workloads active only during certain hours."""
        recs = []

        for (ns, target_kind, target_name), series in target_series.items():
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
                target_label = self._target_label(target_kind, target_name)

                recs.append(
                    Recommendation(
                        pod_name=target_name,
                        namespace=ns,
                        type=RecommendationType.OFF_PEAK_SCALING,
                        scope=self._scope_for_target_kind(target_kind),
                        description=(
                            f"{target_label} is idle {len(consecutive)}h/day "
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

        # Common system namespaces to exclude if option is set
        system_namespaces = {
            "kube-system",
            "kube-public",
            "kube-node-lease",
            "coredns",
            "istio-system",
            "kubernetes-dashboard",
        }

        for m in metrics:
            ns_agg[m.namespace]["joules"] += m.joules
            ns_agg[m.namespace]["cost"] += m.total_cost
            ns_agg[m.namespace]["co2e"] += m.co2e_grams

        for ns, agg in ns_agg.items():
            if not self.recommend_system_namespaces and ns in system_namespaces:
                continue
            if agg["joules"] < self.idle_namespace_energy_threshold and agg["cost"] > 0:
                recs.append(
                    Recommendation(
                        namespace=ns,
                        type=RecommendationType.IDLE_NAMESPACE,
                        scope="namespace",
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

    def _analyze_carbon_aware(
        self,
        metrics: List[CombinedMetric],
        target_series: Dict[Tuple[str, str, str], List[CombinedMetric]],
    ) -> List[Recommendation]:
        """Identifies targets running during high carbon intensity periods."""
        recs = []

        zone_intensities: Dict[str, List[float]] = defaultdict(list)
        for m in metrics:
            if m.emaps_zone and m.grid_intensity:
                zone_intensities[m.emaps_zone].append(m.grid_intensity)

        zone_avg: Dict[str, float] = {}
        for zone, vals in zone_intensities.items():
            zone_avg[zone] = sum(vals) / len(vals)

        for (ns, target_kind, target_name), series in target_series.items():
            agg: Dict = {"intensities": [], "zone": None, "co2e": 0.0}
            for metric in series:
                if metric.grid_intensity:
                    agg["intensities"].append(metric.grid_intensity)
                if metric.emaps_zone:
                    agg["zone"] = metric.emaps_zone
                agg["co2e"] += metric.co2e_grams

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
                target_label = self._target_label(target_kind, target_name)
                recs.append(
                    Recommendation(
                        pod_name=target_name,
                        namespace=ns,
                        type=RecommendationType.CARBON_AWARE_SCHEDULING,
                        scope=self._scope_for_target_kind(target_kind),
                        description=(
                            f"{target_label} runs during high carbon intensity periods "
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
        node_mem_capacity: Dict[str, int] = {}
        for ni in node_infos:
            if hasattr(ni, "name") and hasattr(ni, "cpu_capacity_cores"):
                node_capacity[ni.name] = ni.cpu_capacity_cores or 0
            if hasattr(ni, "name") and hasattr(ni, "memory_capacity_bytes"):
                mem_cap = ni.memory_capacity_bytes
                if isinstance(mem_cap, (int, float)) and mem_cap > 0:
                    node_mem_capacity[ni.name] = int(mem_cap)

        # Group pod CPU and memory usage per node per timestamp, so we can sum across pods
        node_usage_by_ts: Dict[str, Dict] = defaultdict(lambda: defaultdict(float))
        node_mem_usage_by_ts: Dict[str, Dict] = defaultdict(lambda: defaultdict(float))
        node_pods: Dict[str, set] = defaultdict(set)

        for m in metrics:
            if m.node:
                ts_key = m.timestamp if m.timestamp is not None else 0
                if m.cpu_usage_millicores is not None:
                    node_usage_by_ts[m.node][ts_key] += m.cpu_usage_millicores
                if m.memory_usage_bytes is not None:
                    node_mem_usage_by_ts[m.node][ts_key] += m.memory_usage_bytes
                node_pods[m.node].add(m.pod_name)

        for node_name, capacity_cores in node_capacity.items():
            if capacity_cores <= 0:
                continue

            ts_totals = list(node_usage_by_ts.get(node_name, {}).values())
            if not ts_totals:
                continue

            capacity_millicores = capacity_cores * 1000
            avg_total_usage = sum(ts_totals) / len(ts_totals)
            cpu_utilization = avg_total_usage / capacity_millicores
            unique_pods = len(node_pods.get(node_name, set()))

            # Compute memory utilization if capacity is available
            mem_capacity = node_mem_capacity.get(node_name)
            mem_utilization: Optional[float] = None
            avg_mem_usage: float = 0.0
            if mem_capacity:
                mem_ts_totals = list(node_mem_usage_by_ts.get(node_name, {}).values())
                if mem_ts_totals:
                    avg_mem_usage = sum(mem_ts_totals) / len(mem_ts_totals)
                    mem_utilization = avg_mem_usage / mem_capacity

            # OVERPROVISIONED_NODE: both CPU and memory (when available) must be low
            cpu_is_low = cpu_utilization < self.node_utilization_threshold
            mem_is_low = mem_utilization is None or mem_utilization < self.node_utilization_threshold

            if cpu_is_low and mem_is_low:
                mem_detail = (
                    f", memory {mem_utilization:.0%} ({avg_mem_usage / (1024**3):.1f} GiB / "
                    f"{mem_capacity / (1024**3):.1f} GiB)"
                    if mem_utilization is not None
                    else ""
                )
                recs.append(
                    Recommendation(
                        type=RecommendationType.OVERPROVISIONED_NODE,
                        scope="node",
                        description=(
                            f"Node '{node_name}' has {cpu_utilization:.0%} average CPU utilization "
                            f"({avg_total_usage:.0f}m / {capacity_millicores:.0f}m){mem_detail}. "
                            f"Consider consolidating workloads or downsizing."
                        ),
                        reason=(
                            f"Node CPU utilization ({cpu_utilization:.0%}) is below "
                            f"threshold ({self.node_utilization_threshold:.0%})."
                        ),
                        priority="medium",
                        target_node=node_name,
                    )
                )

            # UNDERUTILIZED_NODE: few pods + low utilization
            if unique_pods < 3 and cpu_utilization < 0.15:
                recs.append(
                    Recommendation(
                        type=RecommendationType.UNDERUTILIZED_NODE,
                        scope="node",
                        description=(
                            f"Node '{node_name}' has only {unique_pods} pod(s) and "
                            f"{cpu_utilization:.0%} CPU utilization. Consider draining and removing."
                        ),
                        reason=(
                            f"Node has {unique_pods} pods (< 3) and {cpu_utilization:.0%} CPU utilization (< 15%)."
                        ),
                        priority="low",
                        target_node=node_name,
                    )
                )

        return recs
