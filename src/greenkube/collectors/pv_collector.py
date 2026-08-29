# src/greenkube/collectors/pv_collector.py
"""
Collects orphaned PersistentVolumes (PVs) from the Kubernetes API.

Used to detect volumes that no longer have a live bound claim, so the
recommender can suggest deleting them and freeing provisioned storage.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

from greenkube.collectors.opencost_collector import OpenCostCollector
from greenkube.core.config import config as global_config
from greenkube.core.k8s_client import get_core_v1_api
from greenkube.utils.k8s_utils import parse_storage_request

logger = logging.getLogger(__name__)


@dataclass
class OrphanedPV:
    """Describes a PersistentVolume that no longer has a live bound claim."""

    name: str
    phase: str
    capacity_bytes: int
    claim_namespace: Optional[str] = None
    claim_name: Optional[str] = None
    reclaim_policy: Optional[str] = None
    storage_class: Optional[str] = None
    annual_cost: Optional[float] = None


class PVCollector:
    """Collects orphaned PersistentVolumes from the Kubernetes API.

    A PV is considered orphaned when either:
    - its phase is ``Released`` (the PVC was deleted but the volume was not
      reclaimed, typically because the reclaim policy is ``Retain``), or
    - its ``spec.claimRef`` references a PVC that no longer exists in the
      cluster, so the volume can never be bound or used.

    ``Available`` PVs without a claim reference are intentionally skipped:
    they are usually pre-provisioned static volumes and must not be flagged.
    """

    async def collect(self) -> List[OrphanedPV]:
        """Fetches all PVs and PVCs and extracts the orphaned volumes.

        Returns:
            A list of OrphanedPV descriptors.
            Returns an empty list on any error (graceful degradation).
        """
        orphaned: List[OrphanedPV] = []

        try:
            api = await get_core_v1_api()
            if not api:
                logger.debug("CoreV1 API not available; skipping PV collection.")
                return orphaned

            pv_list = await api.list_persistent_volume(_request_timeout=global_config.K8S_REQUEST_TIMEOUT or None)
            pvc_list = await api.list_persistent_volume_claim_for_all_namespaces(
                _request_timeout=global_config.K8S_REQUEST_TIMEOUT or None
            )

            existing_claims: Set[Tuple[str, str]] = set()
            for pvc in pvc_list.items:
                pvc_namespace = pvc.metadata.namespace
                pvc_name = pvc.metadata.name
                if pvc_namespace and pvc_name:
                    existing_claims.add((pvc_namespace, pvc_name))

            for pv in pv_list.items:
                name = pv.metadata.name
                if not name:
                    continue

                spec = pv.spec
                if not spec:
                    continue

                phase = (pv.status.phase if pv.status else None) or ""

                claim_ref = spec.claim_ref
                claim_namespace = claim_ref.namespace if claim_ref else None
                claim_name = claim_ref.name if claim_ref else None

                is_released = phase.lower() == "released"
                claim_missing = bool(claim_ref and (claim_namespace, claim_name) not in existing_claims)
                if not (is_released or claim_missing):
                    continue

                capacity_bytes = 0
                if spec.capacity:
                    capacity_bytes = parse_storage_request(spec.capacity.get("storage"))

                orphaned.append(
                    OrphanedPV(
                        name=name,
                        phase=phase,
                        capacity_bytes=capacity_bytes,
                        claim_namespace=claim_namespace,
                        claim_name=claim_name,
                        reclaim_policy=spec.persistent_volume_reclaim_policy,
                        storage_class=spec.storage_class_name,
                    )
                )
                logger.debug(
                    "Found orphaned PersistentVolume '%s' (phase=%s, claim=%s/%s)",
                    name,
                    phase,
                    claim_namespace,
                    claim_name,
                )

            logger.info("Collected %d orphaned PersistentVolumes from cluster.", len(orphaned))
        except Exception as e:
            logger.warning(
                "Failed to collect orphaned PersistentVolumes: %s. Continuing without PV cleanup recommendations.",
                e,
            )

        return orphaned


async def enrich_orphaned_pv_costs(
    volumes: List[OrphanedPV],
    window_days: int = 7,
    opencost: Optional[OpenCostCollector] = None,
) -> List[OrphanedPV]:
    """Attach real annualized OpenCost storage costs to orphaned PVs when available.

    Volumes that OpenCost has no cost data for keep ``annual_cost=None`` so the
    recommender falls back to the capacity-based estimate.

    Args:
        volumes: The orphaned PV descriptors to enrich (mutated in place).
        window_days: Observation window in days used for the OpenCost query;
                     the reported window cost is annualized against it.
        opencost: Optional OpenCostCollector instance (dependency injection for tests).

    Returns:
        The same list of volumes, enriched in place.
    """
    if not volumes:
        return volumes

    collector = opencost or OpenCostCollector()
    try:
        costs = await collector.collect_pv_costs([v.name for v in volumes], window_days=window_days)
        enriched_count = 0
        if costs:
            for volume in volumes:
                window_cost = costs.get(volume.name)
                if window_cost:
                    volume.annual_cost = window_cost * (365.0 / window_days)
                    enriched_count += 1
        if enriched_count:
            logger.info(
                "Attached real OpenCost costs to %d of %d orphaned PersistentVolumes.",
                enriched_count,
                len(volumes),
            )
        else:
            logger.debug("OpenCost has no cost data for the orphaned PersistentVolumes; using estimates.")
    except Exception as e:
        logger.warning("Could not enrich orphaned PV costs from OpenCost: %s. Using capacity-based estimates.", e)
    finally:
        if opencost is None:
            await collector.close()

    return volumes
