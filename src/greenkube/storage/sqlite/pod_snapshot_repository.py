"""SQLite pod metadata history (WP-01.1, COL-01, migrations 0120 and 0121)."""

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

# Fixed-width UTC format so that lexicographic comparison equals chronological comparison.
_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_COLUMNS = (
    "namespace, pod_name, spec_hash, owner_kind, owner_name, containers, first_seen, last_seen, pod_uid, cluster_uid"
)
#: Pod names per ``IN (...)`` lookup, below SQLite's bound-parameter limit.
_LOOKUP_CHUNK = 500


def _encode(value: datetime) -> str:
    return ensure_utc(value).strftime(_FORMAT)


def _snapshot(row) -> PodSnapshot:
    return PodSnapshot(
        namespace=row[0],
        pod_name=row[1],
        spec_hash=row[2],
        owner_kind=row[3],
        owner_name=row[4],
        containers=tuple(json.loads(row[5])),
        first_seen=ensure_utc(row[6]),
        last_seen=ensure_utc(row[7]),
        pod_uid=row[8],
        cluster_uid=row[9],
    )


def _group(rows) -> dict[tuple[str, str], list[PodSnapshot]]:
    result: dict[tuple[str, str], list[PodSnapshot]] = defaultdict(list)
    for row in rows:
        result[(row[0], row[1])].append(_snapshot(row))
    return dict(result)


class SQLitePodSnapshotRepository(PodSnapshotRepository):
    """Pod snapshots stored in the ``pod_snapshots`` table."""

    def __init__(self, db_manager) -> None:
        self.db_manager = db_manager

    async def save_pods(self, pods: Sequence[PodMetric], seen_at: datetime, *, cluster_uid: str | None = None) -> int:
        """See :meth:`PodSnapshotRepository.save_pods`."""
        snapshots = snapshots_from_pod_metrics(pods, seen_at)
        if not snapshots:
            return 0
        seen = _encode(seen_at)
        async with self.db_manager.connection_scope() as conn:
            async with conn.execute(
                """SELECT namespace, pod_name, id, spec_hash, pod_uid FROM pod_snapshots
                WHERE id IN (SELECT MAX(id) FROM pod_snapshots GROUP BY namespace, pod_name)"""
            ) as cursor:
                latest = {(row[0], row[1]): LatestVersion(row[2], row[3], row[4]) for row in await cursor.fetchall()}
            updates = []
            inserts = []
            for s in snapshots:
                current = latest.get((s.namespace, s.pod_name))
                if current is not None and continues_version(current, s):
                    updates.append((seen, seen, s.pod_uid, current.version_id))
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
                            seen,
                            s.pod_uid,
                            cluster_uid,
                        )
                    )
            if updates:
                await conn.executemany(
                    """UPDATE pod_snapshots SET
                        first_seen = MIN(first_seen, ?),
                        last_seen = MAX(last_seen, ?),
                        pod_uid = COALESCE(pod_uid, ?)
                    WHERE id = ?""",
                    updates,
                )
            if inserts:
                await conn.executemany(
                    f"INSERT INTO pod_snapshots ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", inserts
                )
            await conn.commit()
        return len(snapshots)

    async def get_snapshots(self, start: datetime, end: datetime) -> dict[tuple[str, str], list[PodSnapshot]]:
        """See :meth:`PodSnapshotRepository.get_snapshots`."""
        async with self.db_manager.connection_scope() as conn:
            async with conn.execute(
                f"""SELECT {_COLUMNS} FROM pod_snapshots WHERE last_seen >= ? AND first_seen <= ?
                ORDER BY namespace, pod_name, first_seen, id""",
                (_encode(start), _encode(end)),
            ) as cursor:
                rows = await cursor.fetchall()
        return _group(rows)

    async def get_pod_versions(
        self, pods: Iterable[tuple[str, str]], end: datetime
    ) -> dict[tuple[str, str], list[PodSnapshot]]:
        """See :meth:`PodSnapshotRepository.get_pod_versions`."""
        wanted = set(pods)
        names = sorted({pod for _namespace, pod in wanted})
        rows = []
        async with self.db_manager.connection_scope() as conn:
            for index in range(0, len(names), _LOOKUP_CHUNK):
                chunk = names[index : index + _LOOKUP_CHUNK]
                placeholders = ", ".join("?" for _ in chunk)
                async with conn.execute(
                    f"""SELECT {_COLUMNS} FROM pod_snapshots
                    WHERE pod_name IN ({placeholders}) AND first_seen <= ?
                    ORDER BY namespace, pod_name, first_seen, id""",
                    (*chunk, _encode(end)),
                ) as cursor:
                    rows.extend(row for row in await cursor.fetchall() if (row[0], row[1]) in wanted)
        return _group(rows)

    async def prune(self, before: datetime) -> int:
        """See :meth:`PodSnapshotRepository.prune`."""
        async with self.db_manager.connection_scope() as conn:
            cursor = await conn.execute("DELETE FROM pod_snapshots WHERE last_seen < ?", (_encode(before),))
            await conn.commit()
            return cursor.rowcount or 0
