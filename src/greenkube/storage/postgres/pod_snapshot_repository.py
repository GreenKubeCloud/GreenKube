"""PostgreSQL pod metadata history (WP-01.1, COL-01, migrations 0120 and 0121)."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from typing import Iterable, Sequence

from greenkube.models.metrics import PodMetric
from greenkube.storage.base_pod_snapshot_repository import (
    LatestVersion,
    PodSnapshot,
    PodSnapshotRepository,
    continues_version,
    snapshots_from_pod_metrics,
)
from greenkube.utils.time import ensure_utc

_SELECT = """SELECT namespace, pod_name, spec_hash, owner_kind, owner_name, containers::text AS containers,
       first_seen, last_seen, pod_uid, cluster_uid
FROM pod_snapshots"""


def _group(rows) -> dict[tuple[str, str], list[PodSnapshot]]:
    result: dict[tuple[str, str], list[PodSnapshot]] = defaultdict(list)
    for row in rows:
        result[(row["namespace"], row["pod_name"])].append(
            PodSnapshot(
                namespace=row["namespace"],
                pod_name=row["pod_name"],
                spec_hash=row["spec_hash"],
                owner_kind=row["owner_kind"],
                owner_name=row["owner_name"],
                containers=tuple(json.loads(row["containers"])),
                first_seen=ensure_utc(row["first_seen"]),
                last_seen=ensure_utc(row["last_seen"]),
                pod_uid=row["pod_uid"],
                cluster_uid=row["cluster_uid"],
            )
        )
    return dict(result)


class PostgresPodSnapshotRepository(PodSnapshotRepository):
    """Pod snapshots stored in the ``pod_snapshots`` table."""

    def __init__(self, db_manager) -> None:
        self.db_manager = db_manager

    async def save_pods(self, pods: Sequence[PodMetric], seen_at: datetime, *, cluster_uid: str | None = None) -> int:
        """See :meth:`PodSnapshotRepository.save_pods`."""
        snapshots = snapshots_from_pod_metrics(pods, seen_at)
        if not snapshots:
            return 0
        seen = ensure_utc(seen_at)
        async with self.db_manager.connection_scope() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """SELECT DISTINCT ON (namespace, pod_name) namespace, pod_name, id, spec_hash, pod_uid
                    FROM pod_snapshots ORDER BY namespace, pod_name, id DESC"""
                )
                latest = {
                    (row["namespace"], row["pod_name"]): LatestVersion(row["id"], row["spec_hash"], row["pod_uid"])
                    for row in rows
                }
                updates = []
                inserts = []
                for s in snapshots:
                    current = latest.get((s.namespace, s.pod_name))
                    if current is not None and continues_version(current, s):
                        updates.append((seen, s.pod_uid, current.version_id))
                    else:
                        inserts.append(
                            (
                                s.namespace,
                                s.pod_name,
                                s.spec_hash,
                                s.owner_kind,
                                s.owner_name,
                                json.dumps(list(s.containers)),
                                seen,
                                s.pod_uid,
                                cluster_uid,
                            )
                        )
                if updates:
                    await conn.executemany(
                        """UPDATE pod_snapshots SET
                            first_seen = LEAST(first_seen, $1),
                            last_seen = GREATEST(last_seen, $1),
                            pod_uid = COALESCE(pod_uid, $2)
                        WHERE id = $3""",
                        updates,
                    )
                if inserts:
                    await conn.executemany(
                        """INSERT INTO pod_snapshots
                            (namespace, pod_name, spec_hash, owner_kind, owner_name, containers,
                             first_seen, last_seen, pod_uid, cluster_uid)
                        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $7, $8, $9)""",
                        inserts,
                    )
        return len(snapshots)

    async def get_snapshots(self, start: datetime, end: datetime) -> dict[tuple[str, str], list[PodSnapshot]]:
        """See :meth:`PodSnapshotRepository.get_snapshots`."""
        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch(
                f"""{_SELECT} WHERE last_seen >= $1 AND first_seen <= $2
                ORDER BY namespace, pod_name, first_seen, id""",
                ensure_utc(start),
                ensure_utc(end),
            )
        return _group(rows)

    async def get_pod_versions(
        self, pods: Iterable[tuple[str, str]], end: datetime
    ) -> dict[tuple[str, str], list[PodSnapshot]]:
        """See :meth:`PodSnapshotRepository.get_pod_versions`."""
        wanted = set(pods)
        if not wanted:
            return {}
        namespaces, names = zip(*sorted(wanted), strict=True)
        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch(
                f"""{_SELECT}
                WHERE (namespace, pod_name) IN (SELECT * FROM unnest($1::text[], $2::text[]))
                  AND first_seen <= $3
                ORDER BY namespace, pod_name, first_seen, id""",
                list(namespaces),
                list(names),
                ensure_utc(end),
            )
        return _group(rows)

    async def prune(self, before: datetime) -> int:
        """See :meth:`PodSnapshotRepository.prune`."""
        async with self.db_manager.connection_scope() as conn:
            status = await conn.execute("DELETE FROM pod_snapshots WHERE last_seen < $1", ensure_utc(before))
        try:
            return int(str(status).rsplit(" ", 1)[-1])
        except ValueError:
            return 0
