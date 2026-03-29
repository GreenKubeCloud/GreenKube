# src/greenkube/collectors/hpa_collector.py
"""
Collects HorizontalPodAutoscaler (HPA) targets from the Kubernetes API.

Used to detect which workloads already have autoscaling configured,
so the recommender can skip autoscaling recommendations for them.
"""

import logging
from typing import Set, Tuple

from greenkube.core.config import config as global_config
from greenkube.core.k8s_client import get_autoscaling_v2_api

logger = logging.getLogger(__name__)


class HPACollector:
    """Collects HPA targets from the Kubernetes API.

    Returns a set of (namespace, target_kind, target_name) tuples
    representing workloads already governed by an HPA.
    """

    async def collect(self) -> Set[Tuple[str, str, str]]:
        """Fetches all HPAs and extracts their scale target references.

        Returns:
            A set of (namespace, target_kind, target_name) tuples.
            Returns an empty set on any error (graceful degradation).
        """
        targets: Set[Tuple[str, str, str]] = set()

        try:
            api = await get_autoscaling_v2_api()
            if not api:
                logger.debug("AutoscalingV2 API not available; skipping HPA collection.")
                return targets

            hpa_list = await api.list_horizontal_pod_autoscaler_for_all_namespaces(
                _request_timeout=global_config.K8S_REQUEST_TIMEOUT or None
            )

            for hpa in hpa_list.items:
                namespace = hpa.metadata.namespace
                ref = hpa.spec.scale_target_ref
                if ref and ref.kind and ref.name:
                    targets.add((namespace, ref.kind, ref.name))
                    logger.debug(
                        "Found HPA targeting %s/%s in namespace %s",
                        ref.kind,
                        ref.name,
                        namespace,
                    )

            logger.info("Collected %d HPA targets from cluster.", len(targets))
        except Exception as e:
            logger.warning("Failed to collect HPA targets: %s. Continuing without HPA filtering.", e)

        return targets
