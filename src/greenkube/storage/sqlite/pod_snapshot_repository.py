"""SQLite pod metadata history (WP-01.1, COL-01, migration 0120)."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from typing import Sequence

from greenkube.models.metrics import PodMetric
from greenkube.storage.base_pod_snapshot_repository import (
    PodSnapshot,
    PodSnapshotRepository,
    snapshots_from_pod_metrics,
)
from greenkube.utils.time import ensure_utc

# Fixed-width UTC format so that lexicographic comparison equals chronological comparison.
_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _encode(value: datetime) -> str:
    return ensure_utc(value).strftime(_FORMAT)


class SQLitePodSnapshotRepository(PodSnapshotRepository):
    """Pod snapshots stored in the ``pod_snapshots`` table."""

    def __init__(self, db_manager) -> None:
        self.db_manager = db_manager

    async def save_pods(self, pods: Sequence[PodMetric], seen_at: datetime) -> int:
        """See :meth:`PodSnapshotRepository.save_pods`."""
        snapshots = snapshots_from_pod_metrics(pods, seen_at)
        if not snapshots:
            return 0
        seen = _encode(seen_at)
        async with self.db_manager.connection_scope() as conn:
            await conn.executemany(
                """INSERT INTO pod_snapshots
                    (namespace, pod_name, spec_hash, owner_kind, owner_name, containers, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(namespace, pod_name, spec_hash) DO UPDATE SET
                    first_seen = MIN(pod_snapshots.first_seen, excluded.first_seen),
                    last_seen = MAX(pod_snapshots.last_seen, excluded.last_seen)""",
                [
                    (
                        s.namespace,
                        s.pod_name,
                        s.spec_hash,
                        s.owner_kind,
                        s.owner_name,
                        json.dumps(list(s.containers)),
                        seen,
                        seen,
                    )
                    for s in snapshots
                ],
            )
            await conn.commit()
        return len(snapshots)

    async def get_snapshots(self, start: datetime, end: datetime) -> dict[tuple[str, str], list[PodSnapshot]]:
        """See :meth:`PodSnapshotRepository.get_snapshots`."""
        async with self.db_manager.connection_scope() as conn:
            async with conn.execute(
                """SELECT namespace, pod_name, spec_hash, owner_kind, owner_name, containers, first_seen, last_seen
                FROM pod_snapshots WHERE last_seen >= ? AND first_seen <= ?
                ORDER BY namespace, pod_name, first_seen""",
                (_encode(start), _encode(end)),
            ) as cursor:
                rows = await cursor.fetchall()
        result: dict[tuple[str, str], list[PodSnapshot]] = defaultdict(list)
        for row in rows:
            result[(row[0], row[1])].append(
                PodSnapshot(
                    namespace=row[0],
                    pod_name=row[1],
                    spec_hash=row[2],
                    owner_kind=row[3],
                    owner_name=row[4],
                    containers=tuple(json.loads(row[5])),
                    first_seen=ensure_utc(row[6]),
                    last_seen=ensure_utc(row[7]),
                )
            )
        return dict(result)

    async def prune(self, before: datetime) -> int:
        """See :meth:`PodSnapshotRepository.prune`."""
        async with self.db_manager.connection_scope() as conn:
            cursor = await conn.execute("DELETE FROM pod_snapshots WHERE last_seen < ?", (_encode(before),))
            await conn.commit()
            return cursor.rowcount or 0
