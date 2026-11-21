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
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from greenkube.models.savings import SavingsAccrualTotal, SavingsCounterTotal
from greenkube.utils.time import ensure_utc
from greenkube.verify.ledger.model import (
    BOOK_RESTATEMENT,
    REASON_RESTATEMENT,
    REASON_SUPERSESSION,
    LedgerEntry,
    LedgerEntryDraft,
    chain_key_id,
    format_time,
    parse_time,
    place,
    v2_method,
)

logger = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"\?")
_CHUNK = 500

V1_RAW_TABLE = "recommendation_savings_ledger"
V1_HOURLY_TABLE = "recommendation_savings_ledger_hourly"
V1_TABLES = (V1_RAW_TABLE, V1_HOURLY_TABLE)
#: v2 methods under the names the v1 report, gauges and counters use.
V1_METHOD_NAMES = {"measured_allocated": "measured"}

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
    "basis",
    "chain_key_id",
    "annual_rate",
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
    #: Accounting basis (stored, or inferred from the method for entries stored before ledger/2).
    basis: str = ""
    #: ``cost_of_fix`` for the cost of reliability fixes (reported apart), ``""`` for savings.
    reason_group: str = ""


#: SQL true for the cost of a reliability fix and its reversals (their group key carries ``cost_of_fix:``): a
#: cost, never a saving, reported apart from every savings total (review WP-09).
_COST_OF_FIX_SQL = "group_key LIKE '%cost_of_fix:%'"

#: SQL of an entry's accounting basis (``model.basis_of``): entries stored before migration 0160 have none.
_BASIS_SQL = (
    "COALESCE(basis, CASE method WHEN 'projected' THEN 'billable_expected' WHEN 'realized_billed' THEN 'billed' "
    "WHEN 'user_declared' THEN 'declared' ELSE 'allocated' END)"
)


#: Group columns of ``savings_ledger_totals`` (LED-09), in primary-key order after ``cluster_uid``.
_TOTAL_KEYS = (
    "namespace",
    "recommendation_type",
    "method",
    "quantity",
    "unit",
    "book",
    "entry_kind",
    "basis",
    "reason_group",
)


def _total_key(entry: LedgerEntry) -> Tuple[str, ...]:
    """The ``savings_ledger_totals`` group of an entry, by the readers' rules (``_BASIS_SQL``, ``_COST_OF_FIX_SQL``)."""
    return (
        entry.namespace or "",
        entry.recommendation_type or "",
        entry.method,
        entry.quantity,
        entry.unit or "",
        entry.book,
        entry.entry_kind,
        entry.effective_basis,
        "cost_of_fix" if "cost_of_fix:" in (entry.group_key or "") else "",
    )


def _impact_currency(raw: Any) -> Optional[str]:
    """The ``currency`` of a stored ``impact`` projection (JSON text or a mapping), ``None`` when absent."""
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    currency = raw.get("currency")
    return str(currency).strip().upper() or None if isinstance(currency, str) else None


@dataclass(frozen=True)
class Provenance:
    change_id: Optional[str] = None
    pr_url: Optional[str] = None
    measurement_id: Optional[str] = None
    method_version: Optional[str] = None
    recommendation_ids: Tuple[int, ...] = ()
    #: Currency of the recommendation's projection (``impact.currency``): the currency its accruals are in.
    currency: Optional[str] = None


class LedgerChainKeyError(RuntimeError):
    """The chain's head was written with a key this process does not hold: nothing is appended (LED-05)."""


def configured_previous_chain_keys() -> List[bytes]:
    """Verify-only keys of a deliberate rotation (``LEDGER_CHAIN_PREVIOUS_KEYS``, comma-separated)."""
    from greenkube.core.config import get_config

    raw = getattr(get_config(), "LEDGER_CHAIN_PREVIOUS_KEYS", None) or ""
    return [item.strip().encode("utf-8") for item in raw.split(",") if item.strip()]


def configured_chain_key() -> Optional[bytes]:
    """The ledger chain key from ``LEDGER_CHAIN_KEY`` (LED-05), or ``None`` when none is configured."""
    from greenkube.core.config import get_config

    raw = getattr(get_config(), "LEDGER_CHAIN_KEY", None)
    return raw.encode("utf-8") if raw else None


def _sqlite_epoch(expression: str) -> str:
    """SQLite seconds since the epoch of a canonical ledger timestamp, to the microsecond."""
    return f"(CAST(strftime('%s', {expression}) AS REAL) + CAST(substr({expression}, 21, 6) AS REAL) / 1000000.0)"


class LedgerRepository:
    """Append-only savings ledger entries, their chain and their projections.

    ``chain_key`` keys the hash chain (LED-05); by default it is read from
    ``LEDGER_CHAIN_KEY``. An empty value means no key. ``previous_keys``
    (default ``LEDGER_CHAIN_PREVIOUS_KEYS``) only verify entries chained
    before a deliberate rotation, and let the writer continue after them.
    """

    def __init__(self, db_manager, chain_key: Optional[bytes] = None, previous_keys: Optional[Sequence[bytes]] = None):
        self.db_manager = db_manager
        self._chain_key = chain_key
        self._previous_keys = list(previous_keys) if previous_keys is not None else None

    @property
    def chain_keys(self) -> Dict[str, bytes]:
        """Every key this process can verify with, by public fingerprint: the current one and the previous ones."""
        previous = self._previous_keys if self._previous_keys is not None else configured_previous_chain_keys()
        keys = {chain_key_id(key): key for key in previous if key}
        current = self.chain_key
        if current:
            keys[chain_key_id(current)] = current
        return keys

    @property
    def chain_key(self) -> Optional[bytes]:
        """Key of the hash chain: the one given, else ``LEDGER_CHAIN_KEY``; ``None`` without a key."""
        if self._chain_key is not None:
            return self._chain_key or None
        return configured_chain_key()

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
        for column in ("confidence", "intensity_g_per_kwh", "annual_rate"):
            if data.get(column) is not None:
                data[column] = float(data[column])
        return LedgerEntry(**data)

    @staticmethod
    def _column_names() -> Tuple[str, ...]:
        """Columns of ``savings_ledger_entries`` in the order of :meth:`_params`."""
        return _COLUMNS

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

    async def _check_writer_key(self, conn, cluster_uid: str, chain_key: Optional[bytes]) -> None:
        """Fail closed when the head is keyed and this process lacks that key or a rotation from it (LED-05).

        Appending unkeyed or differently keyed entries after keyed ones would
        break the chain for good, so nothing is written: the error is counted
        (``greenkube_ledger_write_errors_total{reason="chain_key"}``) and logged.
        """
        rows = await self._fetch(
            conn,
            "SELECT chain_key_id FROM savings_ledger_entries WHERE cluster_uid = ? ORDER BY seq DESC LIMIT 1",
            [cluster_uid],
        )
        head_key_id = rows[0]["chain_key_id"] if rows else None
        if head_key_id is None:
            return
        current_id = chain_key_id(chain_key) if chain_key else None
        if current_id == head_key_id or (current_id is not None and head_key_id in self.chain_keys):
            return
        from greenkube.verify.ledger.projector import LEDGER_WRITE_ERRORS

        LEDGER_WRITE_ERRORS.labels(reason="chain_key").inc()
        message = (
            f"The savings ledger head of cluster {cluster_uid} is chained with key {head_key_id}, and "
            + ("LEDGER_CHAIN_KEY is not set" if current_id is None else f"LEDGER_CHAIN_KEY is key {current_id}")
            + ": nothing is appended. Restore the Secret's LEDGER_CHAIN_KEY, or for a deliberate rotation list "
            "the old key in LEDGER_CHAIN_PREVIOUS_KEYS."
        )
        logger.error(message)
        raise LedgerChainKeyError(message)

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
            chain_key = self.chain_key
            await self._check_writer_key(conn, cluster_uid, chain_key)
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
                    chain_key=chain_key,
                )
                await self._execute(conn, insert, self._params(entry))
                prev_hash = entry.hash
                inserted.append(entry)
            await self._add_totals(conn, cluster_uid, inserted)
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

    async def _add_totals(self, conn, cluster_uid: str, entries: Sequence[LedgerEntry]) -> None:
        """Add ``entries`` to ``savings_ledger_totals`` in the append transaction (LED-09)."""
        groups: Dict[Tuple[str, ...], List[float]] = {}
        for entry in entries:
            sums = groups.setdefault(_total_key(entry), [0.0, 0.0, 0.0, 0.0])
            value = float(entry.value)
            sums[0] += value
            sums[1] += value if value > 0 else 0.0
            sums[2] += value if value < 0 else 0.0
            sums[3] += 1
        columns = ", ".join(("cluster_uid", *_TOTAL_KEYS))
        upsert = (
            f"INSERT INTO savings_ledger_totals ({columns}, value, positive, negative, entries) "
            f"VALUES ({', '.join('?' for _ in range(len(_TOTAL_KEYS) + 5))}) "
            f"ON CONFLICT ({columns}) DO UPDATE SET value = savings_ledger_totals.value + excluded.value, "
            "positive = savings_ledger_totals.positive + excluded.positive, "
            "negative = savings_ledger_totals.negative + excluded.negative, "
            "entries = savings_ledger_totals.entries + excluded.entries"
        )
        for key, (value, positive, negative, count) in groups.items():
            await self._execute(conn, upsert, [cluster_uid, *key, value, positive, negative, int(count)])

    async def totals_mismatch(self, cluster_uid: str, tolerance: float = 1e-9) -> Optional[str]:
        """Compare ``savings_ledger_totals`` with a fold of the cluster's entries (LED-09).

        The totals are what users see; they are not chained, so the full
        verification re-derives them from the chained entries. Returns a
        description of the first group that differs, or ``None``.
        """
        keys = ", ".join(_TOTAL_KEYS)
        folded = await self._select(
            f"SELECT {keys}, SUM(value) AS value, SUM(CASE WHEN value > 0 THEN value ELSE 0 END) AS positive, "
            "SUM(CASE WHEN value < 0 THEN value ELSE 0 END) AS negative, SUM(ABS(value)) AS magnitude, "
            "COUNT(*) AS entries FROM ("
            "SELECT COALESCE(namespace, '') AS namespace, "
            "COALESCE(recommendation_type, '') AS recommendation_type, method, quantity, COALESCE(unit, '') AS unit, "
            f"book, entry_kind, {_BASIS_SQL} AS basis, "
            f"CASE WHEN {_COST_OF_FIX_SQL} THEN 'cost_of_fix' ELSE '' END AS reason_group, value "
            f"FROM savings_ledger_entries WHERE cluster_uid = ?) AS grouped GROUP BY {keys}",
            [cluster_uid],
        )
        stored = await self._select(
            f"SELECT {keys}, value, positive, negative, entries FROM savings_ledger_totals WHERE cluster_uid = ?",
            [cluster_uid],
        )
        expected = {tuple(row[key] for key in _TOTAL_KEYS): row for row in folded}
        actual = {tuple(row[key] for key in _TOTAL_KEYS): row for row in stored if int(row["entries"] or 0)}
        for key in sorted(set(expected) | set(actual)):
            want, have = expected.get(key), actual.get(key)
            if want is None or have is None:
                return f"group {key} is {'missing from the totals' if have is None else 'not in the entries'}"
            if int(want["entries"]) != int(have["entries"]):
                return f"group {key}: totals over {have['entries']} entries, entries {want['entries']}"
            # Relative to the sum of magnitudes: a mixed-sign group whose net is near 0 is not a false mismatch.
            bound = tolerance * max(1.0, float(want["magnitude"] or 0.0))
            for column in ("value", "positive", "negative"):
                folded_sum, total = float(want[column] or 0.0), float(have[column] or 0.0)
                if abs(folded_sum - total) > bound:
                    return f"group {key}: totals {column} {total!r}, entries {column} {folded_sum!r}"
        return None

    async def get_watermarks(self, cluster_uid: str) -> Dict[str, int]:
        rows = await self._select(
            "SELECT source_table, position FROM ledger_projection_watermarks WHERE cluster_uid = ?", [cluster_uid]
        )
        return {row["source_table"]: int(row["position"]) for row in rows}

    # ------------------------------------------------------------------
    # v1 ledger reads (projection)
    # ------------------------------------------------------------------

    def _v1_scope(self, table: str, cluster_uid: Optional[str]) -> str:
        """WHERE fragment of the v1 rows to project (alias ``l``); ``cluster_uid`` keeps only unprojected ones."""
        if table not in V1_TABLES:
            raise ValueError(f"unknown v1 ledger table {table!r}")
        clauses = ["l.cluster_name = ?", "l.id > ?"]
        if table == V1_HOURLY_TABLE:
            clauses.append(self._true("l.legacy_compressed"))
        if cluster_uid is not None:
            # Rows with no non-zero quantity never produce an entry: they are not a backlog.
            clauses.append("(COALESCE(l.co2e_saved_grams, 0) <> 0 OR COALESCE(l.cost_saved_dollars, 0) <> 0)")
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM savings_ledger_entries e "
                f"WHERE e.source_table = '{table}' AND e.source_id = l.id AND e.cluster_uid = ?)"
            )
        return " AND ".join(clauses)

    async def read_v1_rows(
        self,
        table: str,
        cluster_name: str,
        after_id: int,
        limit: int,
        *,
        unprojected_for: Optional[str] = None,
    ) -> List[dict]:
        """v1 rows of ``cluster_name`` after ``after_id`` (legacy hourly rows only when flagged compressed).

        With ``unprojected_for`` (a cluster uid), only the rows that have no
        entry in that cluster's chain yet: an anti-join, so a row committed
        after rows with higher ids (out-of-order sequence commits) is still
        found, however late it becomes visible.
        """
        slot = "l.slot_start" if table == V1_RAW_TABLE else "NULL AS slot_start"
        basis = (
            "l.basis, l.row_kind, l.annual_cost_rate, l.annual_co2e_rate, l.currency, l.measurement_id"
            if table == V1_RAW_TABLE
            else "NULL AS basis, NULL AS row_kind, NULL AS annual_cost_rate, NULL AS annual_co2e_rate, "
            "NULL AS currency, NULL AS measurement_id"
        )
        where = self._v1_scope(table, unprojected_for)
        params: List[Any] = [cluster_name, int(after_id)]
        if unprojected_for is not None:
            params.append(unprojected_for)
        rows = await self._select(
            "SELECT l.id, l.recommendation_id, l.cluster_name, l.namespace, l.recommendation_type, "
            "l.measurement_method, l.co2e_saved_grams, l.cost_saved_dollars, l.period_start, l.period_end, "
            f"{slot}, {basis}, l.confidence, l.superseded FROM {table} l WHERE {where} ORDER BY l.id LIMIT ?",
            [*params, int(limit)],
        )
        for row in rows:
            for column in ("period_start", "period_end", "slot_start"):
                row[column] = parse_time(row.get(column))
            row["superseded"] = bool(row.get("superseded"))
        return rows

    async def count_unprojected_v1_rows(self, table: str, cluster_name: str, cluster_uid: str) -> int:
        """v1 rows of ``cluster_name`` with a non-zero quantity and no entry in ``cluster_uid``'s chain."""
        rows = await self._select(
            f"SELECT COUNT(*) AS n FROM {table} l WHERE {self._v1_scope(table, cluster_uid)}",
            [cluster_name, 0, cluster_uid],
        )
        return int(rows[0]["n"]) if rows else 0

    async def superseded_unreversed(self, cluster_uid: str, limit: int = 5000) -> List[LedgerEntry]:
        """Entries whose v1 source row is superseded and that no supersession reversal cancels yet.

        Also returns the restatement-book entries (restatements and the
        reversals of the projections they restate) attached to such a source.
        """
        not_reversed = (
            "NOT EXISTS (SELECT 1 FROM savings_ledger_entries r WHERE r.reverses_entry_id = e.entry_id "
            f"AND r.reason = '{REASON_SUPERSESSION}')"
        )
        # A restatement-book entry is already cancelled by a supersession reversal or by the reversal of a
        # re-measurement (reason restatement): never reverse it twice (review WP-09).
        cancelled = (
            "NOT EXISTS (SELECT 1 FROM savings_ledger_entries r WHERE r.reverses_entry_id = e.entry_id "
            f"AND r.reason IN ('{REASON_SUPERSESSION}', '{REASON_RESTATEMENT}'))"
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
                    f"AND e.reason IN ('{REASON_RESTATEMENT}', 'not_realized') AND {cancelled} "
                    "ORDER BY e.seq LIMIT ?",
                    [cluster_uid, limit],
                )
                direct.extend(restated)
            for row in direct:
                entry = self._entry(row)
                entries[entry.entry_id] = entry
        return sorted(entries.values(), key=lambda item: item.seq)

    async def reversal_count(self, cluster_uid: str, entry_id: str, book: str, before_seq: int) -> int:
        """Non-drift reversals of ``entry_id`` in ``book`` placed before position ``before_seq``."""
        rows = await self._select(
            "SELECT COUNT(*) AS n FROM savings_ledger_entries WHERE cluster_uid = ? AND reverses_entry_id = ? "
            "AND book = ? AND entry_kind = 'reversal' AND reason <> 'drift' AND seq < ?",
            [cluster_uid, entry_id, book, int(before_seq)],
        )
        return int(rows[0]["n"]) if rows else 0

    async def live_v1_recommendations(self, recommendation_ids: Iterable[int]) -> set[int]:
        """Those of ``recommendation_ids`` that still have v1 rows not flagged superseded."""
        ids = sorted({int(item) for item in recommendation_ids})
        live: set[int] = set()
        for table in V1_TABLES:
            for offset in range(0, len(ids), _CHUNK):
                chunk = ids[offset : offset + _CHUNK]
                placeholders = ", ".join("?" for _ in chunk)
                rows = await self._select(
                    f"SELECT DISTINCT recommendation_id FROM {table} "
                    f"WHERE recommendation_id IN ({placeholders}) AND NOT ({self._true('superseded')})",
                    chunk,
                )
                live.update(int(row["recommendation_id"]) for row in rows)
        return live

    async def supersede_v1(self, recommendation_ids: Iterable[int]) -> int:
        """Flag every v1 row of ``recommendation_ids`` superseded (the only update the v1 triggers allow)."""
        ids = sorted({int(item) for item in recommendation_ids})
        changed = 0
        if not ids:
            return 0
        async with self.db_manager.transaction() as conn:
            for table in V1_TABLES:
                for offset in range(0, len(ids), _CHUNK):
                    chunk = ids[offset : offset + _CHUNK]
                    placeholders = ", ".join("?" for _ in chunk)
                    truth = "TRUE" if self.postgres else "1"
                    changed += await self._execute(
                        conn,
                        f"UPDATE {table} SET superseded = {truth} "
                        f"WHERE recommendation_id IN ({placeholders}) AND NOT ({self._true('superseded')})",
                        chunk,
                    )
        return changed

    async def unreversed_corrections(self, cluster_uid: str, recommendation_ids: Iterable[int]) -> List[LedgerEntry]:
        """Slot restatements and reinstatements of ``recommendation_ids`` no supersession reversal cancels yet.

        They have no v1 source row, so the v1 ``superseded`` flag cannot reach them.
        """
        ids = sorted({int(item) for item in recommendation_ids})
        result: List[LedgerEntry] = []
        for offset in range(0, len(ids), _CHUNK):
            chunk = ids[offset : offset + _CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            rows = await self._select(
                f"SELECT {_SELECT_E} FROM savings_ledger_entries e "
                f"WHERE e.cluster_uid = ? AND e.recommendation_id IN ({placeholders}) "
                "AND (e.entry_key LIKE 'restatement:slot:%' OR e.reason IN ('reinstatement', 'cost_of_fix', "
                "'released_capacity')) "
                "AND NOT EXISTS (SELECT 1 FROM savings_ledger_entries r WHERE r.reverses_entry_id = e.entry_id "
                f"AND r.reason IN ('{REASON_SUPERSESSION}', '{REASON_RESTATEMENT}')) ORDER BY e.seq",
                [cluster_uid, *chunk],
            )
            result.extend(self._entry(row) for row in rows)
        return result

    async def measured_quantity_ends(
        self, cluster_uid: str, recommendation_ids: Iterable[int]
    ) -> Dict[Tuple[int, str, str], datetime]:
        """End of the last released-capacity or cost-of-fix entry per (recommendation, reason, quantity)."""
        ids = sorted({int(item) for item in recommendation_ids})
        ends: Dict[Tuple[int, str, str], datetime] = {}
        for offset in range(0, len(ids), _CHUNK):
            chunk = ids[offset : offset + _CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            rows = await self._select(
                "SELECT recommendation_id, reason, quantity, MAX(period_end) AS period_end FROM savings_ledger_entries "
                f"WHERE cluster_uid = ? AND recommendation_id IN ({placeholders}) "
                "AND reason IN ('cost_of_fix', 'released_capacity') AND entry_kind = 'accrual' "
                "GROUP BY recommendation_id, reason, quantity",
                [cluster_uid, *chunk],
            )
            for row in rows:
                end = parse_time(row.get("period_end"))
                if end is not None:
                    ends[(int(row["recommendation_id"]), str(row["reason"]), str(row["quantity"]))] = end
        return ends

    async def change_measurement_facts(self, change_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        """Latest decided measurement of each change: ``capacity``, ``counterfactual_params``, ``allocations``."""
        ids = sorted({str(item) for item in change_ids if item})
        facts: Dict[str, Dict[str, Any]] = {}
        for offset in range(0, len(ids), _CHUNK):
            chunk = ids[offset : offset + _CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            rows = await self._select(
                "SELECT measurement_id, change_id, capacity, counterfactual_params, allocations "
                "FROM change_measurements "
                f"WHERE change_id IN ({placeholders}) ORDER BY change_id, sequence",
                chunk,
            )
            for row in rows:
                fact: Dict[str, Any] = {"measurement_id": row["measurement_id"]}
                for column in ("capacity", "counterfactual_params", "allocations"):
                    raw = row.get(column)
                    if isinstance(raw, (str, bytes)):
                        try:
                            raw = json.loads(raw)
                        except ValueError:
                            raw = None
                    fact[column] = raw
                facts[str(row["change_id"])] = fact
        return facts

    async def active_measurement_corrections(
        self, cluster_uid: str, recommendation_ids: Iterable[int]
    ) -> List[LedgerEntry]:
        """Restatements and ``not_realized`` reversals of ``recommendation_ids`` that nothing reverses yet."""
        ids = sorted({int(item) for item in recommendation_ids})
        result: List[LedgerEntry] = []
        for offset in range(0, len(ids), _CHUNK):
            chunk = ids[offset : offset + _CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            rows = await self._select(
                f"SELECT {_SELECT_E} FROM savings_ledger_entries e "
                f"WHERE e.cluster_uid = ? AND e.recommendation_id IN ({placeholders}) "
                f"AND ((e.entry_kind = 'restatement' AND e.reason = '{REASON_RESTATEMENT}') "
                "OR e.reason = 'not_realized') "
                "AND NOT EXISTS (SELECT 1 FROM savings_ledger_entries r WHERE r.reverses_entry_id = e.entry_id) "
                "ORDER BY e.seq",
                [cluster_uid, *chunk],
            )
            result.extend(self._entry(row) for row in rows)
        return result

    async def unreinstated_supersessions(
        self, cluster_uid: str, recommendation_ids: Iterable[int]
    ) -> List[LedgerEntry]:
        """Supersession reversals of ``recommendation_ids`` that no reinstatement cancels yet (LED-08)."""
        ids = sorted({int(item) for item in recommendation_ids})
        result: List[LedgerEntry] = []
        for offset in range(0, len(ids), _CHUNK):
            chunk = ids[offset : offset + _CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            rows = await self._select(
                f"SELECT {_SELECT_E} FROM savings_ledger_entries e "
                f"WHERE e.cluster_uid = ? AND e.recommendation_id IN ({placeholders}) "
                f"AND e.reason = '{REASON_SUPERSESSION}' "
                "AND NOT EXISTS (SELECT 1 FROM savings_ledger_entries r WHERE r.reverses_entry_id = e.entry_id "
                "AND r.reason = 'reinstatement') ORDER BY e.seq",
                [cluster_uid, *chunk],
            )
            result.extend(self._entry(row) for row in rows)
        return result

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

    async def covered_slots(
        self, cluster_uid: str, recommendation_ids: Iterable[int]
    ) -> Dict[Tuple[int, str, datetime], List[Tuple[datetime, datetime]]]:
        """Periods covered per ``(recommendation_id, quantity, slot_start)``.

        Covered: a projected or measured accrual of that quantity (reversed or
        not: a reversed projection is restated by its own pair), or a slot
        restatement written earlier (which covers its whole slot).
        """
        ids = sorted({int(item) for item in recommendation_ids})
        covered: Dict[Tuple[int, str, datetime], List[Tuple[datetime, datetime]]] = {}
        for offset in range(0, len(ids), _CHUNK):
            chunk = ids[offset : offset + _CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            rows = await self._select(
                "SELECT recommendation_id, quantity, entry_key, period_start, period_end FROM savings_ledger_entries "
                f"WHERE cluster_uid = ? AND recommendation_id IN ({placeholders}) AND ("
                "(entry_kind = 'accrual' AND method IN ('projected', 'measured_allocated')) "
                "OR entry_key LIKE 'restatement:slot:%')",
                [cluster_uid, *chunk],
            )
            for row in rows:
                start, end = parse_time(row.get("period_start")), parse_time(row.get("period_end"))
                if start is None or end is None:
                    continue
                slot = start - timedelta(seconds=start.timestamp() % 3600)
                if str(row["entry_key"]).startswith("restatement:slot:"):
                    start, end = slot, slot + timedelta(hours=1)
                key = (int(row["recommendation_id"]), str(row["quantity"]), slot)
                covered.setdefault(key, []).append((start, end))
        return covered

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
            impacts = await self._select(
                f"SELECT id, impact FROM recommendation_history WHERE id IN ({placeholders})",
                chunk,
            )
            for row in impacts:
                currency = _impact_currency(row.get("impact"))
                if currency is not None and int(row["id"]) in result:
                    result[int(row["id"])]["currency"] = currency
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

    def _share(self, start: Optional[datetime], end: Optional[datetime]) -> Tuple[str, List[Any]]:
        """SQL of the share of an entry's period inside ``[start, end)``, the rule of the export's ``window_share``.

        A missing bound does not cut the period (1.0 without any window). A
        zero-length entry selected by the window filter lies inside it and
        counts in full (LED-12). SQLite durations are computed from whole
        seconds plus the stored microseconds, not ``julianday``, whose float
        days lose about 1e-9 of a share.
        """
        if start is None and end is None:
            return "1.0", []
        # The bounds are inlined as literals: they are datetimes formatted by format_time (digits, '-', ':',
        # '.', 'T', 'Z'), never user text, and SQLite repeats them in each duration expression.
        cast = "::timestamptz" if self.postgres else ""
        begin_bound = f"'{format_time(start)}'{cast}" if start is not None else "period_start"
        end_bound = f"'{format_time(end)}'{cast}" if end is not None else "period_end"
        if self.postgres:
            begin = f"GREATEST(period_start, {begin_bound})"
            finish = f"LEAST(period_end, {end_bound})"
            share = (
                "CASE WHEN period_end <= period_start THEN 1.0 "
                f"WHEN period_start >= {begin_bound} AND period_end <= {end_bound} THEN 1.0 "
                f"ELSE GREATEST(0.0, EXTRACT(EPOCH FROM ({finish} - {begin}))) "
                "/ EXTRACT(EPOCH FROM (period_end - period_start)) END"
            )
        else:
            begin = f"MAX(period_start, {begin_bound})"
            finish = f"MIN(period_end, {end_bound})"
            share = (
                "CASE WHEN period_end <= period_start THEN 1.0 "
                f"WHEN period_start >= {begin_bound} AND period_end <= {end_bound} THEN 1.0 "
                f"ELSE MAX(0.0, {_sqlite_epoch(finish)} - {_sqlite_epoch(begin)}) "
                f"/ ({_sqlite_epoch('period_end')} - {_sqlite_epoch('period_start')}) END"
            )
        return share, []

    async def accrual_breakdown(
        self,
        cluster_uid: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        *,
        namespace: Optional[str] = None,
        books: Optional[Sequence[str]] = None,
        recommendation_ids: Optional[Sequence[int]] = None,
        reasons: Optional[Sequence[str]] = None,
    ) -> List[SavingsAccrualTotal]:
        """Net amounts per namespace, recommendation type and method, in the v1 report shape (LED-04).

        Both books by default: the *restated* figures (a measured period moved
        from ``projected`` to ``measured_allocated``). ``books=("accrued",)``
        gives the accrued book alone (what the v1 ledger says);
        ``recommendation_ids`` restricts the totals to those recommendations. Methods use the
        v1 names (``measured_allocated`` -> ``measured``). Over a window an
        entry contributes the share of its period inside it, as the v1 report.
        """
        if start is None and end is None and recommendation_ids is None:
            return await self._breakdown_from_totals(cluster_uid, namespace, books, reasons)
        where, params = self._filters(cluster_uid, {"namespace": namespace}, start, end)
        if books:
            marks = ", ".join("?" for _ in books)
            where += f" AND book IN ({marks})"
            params.extend(books)
        if recommendation_ids is not None:
            ids = [int(item) for item in recommendation_ids]
            if not ids:
                return []
            where += f" AND recommendation_id IN ({', '.join('?' for _ in ids)})"
            params.extend(ids)

        where += " AND quantity IN ('cost', 'co2e_g')"
        # The cost of a reliability fix is never netted into savings: only with reasons=("cost_of_fix",).
        cost_of_fix = reasons is not None and "cost_of_fix" in reasons
        where += f" AND {_COST_OF_FIX_SQL}" if cost_of_fix else f" AND NOT ({_COST_OF_FIX_SQL})"
        share, share_params = self._share(start, end)
        rows = await self._select(
            "SELECT namespace, recommendation_type, method, quantity, SUM(value * share) AS value "
            f"FROM (SELECT namespace, recommendation_type, method, quantity, value, {share} AS share "
            f"FROM savings_ledger_entries WHERE {where}) AS windowed "
            "GROUP BY namespace, recommendation_type, method, quantity ORDER BY namespace, recommendation_type, method",
            [*share_params, *params],
        )
        totals: Dict[Tuple[str, str, str], SavingsAccrualTotal] = {}
        for row in rows:
            method = V1_METHOD_NAMES.get(row["method"], row["method"])
            key = (row["namespace"] or "", row["recommendation_type"] or "unknown", method)
            total = totals.setdefault(
                key, SavingsAccrualTotal(namespace=key[0], recommendation_type=key[1], method=method)
            )
            value = float(row["value"] or 0.0)
            if row["quantity"] == "cost":
                total.cost_saved_dollars += value
            else:
                total.co2e_saved_grams += value
        return list(totals.values())

    async def _breakdown_from_totals(
        self,
        cluster_uid: str,
        namespace: Optional[str],
        books: Optional[Sequence[str]],
        reasons: Optional[Sequence[str]],
    ) -> List[SavingsAccrualTotal]:
        """All-time :meth:`accrual_breakdown` from ``savings_ledger_totals`` (LED-09)."""
        where = "cluster_uid = ? AND quantity IN ('cost', 'co2e_g') AND reason_group = ?"
        cost_of_fix = reasons is not None and "cost_of_fix" in reasons
        params: List[Any] = [cluster_uid, "cost_of_fix" if cost_of_fix else ""]
        if namespace is not None:
            where += " AND namespace = ?"
            params.append(namespace)
        if books:
            where += f" AND book IN ({', '.join('?' for _ in books)})"
            params.extend(books)
        rows = await self._select(
            "SELECT namespace, recommendation_type, method, quantity, SUM(value) AS value FROM savings_ledger_totals "
            f"WHERE {where} GROUP BY namespace, recommendation_type, method, quantity "
            "ORDER BY namespace, recommendation_type, method",
            params,
        )
        totals: Dict[Tuple[str, str, str], SavingsAccrualTotal] = {}
        for row in rows:
            method = V1_METHOD_NAMES.get(row["method"], row["method"])
            key = (row["namespace"] or "", row["recommendation_type"] or "unknown", method)
            total = totals.setdefault(
                key, SavingsAccrualTotal(namespace=key[0], recommendation_type=key[1], method=method)
            )
            value = float(row["value"] or 0.0)
            if row["quantity"] == "cost":
                total.cost_saved_dollars += value
            else:
                total.co2e_saved_grams += value
        return list(totals.values())

    async def lifetime_counters(self, cluster_uid: str) -> List[SavingsCounterTotal]:
        """Monotonic counters per recommendation type and method from the entries (LED-04).

        ``accrued`` sums the positive entries of both books and ``reversed``
        the magnitude of the negative ones, so reversals and restatements are
        visible to Prometheus; ``accrued - reversed`` is the restated total.
        Methods use the v1 names.
        """
        rows = await self._select(
            "SELECT recommendation_type, method, quantity, SUM(positive) AS accrued, -SUM(negative) AS reversed "
            "FROM savings_ledger_totals WHERE cluster_uid = ? AND quantity IN ('cost', 'co2e_g') "
            "AND reason_group = '' GROUP BY recommendation_type, method, quantity",
            [cluster_uid],
        )
        counters: Dict[Tuple[str, str], SavingsCounterTotal] = {}
        for row in rows:
            method = V1_METHOD_NAMES.get(row["method"], row["method"])
            key = (row["recommendation_type"] or "unknown", method)
            counter = counters.setdefault(key, SavingsCounterTotal(recommendation_type=key[0], method=method))
            accrued, reversed_ = float(row["accrued"] or 0.0), float(row["reversed"] or 0.0)
            if row["quantity"] == "cost":
                counter.accrued_cost_dollars += accrued
                counter.reversed_cost_dollars += reversed_
            else:
                counter.accrued_co2e_grams += accrued
                counter.reversed_co2e_grams += reversed_
        return sorted(counters.values(), key=lambda item: (item.recommendation_type, item.method))

    async def accrued_book_totals(self, cluster_uid: str) -> Dict[Tuple[str, str], float]:
        """Net ``accrued`` book per (v1 method, quantity): what the v1 ledger must reconcile with."""
        rows = await self._select(
            "SELECT method, quantity, SUM(value) AS value FROM savings_ledger_totals "
            "WHERE cluster_uid = ? AND book = 'accrued' AND quantity IN ('cost', 'co2e_g') GROUP BY method, quantity",
            [cluster_uid],
        )
        return {
            (V1_METHOD_NAMES.get(row["method"], row["method"]), row["quantity"]): float(row["value"] or 0.0)
            for row in rows
        }

    async def v1_totals(self, cluster_name: str) -> Dict[Tuple[str, str], float]:
        """Net non-superseded v1 amounts per (v1 method, quantity), legacy compressed rows included."""
        totals: Dict[Tuple[str, str], float] = {}
        for table in V1_TABLES:
            scope = "cluster_name = ? AND NOT (" + self._true("superseded") + ")"
            if table == V1_HOURLY_TABLE:
                scope += " AND " + self._true("legacy_compressed")
            rows = await self._select(
                "SELECT measurement_method AS method, SUM(cost_saved_dollars) AS cost, SUM(co2e_saved_grams) AS co2e "
                f"FROM {table} WHERE {scope} GROUP BY measurement_method",
                [cluster_name],
            )
            for row in rows:
                method = V1_METHOD_NAMES.get(v2_method(row["method"]), row["method"])
                for quantity, column in (("cost", "cost"), ("co2e_g", "co2e")):
                    totals[(method, quantity)] = totals.get((method, quantity), 0.0) + float(row[column] or 0.0)
        return totals

    async def summary(
        self,
        cluster_uid: str,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        namespace: Optional[str] = None,
    ) -> List[SummaryRow]:
        """Signed totals per method, quantity, book, kind and accounting basis.

        With a window, an entry contributes the share of its period inside
        ``[start, end)``, as ``GET /recommendations/savings`` does.
        """
        if start is None and end is None:
            # All time: the materialized totals, not a scan of every entry (LED-09).
            namespace_sql = " AND namespace = ?" if namespace is not None else ""
            rows = await self._select(
                "SELECT method, quantity, unit, book, entry_kind, basis, reason_group, SUM(value) AS value, "
                "SUM(entries) AS entries FROM savings_ledger_totals WHERE cluster_uid = ?"
                f"{namespace_sql} GROUP BY method, quantity, unit, book, entry_kind, basis, reason_group "
                "ORDER BY method, quantity, book, entry_kind, basis, reason_group",
                [cluster_uid, *([namespace] if namespace is not None else [])],
            )
            return [self._summary_row(row) for row in rows]
        where, params = self._filters(cluster_uid, {"namespace": namespace}, start, end)
        share, share_params = self._share(start, end)
        rows = await self._select(
            "SELECT method, quantity, unit, book, entry_kind, basis, reason_group, SUM(value * share) AS value, "
            "COUNT(*) AS entries FROM (SELECT method, quantity, unit, book, entry_kind, "
            f"{_BASIS_SQL} AS basis, CASE WHEN {_COST_OF_FIX_SQL} THEN 'cost_of_fix' ELSE '' END AS reason_group, "
            f"value, {share} AS share FROM savings_ledger_entries WHERE {where}) AS windowed "
            "GROUP BY method, quantity, unit, book, entry_kind, basis, reason_group "
            "ORDER BY method, quantity, book, entry_kind, basis, reason_group",
            [*share_params, *params],
        )
        return [self._summary_row(row) for row in rows]

    @staticmethod
    def _summary_row(row: Mapping[str, Any]) -> SummaryRow:
        return SummaryRow(
            method=row["method"],
            quantity=row["quantity"],
            unit=row["unit"],
            book=row["book"],
            entry_kind=row["entry_kind"],
            value=float(row["value"] or 0.0),
            entries=int(row["entries"] or 0),
            basis=str(row["basis"]),
            reason_group=str(row["reason_group"] or ""),
        )

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
    "configured_chain_key",
    "configured_previous_chain_keys",
    "LedgerChainKeyError",
    "get_ledger_repository",
]
