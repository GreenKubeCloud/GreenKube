# src/greenkube/collectors/vpa_collector.py
"""
Collects VerticalPodAutoscaler recommendations from the Kubernetes API.

Only VPAs in recommendation-only mode (``updateMode: Off``) are considered:
GreenKube must never duplicate a controller that already applies its own
recommendations.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from greenkube.core.config import config as global_config
from greenkube.core.k8s_client import get_custom_objects_api
from greenkube.utils.k8s_quantities import parse_cpu_quantity, parse_memory_quantity

logger = logging.getLogger(__name__)

VPA_GROUP = "autoscaling.k8s.io"
VPA_VERSION = "v1"
VPA_PLURAL = "verticalpodautoscalers"


@dataclass
class VPARecommendation:
    """A single container recommendation extracted from a VPA object."""

    namespace: str
    vpa_name: str
    target_kind: str
    target_name: str
    container_name: str
    cpu_target_millicores: Optional[int] = None
    memory_target_bytes: Optional[int] = None
    cpu_lower_millicores: Optional[int] = None
    memory_lower_bytes: Optional[int] = None
    cpu_upper_millicores: Optional[int] = None
    memory_upper_bytes: Optional[int] = None
    cpu_uncapped_millicores: Optional[int] = None
    memory_uncapped_bytes: Optional[int] = None
    #: Labels of the VPA object (Goldilocks provenance: ``creator=Fairwinds``, ``source=goldilocks``).
    labels: Dict[str, str] = field(default_factory=dict)


def _parse_container_recommendation(
    namespace: str, vpa_name: str, target_ref: dict, container: dict, labels: Optional[Dict[str, str]] = None
):
    container_name = container.get("containerName") or ""
    if not container_name:
        return None

    target = container.get("target") or {}
    lower = container.get("lowerBound") or {}
    upper = container.get("upperBound") or {}
    uncapped = container.get("uncappedTarget") or {}

    return VPARecommendation(
        namespace=namespace,
        vpa_name=vpa_name,
        target_kind=target_ref.get("kind") or "",
        target_name=target_ref.get("name") or "",
        container_name=container_name,
        cpu_target_millicores=parse_cpu_quantity(target.get("cpu")),
        memory_target_bytes=parse_memory_quantity(target.get("memory")),
        cpu_lower_millicores=parse_cpu_quantity(lower.get("cpu")),
        memory_lower_bytes=parse_memory_quantity(lower.get("memory")),
        cpu_upper_millicores=parse_cpu_quantity(upper.get("cpu")),
        memory_upper_bytes=parse_memory_quantity(upper.get("memory")),
        cpu_uncapped_millicores=parse_cpu_quantity(uncapped.get("cpu")),
        memory_uncapped_bytes=parse_memory_quantity(uncapped.get("memory")),
        labels={str(k): str(v) for k, v in (labels or {}).items()},
    )


class VPACollector:
    """Collects recommendation-mode VPA container targets."""

    def __init__(self) -> None:
        #: VPAs skipped by the last ``collect()``, by update mode (``Auto``, ``Recreate``...).
        self.skipped_by_mode: Dict[str, int] = {}

    async def collect(self) -> List[VPARecommendation]:
        """Fetches all recommendation-mode VPAs across namespaces.

        Returns:
            A list of VPARecommendation objects. Returns an empty list on any
            error (graceful degradation when the CRD is not installed).
        """
        recommendations: List[VPARecommendation] = []
        self.skipped_by_mode = {}

        api = await get_custom_objects_api()
        if not api:
            logger.debug("CustomObjects API not available; skipping VPA collection.")
            return recommendations

        try:
            result = await api.list_cluster_custom_object(
                group=VPA_GROUP,
                version=VPA_VERSION,
                plural=VPA_PLURAL,
                _request_timeout=global_config.K8S_REQUEST_TIMEOUT or None,
            )
        except Exception as e:
            status = getattr(e, "status", None)
            if status == 404:
                logger.debug("VerticalPodAutoscaler CRD is not installed; skipping VPA collection.")
            else:
                logger.warning("Failed to collect VerticalPodAutoscalers: %s.", e)
            return recommendations

        items = (result or {}).get("items", []) or []
        for item in items:
            try:
                metadata = item.get("metadata") or {}
                spec = item.get("spec") or {}
                status = item.get("status") or {}

                update_policy = spec.get("updatePolicy") or {}
                update_mode = update_policy.get("updateMode") or spec.get("updateMode") or "Off"
                if update_mode != "Off":
                    self.skipped_by_mode[str(update_mode)] = self.skipped_by_mode.get(str(update_mode), 0) + 1
                    logger.debug(
                        "Skipping VPA %s/%s: updateMode is '%s' (GreenKube only reads recommendation mode).",
                        metadata.get("namespace"),
                        metadata.get("name"),
                        update_mode,
                    )
                    continue

                target_ref = spec.get("targetRef") or {}
                if not target_ref.get("name"):
                    continue

                recommendation = status.get("recommendation") or {}
                containers = recommendation.get("containerRecommendations") or []
                for container in containers:
                    parsed = _parse_container_recommendation(
                        metadata.get("namespace") or "",
                        metadata.get("name") or "",
                        target_ref,
                        container,
                        metadata.get("labels") or {},
                    )
                    if parsed:
                        recommendations.append(parsed)
            except Exception as e:
                logger.warning("Skipping malformed VPA object: %s", e)

        logger.info("Collected %d VPA container recommendation(s) from cluster.", len(recommendations))
        return recommendations
