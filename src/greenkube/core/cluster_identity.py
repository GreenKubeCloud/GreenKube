"""Stable installation identity for collection watermarks and records (COL-02, STO-09).

``CLUSTER_NAME`` is a display label: it may be auto-detected as ``default``,
changed in Helm values, or differ between pods.  The collection watermark and
the ``collection_windows`` records are keyed by the installation identity
instead: the UID of the ``kube-system`` namespace (assigned once per cluster,
never changed), stored in the single-row ``installation`` table the first time
the controller resolves it.  Without Kubernetes access (a local run, the demo)
the identity is ``name:<CLUSTER_NAME>``.

At every start the live UID is compared with the stored one.  A different
UID means the database belongs to another cluster (two installs sharing one
schema, or a database restored into another cluster): collection refuses to
start, unless ``ALLOW_CLUSTER_UID_CHANGE`` is set, which adopts the new UID.
A transient read failure is retried with backoff; if it persists the
collection does not start (fail closed) rather than writing under a key it
would have to abandon.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from greenkube.storage.installation_repository import (
    AdoptedKeys,
    InstallationIdentity,
    InstallationRepository,
)

logger = logging.getLogger(__name__)

SOURCE_KUBE_SYSTEM = "kube-system"
SOURCE_CLUSTER_NAME = "cluster_name"

#: Identity resolved by this process (one installation per process).
_RESOLVED: dict[str, InstallationIdentity] = {}


class InstallationIdentityError(RuntimeError):
    """The installation identity cannot be used: collection must not start."""


class ClusterIdentityMismatch(InstallationIdentityError):
    """The database belongs to another cluster (different kube-system UID)."""


class InstallationIdentityUnavailable(InstallationIdentityError):
    """The kube-system UID could not be read after every retry."""


def fallback_uid(cluster_name: str | None) -> str:
    """Identity without Kubernetes access: ``name:<CLUSTER_NAME>``."""
    return f"name:{cluster_name or 'default'}"


async def _default_reader() -> str | None:
    # Read-only use of the ledger's kube-system reader (verify/ledger/identity.py).
    from greenkube.verify.ledger.identity import read_kube_system_uid

    return await read_kube_system_uid()


def _is_transient(error: Exception) -> bool:
    from greenkube.verify.ledger.identity import ClusterIdentityUnavailable

    return isinstance(error, ClusterIdentityUnavailable)


async def resolve_installation_identity(
    repo: InstallationRepository,
    cluster_name: str | None,
    *,
    allow_change: bool = False,
    reader: Callable[[], Awaitable[str | None]] | None = None,
    attempts: int = 5,
    backoff_seconds: float = 1.0,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> InstallationIdentity:
    """Resolve, check and store the installation identity.

    Raises:
        InstallationIdentityUnavailable: the kube-system UID stayed unreadable.
        ClusterIdentityMismatch: the stored identity is another cluster's UID.
    """
    read = reader or _default_reader
    live: str | None = None
    for attempt in range(1, max(attempts, 1) + 1):
        try:
            live = await read()
            break
        except Exception as error:
            if not _is_transient(error) and reader is None:
                raise
            if attempt >= attempts:
                raise InstallationIdentityUnavailable(
                    f"The kube-system namespace UID could not be read after {attempts} attempt(s): {error}"
                ) from error
            delay = backoff_seconds * 2 ** (attempt - 1)
            logger.warning("Cluster identity not readable yet (%s); retrying in %.0f s.", error, delay)
            await sleep(delay)

    stored = await repo.get()
    if stored is None:
        identity = InstallationIdentity(
            cluster_uid=live or fallback_uid(cluster_name),
            cluster_name=cluster_name,
            source=SOURCE_KUBE_SYSTEM if live else SOURCE_CLUSTER_NAME,
        )
        stored = await repo.claim(identity)
        logger.info("Installation identity: %s (%s).", stored.cluster_uid, stored.source)
    elif live and stored.cluster_uid != live:
        if stored.source == SOURCE_CLUSTER_NAME:
            # First start with Kubernetes access after a run without it: upgrade the key.
            stored = await repo.replace(InstallationIdentity(live, cluster_name, SOURCE_KUBE_SYSTEM))
            logger.info("Installation identity upgraded from the cluster name to kube-system UID %s.", live)
        elif allow_change:
            logger.warning(
                "ALLOW_CLUSTER_UID_CHANGE: this database belonged to cluster %s; it now belongs to %s.",
                stored.cluster_uid,
                live,
            )
            stored = await repo.replace(InstallationIdentity(live, cluster_name, SOURCE_KUBE_SYSTEM))
        else:
            raise ClusterIdentityMismatch(
                f"This database belongs to cluster {stored.cluster_uid} ({stored.cluster_name or 'unnamed'}), "
                f"but this controller runs in cluster {live}. Use one database schema per cluster "
                "(DB_SCHEMA), or set ALLOW_CLUSTER_UID_CHANGE=true once to adopt this database."
            )
    _RESOLVED["installation"] = stored
    return stored


def cached_installation_identity() -> InstallationIdentity | None:
    """The identity this process resolved, if any (never reads anything)."""
    return _RESOLVED.get("installation")


def forget_installation_identity() -> None:
    """Testing hook."""
    _RESOLVED.clear()


async def adopt_legacy_collection_keys(repo: InstallationRepository, cluster_uid: str, source: str) -> AdoptedKeys:
    """Merge watermarks and window records written under names into the identity, and log it."""
    adopted = await repo.adopt_legacy_keys(cluster_uid, source)
    if adopted.keys:
        logger.warning(
            "Adopted legacy collection key(s) %s into installation identity %s (watermark %s).",
            ", ".join(adopted.keys),
            cluster_uid,
            adopted.watermark.isoformat() if adopted.watermark else None,
        )
    return adopted
