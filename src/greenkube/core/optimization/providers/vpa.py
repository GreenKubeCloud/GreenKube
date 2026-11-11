# src/greenkube/core/optimization/providers/vpa.py
"""VPA (VerticalPodAutoscaler) recommendation source.

Reads recommendation-mode VPAs (``updateMode: Off``) and translates their
container targets into CPU/memory rightsizing recommendations. When this source
is enabled, the engine's arbitration drops the native rightsizing recommendation
for the same workload so VPA advice is never duplicated or contradicted.
"""

import logging
from typing import TYPE_CHECKING, Any, ClassVar, Dict, FrozenSet, List, Optional, Protocol, Tuple

from greenkube.collectors.vpa_collector import VPACollector, VPARecommendation
from greenkube.core.optimization.providers.base import RecommendationSource
from greenkube.core.optimization.statistics import latest_request_value, resource_savings_ratio
from greenkube.impact.projector import ImpactProjector, impact_fields
from greenkube.models.metrics import Recommendation, RecommendationType
from greenkube.models.metrics import RecommendationSource as Source
from greenkube.signals.adapters import RecommendationSourceAdapter
from greenkube.signals.model import Capability
from greenkube.signals.sdk import SourceDeps, SourceTier

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.core.optimization.context import OptimizationContext

logger = logging.getLogger(__name__)


class VPACollectorLike(Protocol):
    """Minimal collector contract, so tests can inject fakes."""

    async def collect(self) -> List[VPARecommendation]: ...


def _aggregate_by_container(
    vpas: List[VPARecommendation],
) -> Dict[Tuple[str, str, str, str], Dict]:
    """Combines duplicate VPA entries without merging distinct containers."""
    grouped: Dict[Tuple[str, str, str, str], Dict] = {}
    for vpa in vpas:
        container_name = vpa.container_name
        if not container_name or container_name.strip() != container_name:
            continue
        key = (vpa.namespace, vpa.target_kind, vpa.target_name, container_name)
        entry = grouped.setdefault(
            key,
            {
                "cpu_target": None,
                "cpu_ambiguous": False,
                "memory_target": None,
                "memory_ambiguous": False,
                "vpa_names": [],
            },
        )
        for resource, value in (
            ("cpu", vpa.cpu_target_millicores),
            ("memory", vpa.memory_target_bytes),
        ):
            if value is None:
                continue
            target_key = f"{resource}_target"
            ambiguous_key = f"{resource}_ambiguous"
            current = entry[target_key]
            if current is not None and current != value:
                entry[ambiguous_key] = True
            else:
                entry[target_key] = value
        if vpa.vpa_name and vpa.vpa_name not in entry["vpa_names"]:
            entry["vpa_names"].append(vpa.vpa_name)
    return grouped


class VpaSource(RecommendationSource):
    """Recommendation source backed by VerticalPodAutoscalers."""

    name = "vpa"
    priority = 3

    def __init__(self, config: "Config", collector: Optional[VPACollectorLike] = None):
        self.config = config
        self._collector = collector if collector is not None else VPACollector()
        self._available: Optional[bool] = None

    async def is_available(self) -> bool:
        """Detects the VPA CRD once; sources are skipped cleanly when absent.

        Test fakes injected through ``collector`` are considered available.
        """
        if self._available is not None:
            return self._available

        if not isinstance(self._collector, VPACollector):
            self._available = True
            return True

        try:
            from greenkube.collectors.vpa_collector import VPA_GROUP, VPA_PLURAL, VPA_VERSION
            from greenkube.core.k8s_client import get_custom_objects_api

            api = await get_custom_objects_api()
            if api is None:
                self._available = False
                return False
            await api.list_cluster_custom_object(
                group=VPA_GROUP,
                version=VPA_VERSION,
                plural=VPA_PLURAL,
                limit=1,
            )
            self._available = True
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status == 404:
                logger.debug("VerticalPodAutoscaler CRD is not installed; VPA source disabled.")
            else:
                logger.warning("VPA availability check failed: %s", exc)
            self._available = False
        return self._available

    async def collect(self, context: "OptimizationContext") -> List[Recommendation]:
        try:
            vpas = await self._collector.collect()
        except Exception as e:
            logger.warning("VPA collection failed: %s. Proceeding without VPA recommendations.", e)
            return []
        if not vpas:
            return []

        target_series = context.target_series()
        projector = ImpactProjector.for_context(context)
        recs: List[Recommendation] = []

        for (ns, target_kind, target_name, container_name), agg in _aggregate_by_container(vpas).items():
            workload_series = target_series.get((ns, target_kind, target_name)) or []
            series = [metric for metric in workload_series if metric.container_name == container_name]
            if not series:
                continue

            current_cpu = latest_request_value(series, "cpu_request")
            current_mem = latest_request_value(series, "memory_request")
            ref = projector.workload_ref(ns, target_kind, target_name, container_name)

            vpa_ref = f"{ns}/{agg['vpa_names'][0]}" if agg["vpa_names"] else ns
            label = f"{target_kind} '{target_name}' container '{container_name}'"

            if agg["cpu_target"] is not None and not agg["cpu_ambiguous"] and current_cpu:
                target = int(agg["cpu_target"])
                ratio = resource_savings_ratio(current_cpu, target)
                if ratio is not None:
                    # One container, one resource: the Impact Model prices only the
                    # released CPU reservation (BUG-15, BUG-16).
                    delta = projector.container_resources(ref, series, cpu_millicores=(current_cpu, target))
                    recs.append(
                        Recommendation(
                            pod_name=target_name,
                            container_name=container_name,
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
                                f"VPA '{vpa_ref}' recommends {target}m CPU for container "
                                f"'{container_name}'; current request is {current_cpu}m."
                            ),
                            priority="medium",
                            current_cpu_request_millicores=current_cpu,
                            recommended_cpu_request_millicores=target,
                            **impact_fields(delta),
                        )
                    )

            if agg["memory_target"] is not None and not agg["memory_ambiguous"] and current_mem:
                target_mem = int(agg["memory_target"])
                ratio = resource_savings_ratio(current_mem, target_mem)
                if ratio is not None:
                    delta = projector.container_resources(ref, series, memory_bytes=(current_mem, target_mem))
                    recs.append(
                        Recommendation(
                            pod_name=target_name,
                            container_name=container_name,
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
                                f"for container '{container_name}'; current request is "
                                f"{current_mem / (1024 * 1024):.0f}MiB."
                            ),
                            priority="medium",
                            current_memory_request_bytes=current_mem,
                            recommended_memory_request_bytes=target_mem,
                            **impact_fields(delta),
                        )
                    )

        return recs


class VpaSignalSource(RecommendationSourceAdapter):
    """VPA recommendation-mode targets as a Source SDK source (roadmap E5)."""

    id: ClassVar[str] = "vpa"
    display_name: ClassVar[str] = "Vertical Pod Autoscaler"
    tier: ClassVar[SourceTier] = SourceTier.OSS
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.CONTAINER_REQUESTS})
    legacy_source: ClassVar[Optional[str]] = Source.VPA.value
    algorithm_name: ClassVar[Optional[str]] = "vpa-recommender"
    description: ClassVar[str] = "Container request targets of VPAs in recommendation mode (updateMode: Off)."

    def build_connector(self, deps: SourceDeps) -> RecommendationSource:
        return VpaSource(deps.config)

    def raw_payload(self, rec: Recommendation) -> Optional[Dict[str, Any]]:
        return {
            "vpa": rec.source_ref,
            "container": rec.container_name,
            "target": {
                "cpu_millicores": rec.recommended_cpu_request_millicores,
                "memory_bytes": rec.recommended_memory_request_bytes,
            },
        }
