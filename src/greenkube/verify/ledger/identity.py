# src/greenkube/verify/ledger/identity.py
"""Cluster identity for ledger and change rows (roadmap N9).

``CLUSTER_NAME`` is a label: it defaults to a node name or ``default`` and two
clusters can share it. The ledger chain is keyed by ``cluster_uid`` instead:
the UID of the ``kube-system`` namespace, which Kubernetes assigns once per
cluster and never changes (RBAC: ``get`` on namespaces, already granted).

The identity is stored next to ``CLUSTER_NAME`` in ``cluster_identities`` the
first time it is resolved and never replaced, so the chain key stays stable
even if a later read would answer differently. Without Kubernetes access (a
local run, the demo) or when the read is forbidden, the identity falls back to
``name:<CLUSTER_NAME>`` (source ``cluster_name``). A transient error (API
server unreachable, timeout) resolves nothing: the caller skips the ledger
write and retries later rather than writing under a key it would have to
abandon (fail closed).

Signal ids and recommendation fingerprints keep their own cluster key
(``core/optimization/registry.py``): this identity is used only by ledger and
change rows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

SOURCE_KUBE_SYSTEM = "kube-system"
SOURCE_CLUSTER_NAME = "cluster_name"

#: cluster name -> cluster uid resolved by this process.
_RESOLVED: Dict[str, str] = {}


class ClusterIdentityUnavailable(RuntimeError):
    """The cluster UID could not be read right now (transient); retry later."""


@dataclass(frozen=True)
class ClusterIdentity:
    cluster_name: str
    cluster_uid: str
    source: str


def fallback_uid(cluster_name: str) -> str:
    return f"name:{cluster_name or 'default'}"


def cached_cluster_uid(cluster_name: str) -> Optional[str]:
    """The identity this process already resolved for ``cluster_name`` (never reads anything)."""
    return _RESOLVED.get(cluster_name or "default")


def forget_cached_identities() -> None:
    """Testing hook."""
    _RESOLVED.clear()


async def read_kube_system_uid() -> Optional[str]:
    """UID of the ``kube-system`` namespace; ``None`` when there is no cluster or the read is forbidden.

    Raises:
        ClusterIdentityUnavailable: any other failure (the read may succeed later).
    """
    try:
        from greenkube.core.k8s_client import get_core_v1_api

        api = await get_core_v1_api()
    except Exception as exc:  # pragma: no cover - configuration errors depend on the host
        logger.info("No Kubernetes client for the cluster identity: %s", exc)
        return None
    if api is None:
        return None
    try:
        namespace = await api.read_namespace("kube-system")
    except Exception as exc:
        status = getattr(exc, "status", None)
        if status in (401, 403, 404):
            logger.warning("Cannot read the kube-system namespace (HTTP %s): using the cluster name.", status)
            return None
        raise ClusterIdentityUnavailable(f"kube-system namespace could not be read: {exc}") from exc
    uid = getattr(getattr(namespace, "metadata", None), "uid", None)
    return str(uid) if uid else None


class ClusterIdentityResolver:
    """Resolves and stores the ledger identity of ``cluster_name``."""

    def __init__(
        self,
        repo,
        cluster_name: str,
        *,
        reader: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
    ) -> None:
        self.repo = repo
        self.cluster_name = cluster_name or "default"
        self._reader = reader if reader is not None else read_kube_system_uid

    async def resolve(self) -> ClusterIdentity:
        """The stored identity, or a newly resolved one (stored before it is returned)."""
        stored = await self.repo.get_identity(self.cluster_name)
        if stored is None:
            uid = await self._reader()
            source = SOURCE_KUBE_SYSTEM if uid else SOURCE_CLUSTER_NAME
            stored = await self.repo.claim_identity(self.cluster_name, uid or fallback_uid(self.cluster_name), source)
            logger.info("Ledger cluster identity for %s: %s (%s).", self.cluster_name, stored[0], stored[1])
        _RESOLVED[self.cluster_name] = stored[0]
        return ClusterIdentity(self.cluster_name, stored[0], stored[1])


__all__ = [
    "ClusterIdentity",
    "ClusterIdentityResolver",
    "ClusterIdentityUnavailable",
    "SOURCE_CLUSTER_NAME",
    "SOURCE_KUBE_SYSTEM",
    "cached_cluster_uid",
    "fallback_uid",
    "forget_cached_identities",
    "read_kube_system_uid",
]
