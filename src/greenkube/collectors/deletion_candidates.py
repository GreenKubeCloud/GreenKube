# src/greenkube/collectors/deletion_candidates.py
"""Persistence of deletion recommendations.

:func:`attach_first_seen` records each complete LoadBalancer or
PersistentVolume evaluation in ``deletion_candidates`` and gives every
candidate the time since which it has continuously looked unused. Without a
database (or when the write fails) candidates keep ``first_seen_at=None`` and
the analyzers recommend nothing: an unproven duration is not evidence.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, List, Optional, Sequence

logger = logging.getLogger(__name__)

LOAD_BALANCER_KIND = "LoadBalancerService"
PERSISTENT_VOLUME_KIND = "PersistentVolume"


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
