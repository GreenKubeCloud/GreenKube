# src/greenkube/core/optimization/providers/vpa.py
"""VPA (VerticalPodAutoscaler) recommendation source.

Reads recommendation-mode VPAs (``updateMode: Off``) and translates their
container targets into CPU/memory rightsizing recommendations. When this source
is enabled, the engine's arbitration drops the native rightsizing recommendation
for the same workload so VPA advice is never duplicated or contradicted.
"""

import logging
from typing import TYPE_CHECKING, Dict, List, Optional, Protocol, Tuple

from greenkube.collectors.vpa_collector import VPACollector, VPARecommendation
from greenkube.core.optimization.providers.base import RecommendationSource
from greenkube.core.optimization.statistics import (
    annualized_window_total,
    latest_request_value,
    resource_savings_ratio,
)
from greenkube.models.metrics import Recommendation, RecommendationType
from greenkube.models.metrics import RecommendationSource as Source

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.core.optimization.context import OptimizationContext

logger = logging.getLogger(__name__)


class VPACollectorLike(Protocol):
    """Minimal collector contract, so tests can inject fakes."""

    async def collect(self) -> List[VPARecommendation]: ...


def _aggregate_by_target(
    vpas: List[VPARecommendation],
) -> Dict[Tuple[str, str, str], Dict]:
    """Aggregates per-container VPA targets into one entry per workload.

    A pod's native request is the sum of its containers' requests, so summing
    VPA per-container targets yields a directly comparable value.
    """
    grouped: Dict[Tuple[str, str, str], Dict] = {}
    for vpa in vpas:
        key = (vpa.namespace, vpa.target_kind, vpa.target_name)
        entry = grouped.setdefault(
            key,
            {
                "cpu_target": 0,
                "cpu_seen": False,
                "memory_target": 0,
                "memory_seen": False,
                "vpa_names": [],
                "containers": 0,
            },
        )
        if vpa.cpu_target_millicores is not None:
            entry["cpu_target"] += vpa.cpu_target_millicores
            entry["cpu_seen"] = True
        if vpa.memory_target_bytes is not None:
            entry["memory_target"] += vpa.memory_target_bytes
            entry["memory_seen"] = True
        if vpa.vpa_name and vpa.vpa_name not in entry["vpa_names"]:
            entry["vpa_names"].append(vpa.vpa_name)
        entry["containers"] += 1
    return grouped


class VpaSource(RecommendationSource):
    """Recommendation source backed by VerticalPodAutoscalers."""

    name = "vpa"
    priority = 3

    def __init__(self, config: "Config", collector: Optional[VPACollectorLike] = None):
        self.config = config
        self._collector = collector if collector is not None else VPACollector()

    async def is_available(self) -> bool:
        # Availability of the CRD is handled gracefully by the collector.
        return True

    async def collect(self, context: "OptimizationContext") -> List[Recommendation]:
        try:
            vpas = await self._collector.collect()
        except Exception as e:
            logger.warning("VPA collection failed: %s. Proceeding without VPA recommendations.", e)
            return []
        if not vpas:
            return []

        target_series = context.target_series()
        recs: List[Recommendation] = []

        for (ns, target_kind, target_name), agg in _aggregate_by_target(vpas).items():
            series = target_series.get((ns, target_kind, target_name)) or []
            if not series:
                continue

            current_cpu = latest_request_value(series, "cpu_request")
            current_mem = latest_request_value(series, "memory_request")
            total_cost = sum(m.total_cost for m in series)
            total_co2 = sum(m.co2e_grams for m in series)
            annual_cost = annualized_window_total(total_cost, context.analysis_window_seconds)
            annual_co2 = annualized_window_total(total_co2, context.analysis_window_seconds)

            vpa_ref = f"{ns}/{agg['vpa_names'][0]}" if agg["vpa_names"] else ns
            label = f"{target_kind} '{target_name}'"

            if agg["cpu_seen"] and current_cpu:
                target = int(agg["cpu_target"])
                ratio = resource_savings_ratio(current_cpu, target)
                if ratio is not None:
                    recs.append(
                        Recommendation(
                            pod_name=target_name,
                            namespace=ns,
                            type=RecommendationType.RIGHTSIZING_CPU,
                            scope="workload",
                            owner_kind=target_kind,
                            owner_name=target_name,
                            source=Source.VPA,
                            source_ref=vpa_ref,
                            description=(
                                f"{label} CPU request {current_cpu}m exceeds the VPA target {target}m "
                                f"({ratio:.0%} reduction). Recommend reducing to {target}m."
                            ),
                            reason=(
                                f"VPA '{vpa_ref}' recommends {target}m CPU across "
                                f"{agg['containers']} container(s); current request is {current_cpu}m."
                            ),
                            priority="medium",
                            current_cpu_request_millicores=current_cpu,
                            recommended_cpu_request_millicores=target,
                            potential_savings_cost=annual_cost * ratio,
                            potential_savings_co2e_grams=annual_co2 * ratio,
                        )
                    )

            if agg["memory_seen"] and current_mem:
                target_mem = int(agg["memory_target"])
                ratio = resource_savings_ratio(current_mem, target_mem)
                if ratio is not None:
                    recs.append(
                        Recommendation(
                            pod_name=target_name,
                            namespace=ns,
                            type=RecommendationType.RIGHTSIZING_MEMORY,
                            scope="workload",
                            owner_kind=target_kind,
                            owner_name=target_name,
                            source=Source.VPA,
                            source_ref=vpa_ref,
                            description=(
                                f"{label} memory request {current_mem / (1024 * 1024):.0f}MiB exceeds the "
                                f"VPA target {target_mem / (1024 * 1024):.0f}MiB ({ratio:.0%} reduction). "
                                f"Recommend reducing to {target_mem / (1024 * 1024):.0f}MiB."
                            ),
                            reason=(
                                f"VPA '{vpa_ref}' recommends {target_mem / (1024 * 1024):.0f}MiB memory "
                                f"across {agg['containers']} container(s); current request is "
                                f"{current_mem / (1024 * 1024):.0f}MiB."
                            ),
                            priority="medium",
                            current_memory_request_bytes=current_mem,
                            recommended_memory_request_bytes=target_mem,
                            potential_savings_cost=annual_cost * ratio,
                            potential_savings_co2e_grams=annual_co2 * ratio,
                        )
                    )

        return recs
