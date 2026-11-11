# src/greenkube/collectors/pv_collector.py
"""
Finds PersistentVolumes that appear unused, with the evidence.

Only volumes whose claim is gone are candidates: phase ``Released`` (the PVC
was deleted and the volume kept, usually by ``reclaimPolicy: Retain``) or
``Failed`` (automatic reclamation failed), with a ``claimRef`` naming a PVC
that no longer exists. A PersistentVolume is never reported when it:

* is ``Available`` or ``Bound`` (static pre-provisioned volumes, volumes
  pre-bound to a claim that is not created yet, volumes in use);
* is opted out (``greenkube.io/ignore: "true"`` or ``greenkube.io/keep``);
* is a ``local`` or ``hostPath`` volume (no billable storage is released and
  deleting the object does not free the disk);
* belongs to a StatefulSet replica (its claim name matches a
  ``volumeClaimTemplate`` of a StatefulSet that still exists, for example
  scaled to zero): the data is expected back when the StatefulSet scales up;
* backs a VolumeSnapshot (its claim is a snapshot source, or a
  VolumeSnapshotContent points at its volume handle);
* a PVC with the same name exists again (it may be rebound by an operator);
* depends on an API that could not be read (RBAC, timeout).

The recommendation is emitted only once the condition has persisted for
``DELETION_MIN_UNUSED_DAYS`` (``greenkube.collectors.deletion_candidates``).
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple

from greenkube.collectors.k8s_object_reader import KubernetesObjectReader, ObjectList
from greenkube.collectors.opencost_collector import OpenCostCollector
from greenkube.core.deletion_safety import opt_out_reason
from greenkube.utils.k8s_utils import parse_storage_request

logger = logging.getLogger(__name__)

_CANDIDATE_PHASES = frozenset({"released", "failed"})


@dataclass
class OrphanedPV:
    """A PersistentVolume whose claim is gone and that appears unused, with the evidence."""

    name: str
    phase: str
    capacity_bytes: int
    claim_namespace: Optional[str] = None
    claim_name: Optional[str] = None
    reclaim_policy: Optional[str] = None
    storage_class: Optional[str] = None
    annual_cost: Optional[float] = None
    uid: str = ""
    #: Facts that show the volume is unused (shown in the recommendation).
    evidence: List[str] = field(default_factory=list)
    #: When GreenKube first saw it unused (None: not persisted, never recommended).
    first_seen_at: Optional[datetime] = None


@dataclass
class PersistentVolumeScan:
    """Outcome of one PersistentVolume evaluation."""

    candidates: List[OrphanedPV] = field(default_factory=list)
    skipped: Dict[str, str] = field(default_factory=dict)
    #: True when every PersistentVolume was evaluated (PVs and PVCs could be listed).
    complete: bool = False


def _metadata(obj: Mapping[str, Any]) -> Mapping[str, Any]:
    return obj.get("metadata") or {}


def static_exclusion(pv: Mapping[str, Any]) -> Optional[str]:
    """Why a PersistentVolume is never a deletion candidate, from its own manifest."""
    spec = pv.get("spec") or {}
    phase = str((pv.get("status") or {}).get("phase") or "")
    if phase.lower() not in _CANDIDATE_PHASES:
        return f"phase {phase or 'unknown'}: only Released or Failed volumes can be unused"
    opt_out = opt_out_reason(_metadata(pv))
    if opt_out:
        return f"opted out ({opt_out})"
    claim_ref = spec.get("claimRef") or {}
    if not claim_ref.get("name"):
        return "no claim reference: a volume kept for manual rebinding"
    if spec.get("local") or spec.get("hostPath"):
        return "local or hostPath volume: no billable storage is released"
    return None


def statefulset_owner(statefulsets: List[Mapping[str, Any]], claim_name: str) -> Optional[str]:
    """The StatefulSet whose volumeClaimTemplates produce ``claim_name`` (``<template>-<set>-<ordinal>``)."""
    for statefulset in statefulsets:
        set_name = _metadata(statefulset).get("name") or ""
        for template in (statefulset.get("spec") or {}).get("volumeClaimTemplates") or []:
            template_name = _metadata(template).get("name") or ""
            if template_name and re.fullmatch(rf"{re.escape(template_name)}-{re.escape(set_name)}-\d+", claim_name):
                return set_name
    return None


def snapshot_reference(
    snapshots: List[Mapping[str, Any]],
    contents: List[Mapping[str, Any]],
    claim_namespace: str,
    claim_name: str,
    volume_handle: Optional[str],
) -> Optional[str]:
    for snapshot in snapshots:
        metadata = _metadata(snapshot)
        source = (snapshot.get("spec") or {}).get("source") or {}
        if metadata.get("namespace") == claim_namespace and source.get("persistentVolumeClaimName") == claim_name:
            return f"VolumeSnapshot {claim_namespace}/{metadata.get('name')} was taken from its claim"
    if volume_handle:
        for content in contents:
            source = (content.get("spec") or {}).get("source") or {}
            if source.get("volumeHandle") == volume_handle:
                return f"VolumeSnapshotContent {_metadata(content).get('name')} depends on its volume"
    return None


class PVCollector:
    """Evaluates every PersistentVolume and returns those that appear unused."""

    def __init__(self, reader: Any = None):
        self.reader = reader

    async def collect(self) -> Optional[List[OrphanedPV]]:
        """Candidates of one complete evaluation, or None when nothing could be proven (any error)."""
        scan = await self.scan()
        return scan.candidates if scan.complete else None

    async def scan(self) -> PersistentVolumeScan:
        result = PersistentVolumeScan()
        reader = self.reader or KubernetesObjectReader()
        try:
            volumes = await reader.list("persistentvolumes")
            claims = await reader.list("persistentvolumeclaims")
            if volumes.status != "ok" or claims.status != "ok":
                logger.warning(
                    "Cannot list PersistentVolumes or PersistentVolumeClaims (%s); "
                    "no PV cleanup recommendations this scan.",
                    volumes.detail or claims.detail,
                )
                return result
            result.complete = True
            existing = {(_metadata(claim).get("namespace"), _metadata(claim).get("name")) for claim in claims.items}
            for pv in volumes.items:
                name = _metadata(pv).get("name")
                if not name:
                    continue
                reason = static_exclusion(pv)
                candidate: Optional[OrphanedPV] = None
                if reason is None:
                    candidate, reason = await self._evaluate(reader, pv, existing)
                if candidate is not None:
                    result.candidates.append(candidate)
                else:
                    result.skipped[name] = reason or "in use"
            logger.info(
                "Evaluated PersistentVolumes: %d appear unused, %d in use or unproven.",
                len(result.candidates),
                len(result.skipped),
            )
        except Exception as e:
            logger.warning(
                "Failed to evaluate PersistentVolumes: %s. Continuing without PV cleanup recommendations.",
                e,
            )
            return PersistentVolumeScan()
        return result

    async def _evaluate(self, reader: Any, pv: Mapping[str, Any], existing: set) -> Tuple[Optional[OrphanedPV], str]:
        metadata = _metadata(pv)
        spec = pv.get("spec") or {}
        phase = str((pv.get("status") or {}).get("phase") or "")
        claim_ref = spec.get("claimRef") or {}
        claim_namespace = claim_ref.get("namespace") or ""
        claim_name = claim_ref.get("name") or ""

        def unproven(listing: ObjectList) -> str:
            return f"cannot prove it is unused: {listing.kind} unreadable ({listing.detail})"

        if (claim_namespace, claim_name) in existing:
            return None, f"a PVC {claim_namespace}/{claim_name} exists again and may rebind it"

        statefulsets = await reader.list("statefulsets", claim_namespace)
        if not statefulsets.known:
            return None, unproven(statefulsets)
        owner = statefulset_owner(statefulsets.items, claim_name)
        if owner:
            return None, f"its claim belongs to StatefulSet {claim_namespace}/{owner}, which may scale up again"

        snapshots = await reader.list("volumesnapshots", claim_namespace)
        contents = await reader.list("volumesnapshotcontents")
        for listing in (snapshots, contents):
            if not listing.known:
                return None, unproven(listing)
        volume_handle = (spec.get("csi") or {}).get("volumeHandle")
        reference = snapshot_reference(snapshots.items, contents.items, claim_namespace, claim_name, volume_handle)
        if reference:
            return None, reference

        reclaim_policy = spec.get("persistentVolumeReclaimPolicy")
        evidence = [
            f"phase {phase}: its claim {claim_namespace}/{claim_name} was deleted",
            f"no PVC named {claim_namespace}/{claim_name} exists",
            "no StatefulSet volumeClaimTemplate produces that claim name",
            "no VolumeSnapshot or VolumeSnapshotContent depends on it",
        ]
        if str(reclaim_policy).lower() == "retain":
            evidence.append(
                "reclaim policy Retain: deleting the PersistentVolume object does not delete the disk; "
                "release it in the storage backend once its data is no longer needed"
            )
        capacity = spec.get("capacity") or {}
        return (
            OrphanedPV(
                name=metadata.get("name") or "",
                phase=phase,
                capacity_bytes=parse_storage_request(capacity.get("storage")) if capacity.get("storage") else 0,
                claim_namespace=claim_namespace or None,
                claim_name=claim_name or None,
                reclaim_policy=reclaim_policy,
                storage_class=spec.get("storageClassName"),
                uid=metadata.get("uid") or "",
                evidence=evidence,
            ),
            "",
        )


async def enrich_orphaned_pv_costs(
    volumes: List[OrphanedPV],
    window_days: int = 7,
    opencost: Optional[OpenCostCollector] = None,
) -> List[OrphanedPV]:
    """Attach real annualized OpenCost storage costs to unused PVs when available.

    Volumes that OpenCost has no cost data for keep ``annual_cost=None`` so the
    recommender falls back to the capacity-based estimate.

    Args:
        volumes: The PV descriptors to enrich (mutated in place).
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
                "Attached real OpenCost costs to %d of %d unused PersistentVolumes.",
                enriched_count,
                len(volumes),
            )
        else:
            logger.debug("OpenCost has no cost data for the unused PersistentVolumes; using estimates.")
    except Exception as e:
        logger.warning("Could not enrich PV costs from OpenCost: %s. Using capacity-based estimates.", e)
    finally:
        if opencost is None:
            await collector.close()

    return volumes
