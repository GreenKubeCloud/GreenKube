# src/greenkube/collectors/vpa_collector.py
"""
Collects VerticalPodAutoscaler recommendations from the Kubernetes API.

Only VPAs in recommendation-only mode (``updateMode: Off``) are considered:
GreenKube must never duplicate a controller that already applies its own
recommendations.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional

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


def _parse_container_recommendation(namespace: str, vpa_name: str, target_ref: dict, container: dict):
    container_name = container.get("containerName") or ""
    if not container_name:
        return None

    target = container.get("target") or {}
    lower = container.get("lowerBound") or {}
    upper = container.get("upperBound") or {}

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
    )


class VPACollector:
    """Collects recommendation-mode VPA container targets."""

    async def collect(self) -> List[VPARecommendation]:
        """Fetches all recommendation-mode VPAs across namespaces.

        Returns:
            A list of VPARecommendation objects. Returns an empty list on any
            error (graceful degradation when the CRD is not installed).
        """
        recommendations: List[VPARecommendation] = []

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
                    )
                    if parsed:
                        recommendations.append(parsed)
            except Exception as e:
                logger.warning("Skipping malformed VPA object: %s", e)

        logger.info("Collected %d VPA container recommendation(s) from cluster.", len(recommendations))
        return recommendations
