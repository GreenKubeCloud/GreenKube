# src/greenkube/collectors/deletion_candidates.py
"""Persistence and namespace inputs of deletion recommendations.

* :func:`attach_first_seen` records each complete LoadBalancer or
  PersistentVolume evaluation in ``deletion_candidates`` and gives every
  candidate the time since which it has continuously looked unused. Without
  a database (or when the write fails) candidates keep ``first_seen_at=None``
  and the analyzers recommend nothing: an unproven duration is not evidence.
* :func:`collect_namespace_info` reads namespace metadata (opt-out
  annotations, GitOps labels, phase, age) for the idle-namespace, zombie and
  off-peak safety rules. ``None`` means the namespaces could not be read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence

from greenkube.collectors.k8s_object_reader import KubernetesObjectReader
from greenkube.utils.time import ensure_utc

logger = logging.getLogger(__name__)

LOAD_BALANCER_KIND = "LoadBalancerService"
PERSISTENT_VOLUME_KIND = "PersistentVolume"

#: Labels and annotations that show a namespace is managed by a GitOps tool.
_GITOPS_MARKERS = (
    ("label", "argocd.argoproj.io/instance", "Argo CD"),
    ("annotation", "argocd.argoproj.io/tracking-id", "Argo CD"),
    ("label", "kustomize.toolkit.fluxcd.io/name", "Flux"),
    ("label", "helm.toolkit.fluxcd.io/name", "Flux"),
)


@dataclass
class NamespaceInfo:
    """Metadata of a namespace used by the deletion safety rules."""

    name: str
    labels: Dict[str, str] = field(default_factory=dict)
    annotations: Dict[str, str] = field(default_factory=dict)
    phase: str = "Active"
    created_at: Optional[datetime] = None

    @property
    def metadata(self) -> Dict[str, Any]:
        return {"labels": self.labels, "annotations": self.annotations}

    @property
    def gitops_manager(self) -> Optional[str]:
        for source, key, manager in _GITOPS_MARKERS:
            values = self.labels if source == "label" else self.annotations
            if key in values:
                return f"{manager} ({source} {key}={values[key]})"
        return None


async def attach_first_seen(
    repository: Any,
    resource_kind: str,
    candidates: Optional[Sequence[Any]],
    now: Optional[datetime] = None,
) -> Optional[List[Any]]:
    """Record a complete evaluation and set ``first_seen_at`` on each candidate.

    ``candidates=None`` (incomplete evaluation) records nothing and keeps the
    stored first-seen times, so a transient API failure does not restart the
    clock (a gap longer than the repository's ``max_gap`` does).
    """
    if candidates is None:
        return None
    items = list(candidates)
    if repository is None:
        if items:
            logger.info(
                "No database for deletion-candidate history: %d %s candidate(s) are not recommended "
                "because their unused duration cannot be proven.",
                len(items),
                resource_kind,
            )
        return items
    from greenkube.storage.deletion_candidate_repository import CandidateObservation

    observations = [
        CandidateObservation(
            # PersistentVolumes are cluster-scoped.
            namespace=(getattr(item, "namespace", None) or "") if resource_kind == LOAD_BALANCER_KIND else "",
            name=item.name,
            uid=getattr(item, "uid", "") or "",
            evidence=list(getattr(item, "evidence", []) or []),
        )
        for item in items
    ]
    try:
        first_seen = await repository.observe(resource_kind, observations, now=now)
    except Exception as exc:
        logger.warning("Could not record %s deletion candidates: %s. Not recommending them.", resource_kind, exc)
        return items
    for item, observation in zip(items, observations):
        item.first_seen_at = first_seen.get((observation.namespace, observation.name))
    return items


def _namespace_info(item: Mapping[str, Any]) -> Optional[NamespaceInfo]:
    metadata = item.get("metadata") or {}
    name = metadata.get("name")
    if not name:
        return None
    created = metadata.get("creationTimestamp")
    created_at: Optional[datetime] = None
    if isinstance(created, datetime):
        created_at = ensure_utc(created)
    elif isinstance(created, str) and created:
        try:
            created_at = ensure_utc(created)
        except ValueError:
            created_at = None
    return NamespaceInfo(
        name=name,
        labels={str(k): str(v) for k, v in (metadata.get("labels") or {}).items()},
        annotations={str(k): str(v) for k, v in (metadata.get("annotations") or {}).items()},
        phase=str((item.get("status") or {}).get("phase") or "Active"),
        created_at=created_at,
    )


async def collect_namespace_info(reader: Any = None) -> Optional[Dict[str, NamespaceInfo]]:
    """Namespace metadata by name, or None when namespaces cannot be read."""
    try:
        listing = await (reader or KubernetesObjectReader()).list("namespaces")
    except Exception as exc:
        logger.warning("Could not read namespaces for deletion safety: %s", exc)
        return None
    if listing.status != "ok":
        logger.info("Namespaces unreadable (%s): no idle-namespace recommendations this scan.", listing.detail)
        return None
    result: Dict[str, NamespaceInfo] = {}
    for item in listing.items:
        info = _namespace_info(item)
        if info is not None:
            result[info.name] = info
    return result
