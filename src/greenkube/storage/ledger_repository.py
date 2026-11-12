# src/greenkube/storage/ledger_repository.py
"""Storage of the append-only savings ledger v2 on SQLite and PostgreSQL (roadmap C6, C7, N9).

Tables (migration 0079):

* ``savings_ledger_entries`` — INSERT-only (triggers reject UPDATE and DELETE,
  and TRUNCATE on PostgreSQL); a per-cluster hash chain;
* ``ledger_projection_watermarks`` — how far the v1 ledger tables were
  projected into entries, per cluster and source table;
* ``ledger_chain_status`` — the last chain verification per cluster;
* ``cluster_identities`` — the cluster UID stored next to ``CLUSTER_NAME``.

Appends are serialized per cluster (``BEGIN IMMEDIATE`` on SQLite, a
transaction-scoped advisory lock on PostgreSQL): the head of the chain is read,
the entries are hashed after it and inserted in the same transaction, so the
chain never forks and ``seq`` never has a gap. Entries whose ``entry_key``
already exists are skipped before hashing, which makes every append idempotent.

Queries are written once with ``?`` placeholders and translated for PostgreSQL.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from greenkube.utils.time import ensure_utc
from greenkube.verify.ledger.model import (
    BOOK_RESTATEMENT,
    REASON_RESTATEMENT,
    REASON_SUPERSESSION,
    LedgerEntry,
    LedgerEntryDraft,
    format_time,
    parse_time,
    place,
)

logger = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"\?")
_CHUNK = 500

V1_RAW_TABLE = "recommendation_savings_ledger"
V1_HOURLY_TABLE = "recommendation_savings_ledger_hourly"
V1_TABLES = (V1_RAW_TABLE, V1_HOURLY_TABLE)

_COLUMNS = (
    "entry_id",
    "cluster_uid",
    "cluster_name",
    "seq",
    "entry_key",
    "group_key",
    "entry_kind",
    "book",
    "reason",
    "method",
    "quantity",
    "value",
    "unit",
    "currency",
    "recommendation_id",
    "recommendation_ids",
    "namespace",
    "recommendation_type",
    "change_id",
    "measurement_id",
    "pr_url",
    "price_card_version",
    "intensity_g_per_kwh",
    "intensity_source",
    "method_version",
    "confidence",
    "period_start",
    "period_end",
    "slot_start",
    "reverses_entry_id",
    "restates_entry_id",
    "source_table",
    "source_id",
    "created_at",
    "created_by",
    "prev_hash",
    "hash",
)
_TIME_COLUMNS = ("period_start", "period_end", "slot_start", "created_at")
_SELECT = ", ".join(_COLUMNS)
_SELECT_E = ", ".join(f"e.{column}" for column in _COLUMNS)

#: Filters accepted by :meth:`LedgerRepository.list_entries` and the export.
FILTER_COLUMNS = (
    "namespace",
    "method",
    "quantity",
    "entry_kind",
    "book",
    "reason",
    "recommendation_id",
    "change_id",
    "measurement_id",
)


class LedgerConflictError(RuntimeError):
    """Another writer extended the chain at the same position (retry)."""


@dataclass(frozen=True)
class ChainStatus:
    cluster_uid: str
    verified_at: datetime
    ok: bool
    entries: int
    head_seq: Optional[int]
    head_hash: Optional[str]
    failure: Optional[str]


@dataclass(frozen=True)
class SummaryRow:
    method: str
    quantity: str
    unit: str
    book: str
    entry_kind: str
    value: float
    entries: int


@dataclass(frozen=True)
class Provenance:
    change_id: Optional[str] = None
    pr_url: Optional[str] = None
    measurement_id: Optional[str] = None
    method_version: Optional[str] = None
    recommendation_ids: Tuple[int, ...] = ()


class LedgerRepository:
    """Append-only savings ledger entries, their chain and their projections."""

    def __init__(self, db_manager):
        self.db_manager = db_manager

    # ------------------------------------------------------------------
    # Dialect helpers
    # ------------------------------------------------------------------

    @property
    def postgres(self) -> bool:
        return getattr(self.db_manager, "db_type", "sqlite") == "postgres"

    def _sql(self, query: str) -> str:
        if not self.postgres:
            return query
        counter = iter(range(1, 100_000))
        return _PLACEHOLDER.sub(lambda _match: f"${next(counter)}", query)

    def _ts(self, value: Optional[datetime]) -> Any:
        if value is None:
            return None
        return ensure_utc(value) if self.postgres else format_time(value)

    def _true(self, column: str) -> str:
        return column if self.postgres else f"{column} <> 0"

    async def _fetch(self, conn, query: str, params: Sequence[Any] = ()) -> List[dict]:
        sql = self._sql(query)
        if self.postgres:
            return [dict(row) for row in await conn.fetch(sql, *params)]
        cursor = await conn.execute(sql, list(params))
        rows = await cursor.fetchall()
        names = [column[0] for column in cursor.description or []]
        return [dict(zip(names, row)) for row in rows]

    async def _select(self, query: str, params: Sequence[Any] = ()) -> List[dict]:
        async with self.db_manager.connection_scope() as conn:
            return await self._fetch(conn, query, params)

    async def _execute(self, conn, query: str, params: Sequence[Any] = ()) -> int:
        sql = self._sql(query)
        if self.postgres:
            status = await conn.execute(sql, *params)
            try:
                return int(str(status).rsplit(" ", 1)[-1])
            except ValueError:
                return 0
        cursor = await conn.execute(sql, list(params))
        return cursor.rowcount

    def _transaction(self):
        if self.postgres:
            return self.db_manager.transaction()
        return self.db_manager.transaction(immediate=True)

    @staticmethod
    def _entry(row: Mapping[str, Any]) -> LedgerEntry:
        data = dict(row)
        for column in _TIME_COLUMNS:
            data[column] = parse_time(data.get(column))
        raw_ids = data.get("recommendation_ids") or "[]"
        data["recommendation_ids"] = [int(item) for item in json.loads(raw_ids)]
        data["value"] = float(data["value"])
        for column in ("seq", "recommendation_id", "source_id"):
            if data.get(column) is not None:
                data[column] = int(data[column])
        for column in ("confidence", "intensity_g_per_kwh"):
            if data.get(column) is not None:
                data[column] = float(data[column])
        return LedgerEntry(**data)

    def _params(self, entry: LedgerEntry) -> List[Any]:
        values: List[Any] = []
        for column in _COLUMNS:
            value = getattr(entry, column)
            if column in _TIME_COLUMNS:
                value = self._ts(value)
            elif column == "recommendation_ids":
                value = json.dumps(sorted(int(item) for item in value), separators=(",", ":"))
            values.append(value)
        return values

    # ------------------------------------------------------------------
    # Cluster identity (N9)
    # ------------------------------------------------------------------

    async def get_identity(self, cluster_name: str) -> Optional[Tuple[str, str]]:
        rows = await self._select(
            "SELECT cluster_uid, source FROM cluster_identities WHERE cluster_name = ?", [cluster_name]
        )
        return (rows[0]["cluster_uid"], rows[0]["source"]) if rows else None

    async def claim_identity(self, cluster_name: str, cluster_uid: str, source: str) -> Tuple[str, str]:
        """Store the identity unless one exists; returns the stored one (first resolution wins)."""
        async with self.db_manager.transaction() as conn:
            await self._execute(
                conn,
                "INSERT INTO cluster_identities (cluster_name, cluster_uid, source, resolved_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (cluster_name) DO NOTHING",
                [cluster_name, cluster_uid, source, self._ts(datetime.now(timezone.utc))],
            )
            rows = await self._fetch(
                conn, "SELECT cluster_uid, source FROM cluster_identities WHERE cluster_name = ?", [cluster_name]
            )
        return rows[0]["cluster_uid"], rows[0]["source"]

    # ------------------------------------------------------------------
    # Appends
    # ------------------------------------------------------------------

    async def _existing_keys(self, conn, cluster_uid: str, keys: Sequence[str]) -> set[str]:
        found: set[str] = set()
        for offset in range(0, len(keys), _CHUNK):
            chunk = list(keys[offset : offset + _CHUNK])
            placeholders = ", ".join("?" for _ in chunk)
            rows = await self._fetch(
                conn,
                f"SELECT entry_key FROM savings_ledger_entries WHERE cluster_uid = ? AND entry_key IN ({placeholders})",
                [cluster_uid, *chunk],
            )
            found.update(row["entry_key"] for row in rows)
        return found

    async def _head(self, conn, cluster_uid: str) -> Tuple[int, Optional[str]]:
        rows = await self._fetch(
            conn,
            "SELECT seq, hash FROM savings_ledger_entries WHERE cluster_uid = ? ORDER BY seq DESC LIMIT 1",
            [cluster_uid],
        )
        return (int(rows[0]["seq"]), rows[0]["hash"]) if rows else (0, None)

    async def append(
        self,
        cluster_uid: str,
        cluster_name: str,
        drafts: Sequence[LedgerEntryDraft],
        *,
        watermarks: Optional[Mapping[str, int]] = None,
        now: Optional[datetime] = None,
    ) -> List[LedgerEntry]:
        """Chain and insert ``drafts`` (skipping keys already stored) and advance ``watermarks``.

        Returns the entries inserted. Everything happens in one transaction.
        """
        moment = ensure_utc(now) if now is not None else datetime.now(timezone.utc)
        unique: Dict[str, LedgerEntryDraft] = {}
        for draft in drafts:
            unique.setdefault(draft.entry_key, draft)
        inserted: List[LedgerEntry] = []
        async with self._transaction() as conn:
            if self.postgres:
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"ledger:{cluster_uid}")
            existing = await self._existing_keys(conn, cluster_uid, list(unique))
            seq, prev_hash = await self._head(conn, cluster_uid)
            placeholders = ", ".join("?" for _ in _COLUMNS)
            insert = f"INSERT INTO savings_ledger_entries ({_SELECT}) VALUES ({placeholders})"
            for key, draft in unique.items():
                if key in existing:
                    continue
                seq += 1
                entry = place(
                    draft,
                    cluster_uid=cluster_uid,
                    cluster_name=cluster_name,
                    seq=seq,
                    prev_hash=prev_hash,
                    created_at=moment,
                )
                await self._execute(conn, insert, self._params(entry))
                prev_hash = entry.hash
                inserted.append(entry)
            for source_table, position in (watermarks or {}).items():
                await self._execute(
                    conn,
                    "INSERT INTO ledger_projection_watermarks (cluster_uid, source_table, position, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT (cluster_uid, source_table) DO UPDATE SET "
                    "position = CASE WHEN excluded.position > ledger_projection_watermarks.position "
                    "THEN excluded.position ELSE ledger_projection_watermarks.position END, "
                    "updated_at = excluded.updated_at",
                    [cluster_uid, source_table, int(position), self._ts(moment)],
                )
        return inserted

    async def get_watermarks(self, cluster_uid: str) -> Dict[str, int]:
        rows = await self._select(
            "SELECT source_table, position FROM ledger_projection_watermarks WHERE cluster_uid = ?", [cluster_uid]
        )
        return {row["source_table"]: int(row["position"]) for row in rows}

    # ------------------------------------------------------------------
    # v1 ledger reads (projection)
    # ------------------------------------------------------------------

    async def read_v1_rows(self, table: str, cluster_name: str, after_id: int, limit: int) -> List[dict]:
        """v1 rows of ``cluster_name`` after ``after_id`` (legacy hourly rows only when flagged compressed)."""
        if table not in V1_TABLES:
            raise ValueError(f"unknown v1 ledger table {table!r}")
        slot = "slot_start" if table == V1_RAW_TABLE else "NULL AS slot_start"
        legacy = f" AND {self._true('legacy_compressed')}" if table == V1_HOURLY_TABLE else ""
        rows = await self._select(
            f"SELECT id, recommendation_id, cluster_name, namespace, recommendation_type, measurement_method, "
            f"co2e_saved_grams, cost_saved_dollars, period_start, period_end, {slot}, confidence, superseded "
            f"FROM {table} WHERE cluster_name = ? AND id > ?{legacy} ORDER BY id LIMIT ?",
            [cluster_name, int(after_id), int(limit)],
        )
        for row in rows:
            for column in ("period_start", "period_end", "slot_start"):
                row[column] = parse_time(row.get(column))
            row["superseded"] = bool(row.get("superseded"))
        return rows

    async def superseded_unreversed(self, cluster_uid: str, limit: int = 5000) -> List[LedgerEntry]:
        """Entries whose v1 source row is superseded and that no supersession reversal cancels yet.

        Also returns the restatement-book entries (restatements and the
        reversals of the projections they restate) attached to such a source.
        """
        not_reversed = (
            "NOT EXISTS (SELECT 1 FROM savings_ledger_entries r WHERE r.reverses_entry_id = e.entry_id "
            f"AND r.reason = '{REASON_SUPERSESSION}')"
        )
        entries: Dict[str, LedgerEntry] = {}
        for table in V1_TABLES:
            superseded = self._true("l.superseded")
            direct = await self._select(
                f"SELECT {_SELECT_E} FROM {table} l JOIN savings_ledger_entries e "
                f"ON e.source_table = '{table}' AND e.source_id = l.id "
                f"WHERE {superseded} AND e.cluster_uid = ? AND e.reason <> '{REASON_SUPERSESSION}' AND {not_reversed} "
                "ORDER BY e.seq LIMIT ?",
                [cluster_uid, limit],
            )
            for link in ("restates_entry_id", "reverses_entry_id"):
                restated = await self._select(
                    f"SELECT {_SELECT_E} FROM {table} l JOIN savings_ledger_entries p "
                    f"ON p.source_table = '{table}' AND p.source_id = l.id "
                    f"JOIN savings_ledger_entries e ON e.{link} = p.entry_id "
                    f"WHERE {superseded} AND e.cluster_uid = ? AND e.book = '{BOOK_RESTATEMENT}' "
                    f"AND e.reason = '{REASON_RESTATEMENT}' AND {not_reversed} ORDER BY e.seq LIMIT ?",
                    [cluster_uid, limit],
                )
                direct.extend(restated)
            for row in direct:
                entry = self._entry(row)
                entries[entry.entry_id] = entry
        return sorted(entries.values(), key=lambda item: item.seq)

    async def unrestated_projections(self, cluster_uid: str, recommendation_ids: Iterable[int]) -> List[LedgerEntry]:
        """Projected accruals of ``recommendation_ids`` that no reversal (of any reason) cancels."""
        ids = sorted({int(item) for item in recommendation_ids})
        result: List[LedgerEntry] = []
        for offset in range(0, len(ids), _CHUNK):
            chunk = ids[offset : offset + _CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            rows = await self._select(
                f"SELECT {_SELECT_E} FROM savings_ledger_entries e "
                f"WHERE e.cluster_uid = ? AND e.recommendation_id IN ({placeholders}) "
                "AND e.method = 'projected' AND e.entry_kind = 'accrual' "
                "AND NOT EXISTS (SELECT 1 FROM savings_ledger_entries r WHERE r.reverses_entry_id = e.entry_id) "
                "ORDER BY e.seq",
                [cluster_uid, *chunk],
            )
            result.extend(self._entry(row) for row in rows)
        return result

    async def superseded_sources(self, entries: Sequence[LedgerEntry]) -> set[str]:
        """Entry ids among ``entries`` whose v1 source row is superseded."""
        found: set[str] = set()
        for table in V1_TABLES:
            ids = sorted({entry.source_id for entry in entries if entry.source_table == table and entry.source_id})
            for offset in range(0, len(ids), _CHUNK):
                chunk = ids[offset : offset + _CHUNK]
                placeholders = ", ".join("?" for _ in chunk)
                rows = await self._select(
                    f"SELECT id FROM {table} WHERE id IN ({placeholders}) AND {self._true('superseded')}", chunk
                )
                superseded = {int(row["id"]) for row in rows}
                found.update(
                    entry.entry_id for entry in entries if entry.source_table == table and entry.source_id in superseded
                )
        return found

    async def measured_accruals(
        self, cluster_uid: str, recommendation_id: int, start: datetime, end: datetime
    ) -> List[LedgerEntry]:
        """Measured accruals of one recommendation overlapping ``[start, end)``, oldest first."""
        rows = await self._select(
            f"SELECT {_SELECT} FROM savings_ledger_entries WHERE cluster_uid = ? AND recommendation_id = ? "
            "AND method = 'measured_allocated' AND entry_kind = 'accrual' AND period_end > ? AND period_start < ? "
            "ORDER BY period_start, seq",
            [cluster_uid, int(recommendation_id), self._ts(start), self._ts(end)],
        )
        return [self._entry(row) for row in rows]

    async def provenance(self, recommendation_ids: Iterable[int]) -> Dict[int, Provenance]:
        """Latest change (with its pull request) and latest measurement of each recommendation."""
        ids = sorted({int(item) for item in recommendation_ids})
        result: Dict[int, Dict[str, Any]] = {rec_id: {} for rec_id in ids}
        for offset in range(0, len(ids), _CHUNK):
            chunk = ids[offset : offset + _CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            changes = await self._select(
                "SELECT m.recommendation_id, m.change_id, c.pr_url FROM change_members m "
                "JOIN change_events c ON c.change_id = m.change_id "
                f"WHERE m.recommendation_id IN ({placeholders}) ORDER BY m.added_at, c.created_at",
                chunk,
            )
            for row in changes:
                result[int(row["recommendation_id"])].update(change_id=row["change_id"], pr_url=row["pr_url"])
            if changes:
                change_ids = sorted({row["change_id"] for row in changes})
                marks = ", ".join("?" for _ in change_ids)
                members = await self._select(
                    f"SELECT change_id, recommendation_id FROM change_members WHERE change_id IN ({marks})",
                    change_ids,
                )
                by_change: Dict[str, List[int]] = {}
                for row in members:
                    by_change.setdefault(row["change_id"], []).append(int(row["recommendation_id"]))
                for data in result.values():
                    if data.get("change_id") in by_change:
                        data["recommendation_ids"] = tuple(sorted(by_change[data["change_id"]]))
            measurements = await self._select(
                "SELECT recommendation_id, measurement_id, strategy_key, strategy_version, change_id "
                f"FROM savings_measurements WHERE recommendation_id IN ({placeholders}) ORDER BY decided_at, sequence",
                chunk,
            )
            for row in measurements:
                data = result[int(row["recommendation_id"])]
                data.update(
                    measurement_id=row["measurement_id"],
                    method_version=f"{row['strategy_key']}/{row['strategy_version']}",
                )
                if row.get("change_id") and not data.get("change_id"):
                    data["change_id"] = row["change_id"]
        return {rec_id: Provenance(**data) for rec_id, data in result.items()}

    # ------------------------------------------------------------------
    # Chain reads
    # ------------------------------------------------------------------

    async def clusters(self) -> List[str]:
        rows = await self._select(
            "SELECT cluster_uid FROM savings_ledger_entries UNION SELECT cluster_uid FROM ledger_chain_status "
            "ORDER BY cluster_uid"
        )
        return [row["cluster_uid"] for row in rows]

    async def chain_page(self, cluster_uid: str, after_seq: int, limit: int) -> List[LedgerEntry]:
        rows = await self._select(
            f"SELECT {_SELECT} FROM savings_ledger_entries WHERE cluster_uid = ? AND seq > ? ORDER BY seq LIMIT ?",
            [cluster_uid, int(after_seq), int(limit)],
        )
        return [self._entry(row) for row in rows]

    async def head(self, cluster_uid: str) -> Tuple[int, Optional[str]]:
        async with self.db_manager.connection_scope() as conn:
            return await self._head(conn, cluster_uid)

    async def count(self, cluster_uid: str) -> int:
        rows = await self._select(
            "SELECT COUNT(*) AS n FROM savings_ledger_entries WHERE cluster_uid = ?", [cluster_uid]
        )
        return int(rows[0]["n"]) if rows else 0

    async def get_entry(self, entry_id: str) -> Optional[LedgerEntry]:
        rows = await self._select(f"SELECT {_SELECT} FROM savings_ledger_entries WHERE entry_id = ?", [entry_id])
        return self._entry(rows[0]) if rows else None

    def _filters(
        self,
        cluster_uid: str,
        filters: Mapping[str, Any],
        start: Optional[datetime],
        end: Optional[datetime],
    ) -> Tuple[str, List[Any]]:
        clauses = ["cluster_uid = ?"]
        params: List[Any] = [cluster_uid]
        for column in FILTER_COLUMNS:
            value = filters.get(column)
            if value is None:
                continue
            clauses.append(f"{column} = ?")
            params.append(value)
        if start is not None:
            clauses.append("period_end > ?")
            params.append(self._ts(start))
        if end is not None:
            clauses.append("period_start < ?")
            params.append(self._ts(end))
        return " AND ".join(clauses), params

    async def list_entries(
        self,
        cluster_uid: str,
        *,
        filters: Optional[Mapping[str, Any]] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        before_seq: Optional[int] = None,
        limit: int = 100,
        ascending: bool = False,
    ) -> List[LedgerEntry]:
        """Entries newest first (``before_seq`` is the cursor), or oldest first for exports."""
        where, params = self._filters(cluster_uid, filters or {}, start, end)
        if before_seq is not None:
            where += " AND seq > ?" if ascending else " AND seq < ?"
            params.append(int(before_seq))
        order = "ASC" if ascending else "DESC"
        rows = await self._select(
            f"SELECT {_SELECT} FROM savings_ledger_entries WHERE {where} ORDER BY seq {order} LIMIT ?",
            [*params, int(limit)],
        )
        return [self._entry(row) for row in rows]

    async def summary(
        self,
        cluster_uid: str,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        namespace: Optional[str] = None,
    ) -> List[SummaryRow]:
        """Signed totals per method, quantity, book and kind.

        With a window, an entry contributes the share of its period inside
        ``[start, end)``, as ``GET /recommendations/savings`` does.
        """
        where, params = self._filters(cluster_uid, {"namespace": namespace}, start, end)
        share = "1.0"
        share_params: List[Any] = []
        if start is not None and end is not None:
            if self.postgres:
                share = (
                    "CASE WHEN period_start >= ? AND period_end <= ? THEN 1.0 ELSE GREATEST(0.0, "
                    "EXTRACT(EPOCH FROM (LEAST(period_end, ?) - GREATEST(period_start, ?)))) "
                    "/ NULLIF(EXTRACT(EPOCH FROM (period_end - period_start)), 0) END"
                )
            else:
                share = (
                    "CASE WHEN period_start >= ? AND period_end <= ? THEN 1.0 ELSE MAX(0.0, "
                    "MIN(julianday(period_end), julianday(?)) - MAX(julianday(period_start), julianday(?))) "
                    "/ NULLIF(julianday(period_end) - julianday(period_start), 0) END"
                )
            share_params = [self._ts(start), self._ts(end), self._ts(end), self._ts(start)]
        rows = await self._select(
            f"SELECT method, quantity, unit, book, entry_kind, SUM(value * share) AS value, COUNT(*) AS entries "
            f"FROM (SELECT method, quantity, unit, book, entry_kind, value, {share} AS share "
            f"FROM savings_ledger_entries WHERE {where}) AS windowed "
            "GROUP BY method, quantity, unit, book, entry_kind ORDER BY method, quantity, book, entry_kind",
            [*share_params, *params],
        )
        return [
            SummaryRow(
                method=row["method"],
                quantity=row["quantity"],
                unit=row["unit"],
                book=row["book"],
                entry_kind=row["entry_kind"],
                value=float(row["value"] or 0.0),
                entries=int(row["entries"] or 0),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Chain status
    # ------------------------------------------------------------------

    async def record_chain_status(self, status: ChainStatus) -> None:
        ok: Any = bool(status.ok) if self.postgres else int(bool(status.ok))
        async with self.db_manager.transaction() as conn:
            await self._execute(
                conn,
                "INSERT INTO ledger_chain_status (cluster_uid, verified_at, ok, entries, head_seq, head_hash, failure) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (cluster_uid) DO UPDATE SET "
                "verified_at = excluded.verified_at, ok = excluded.ok, entries = excluded.entries, "
                "head_seq = excluded.head_seq, head_hash = excluded.head_hash, failure = excluded.failure",
                [
                    status.cluster_uid,
                    self._ts(status.verified_at),
                    ok,
                    int(status.entries),
                    status.head_seq,
                    status.head_hash,
                    status.failure,
                ],
            )

    async def get_chain_status(self, cluster_uid: str) -> Optional[ChainStatus]:
        rows = await self._select(
            "SELECT cluster_uid, verified_at, ok, entries, head_seq, head_hash, failure "
            "FROM ledger_chain_status WHERE cluster_uid = ?",
            [cluster_uid],
        )
        if not rows:
            return None
        row = rows[0]
        verified_at = parse_time(row["verified_at"])
        assert verified_at is not None
        return ChainStatus(
            cluster_uid=row["cluster_uid"],
            verified_at=verified_at,
            ok=bool(row["ok"]),
            entries=int(row["entries"]),
            head_seq=int(row["head_seq"]) if row["head_seq"] is not None else None,
            head_hash=row["head_hash"],
            failure=row["failure"],
        )


def get_ledger_repository(db_manager=None) -> LedgerRepository:
    """Return a ledger repository bound to ``db_manager`` (the shared manager by default)."""
    if db_manager is None:
        from greenkube.core.db import get_db_manager

        db_manager = get_db_manager()
    return LedgerRepository(db_manager)


__all__ = [
    "ChainStatus",
    "FILTER_COLUMNS",
    "LedgerConflictError",
    "LedgerRepository",
    "Provenance",
    "SummaryRow",
    "V1_HOURLY_TABLE",
    "V1_RAW_TABLE",
    "V1_TABLES",
    "get_ledger_repository",
]
