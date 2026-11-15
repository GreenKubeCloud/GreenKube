"""PostgreSQL pod metadata history (WP-01.1, COL-01, migration 0120)."""

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


class PostgresPodSnapshotRepository(PodSnapshotRepository):
    """Pod snapshots stored in the ``pod_snapshots`` table."""

    def __init__(self, db_manager) -> None:
        self.db_manager = db_manager

    async def save_pods(self, pods: Sequence[PodMetric], seen_at: datetime) -> int:
        """See :meth:`PodSnapshotRepository.save_pods`."""
        snapshots = snapshots_from_pod_metrics(pods, seen_at)
        if not snapshots:
            return 0
        seen = ensure_utc(seen_at)
        async with self.db_manager.connection_scope() as conn:
            await conn.executemany(
                """INSERT INTO pod_snapshots
                    (namespace, pod_name, spec_hash, owner_kind, owner_name, containers, first_seen, last_seen)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $7)
                ON CONFLICT (namespace, pod_name, spec_hash) DO UPDATE SET
                    first_seen = LEAST(pod_snapshots.first_seen, EXCLUDED.first_seen),
                    last_seen = GREATEST(pod_snapshots.last_seen, EXCLUDED.last_seen)""",
                [
                    (
                        s.namespace,
                        s.pod_name,
                        s.spec_hash,
                        s.owner_kind,
                        s.owner_name,
                        json.dumps(list(s.containers)),
                        seen,
                    )
                    for s in snapshots
                ],
            )
        return len(snapshots)

    async def get_snapshots(self, start: datetime, end: datetime) -> dict[tuple[str, str], list[PodSnapshot]]:
        """See :meth:`PodSnapshotRepository.get_snapshots`."""
        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch(
                """SELECT namespace, pod_name, spec_hash, owner_kind, owner_name, containers::text AS containers,
                       first_seen, last_seen
                FROM pod_snapshots WHERE last_seen >= $1 AND first_seen <= $2
                ORDER BY namespace, pod_name, first_seen""",
                ensure_utc(start),
                ensure_utc(end),
            )
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
                )
            )
        return dict(result)

    async def prune(self, before: datetime) -> int:
        """See :meth:`PodSnapshotRepository.prune`."""
        async with self.db_manager.connection_scope() as conn:
            status = await conn.execute("DELETE FROM pod_snapshots WHERE last_seen < $1", ensure_utc(before))
        try:
            return int(str(status).rsplit(" ", 1)[-1])
        except ValueError:
            return 0
