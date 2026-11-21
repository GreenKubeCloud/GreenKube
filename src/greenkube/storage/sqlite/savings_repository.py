# src/greenkube/storage/sqlite/savings_repository.py
"""SQLite implementation of the append-only SavingsLedgerRepository."""

import logging
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from ...models.savings import SavingsAccrualTotal, SavingsCounterTotal, SavingsLedgerRecord, normalize_savings_method
from ...utils.date_utils import ensure_utc, to_iso_z
from ..base_savings_repository import ATTRIBUTION_STREAM, AccrualWatermarkConflict, SavingsLedgerRepository

logger = logging.getLogger(__name__)

_INSERT_SQL = """
    INSERT INTO recommendation_savings_ledger
        (recommendation_id, cluster_name, namespace,
         recommendation_type, co2e_saved_grams,
         cost_saved_dollars, period_seconds, timestamp,
         period_start, period_end, slot_start,
         measurement_method, baseline_value, actual_value,
         confidence, superseded, basis, row_kind,
         annual_cost_rate, annual_co2e_rate, currency, measurement_id)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT DO NOTHING
"""

# Share of a row's period that falls inside the [:start, :end) window.
_WINDOW_SHARE_SQL = """
    CASE
        WHEN period_start >= :start AND period_end <= :end THEN 1.0
        ELSE MAX(
            0.0,
            MIN(julianday(period_end), julianday(:end)) - MAX(julianday(period_start), julianday(:start))
        ) / NULLIF(julianday(period_end) - julianday(period_start), 0)
    END
"""

_ID_CHUNK = 500


def _bound(value: datetime) -> str:
    """Serialize a window bound in the canonical whole-second ledger format."""
    return to_iso_z(ensure_utc(value).replace(microsecond=0))


def _row_params(record: SavingsLedgerRecord) -> tuple:
    return (
        record.recommendation_id,
        record.cluster_name,
        record.namespace or "",
        record.recommendation_type,
        record.co2e_saved_grams,
        record.cost_saved_dollars,
        record.period_seconds,
        to_iso_z(record.timestamp),
        to_iso_z(record.period_start or record.timestamp),
        to_iso_z(record.period_end or record.timestamp),
        to_iso_z(record.slot_start) if record.slot_start is not None else None,
        normalize_savings_method(record.measurement_method),
        record.baseline_value,
        record.actual_value,
        record.confidence,
        int(bool(record.superseded)),
        record.basis,
        record.row_kind,
        record.annual_cost_rate,
        record.annual_co2e_rate,
        record.currency,
        record.measurement_id,
    )


class SQLiteSavingsLedgerRepository(SavingsLedgerRepository):
    """Persists and queries the recommendation savings ledger in SQLite."""

    def __init__(self, db_manager):
        self._db = db_manager

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def _insert(self, conn, records: List[SavingsLedgerRecord]) -> int:
        if not records:
            return 0
        cursor = await conn.executemany(_INSERT_SQL, [_row_params(r) for r in records])
        return max(cursor.rowcount, 0)

    async def save_records(self, records: List[SavingsLedgerRecord]) -> int:
        """Append accrual rows; rows whose key already exists are left untouched."""
        if not records:
            return 0
        async with self._db.connection_scope() as conn:
            try:
                inserted = await self._insert(conn, records)
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        logger.debug("Appended %d/%d savings ledger rows to SQLite.", inserted, len(records))
        return inserted

    async def append_accruals(
        self,
        records: List[SavingsLedgerRecord],
        *,
        cluster_name: str,
        stream: str,
        expected_watermark: Optional[datetime],
        new_watermark: datetime,
    ) -> int:
        """Compare-and-swap the watermark and append rows in one transaction."""
        now = to_iso_z(datetime.now(timezone.utc))
        async with self._db.connection_scope() as conn:
            try:
                if expected_watermark is None:
                    cursor = await conn.execute(
                        """
                        INSERT INTO accrual_watermarks (cluster_name, stream, watermark, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT DO NOTHING
                        """,
                        (cluster_name, stream, to_iso_z(new_watermark), now),
                    )
                else:
                    cursor = await conn.execute(
                        """
                        UPDATE accrual_watermarks
                        SET watermark = ?, updated_at = ?
                        WHERE cluster_name = ? AND stream = ? AND watermark = ?
                        """,
                        (to_iso_z(new_watermark), now, cluster_name, stream, to_iso_z(expected_watermark)),
                    )
                if cursor.rowcount != 1:
                    raise AccrualWatermarkConflict(
                        f"Accrual watermark for {cluster_name}/{stream} moved; another writer is attributing."
                    )
                inserted = await self._insert(conn, records)
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return inserted

    async def supersede_for_recommendation(self, recommendation_id: int) -> int:
        """Flags every attribution row for a recommendation as superseded."""
        total = 0
        async with self._db.connection_scope() as conn:
            try:
                for table in ("recommendation_savings_ledger", "recommendation_savings_ledger_hourly"):
                    cursor = await conn.execute(
                        f"UPDATE {table} SET superseded = 1 WHERE recommendation_id = ? AND superseded = 0",
                        (recommendation_id,),
                    )
                    total += cursor.rowcount
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        if total:
            logger.info("Superseded %d savings ledger row(s) for recommendation %d.", total, recommendation_id)
        return total

    # ------------------------------------------------------------------
    # Watermarks
    # ------------------------------------------------------------------

    async def get_accrual_watermark(self, cluster_name: str, stream: str = ATTRIBUTION_STREAM) -> Optional[datetime]:
        async with self._db.connection_scope() as conn:
            cursor = await conn.execute(
                "SELECT watermark FROM accrual_watermarks WHERE cluster_name = ? AND stream = ?",
                (cluster_name, stream),
            )
            row = await cursor.fetchone()
        return ensure_utc(row[0]) if row and row[0] else None

    async def get_accrual_floors(self, recommendation_ids: Iterable[int]) -> Dict[int, datetime]:
        ids = sorted({int(i) for i in recommendation_ids})
        floors: Dict[int, datetime] = {}
        if not ids:
            return floors
        async with self._db.connection_scope() as conn:
            for offset in range(0, len(ids), _ID_CHUNK):
                chunk = ids[offset : offset + _ID_CHUNK]
                placeholders = ", ".join("?" for _ in chunk)
                cursor = await conn.execute(
                    f"""
                    SELECT recommendation_id, MAX(period_end)
                    FROM savings_accruals_v1
                    WHERE recommendation_id IN ({placeholders})
                    GROUP BY recommendation_id
                    """,
                    chunk,
                )
                for rec_id, period_end in await cursor.fetchall():
                    if period_end:
                        floors[int(rec_id)] = ensure_utc(period_end)
        return floors

    async def get_measured_totals(self, recommendation_id: int, start: datetime, end: datetime) -> Tuple[float, float]:
        """Sum of the measured grid accruals of one recommendation inside ``[start, end)`` (LED-12)."""
        params = {"rec": int(recommendation_id), "start": _bound(start), "end": _bound(end)}
        async with self._db.connection_scope() as conn:
            cursor = await conn.execute(
                f"""
                SELECT SUM(co2e_saved_grams * share), SUM(cost_saved_dollars * share)
                FROM (
                    SELECT co2e_saved_grams, cost_saved_dollars, {_WINDOW_SHARE_SQL} AS share
                    FROM savings_accruals_v1
                    WHERE recommendation_id = :rec AND measurement_method = 'measured'
                      AND slot_start IS NOT NULL AND superseded = 0
                      AND period_end > :start AND period_start < :end
                ) AS entries
                """,
                params,
            )
            row = await cursor.fetchone()
        return (float(row[0] or 0.0), float(row[1] or 0.0)) if row else (0.0, 0.0)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_accrual_breakdown(
        self,
        cluster_name: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        namespace: Optional[str] = None,
    ) -> List[SavingsAccrualTotal]:
        params: dict = {"cluster": cluster_name}
        share = "1.0"
        filters = ""
        if start_time is not None and end_time is not None:
            params.update(start=_bound(start_time), end=_bound(end_time))
            share = _WINDOW_SHARE_SQL
            filters += " AND period_end > :start AND period_start < :end"
        if namespace == "":
            filters += " AND (namespace IS NULL OR namespace = '')"
        elif namespace is not None:
            params["namespace"] = namespace
            filters += " AND namespace = :namespace"

        async with self._db.connection_scope() as conn:
            cursor = await conn.execute(
                f"""
                SELECT namespace, recommendation_type, measurement_method,
                       SUM(co2e_saved_grams * share) AS co2e,
                       SUM(cost_saved_dollars * share) AS cost
                FROM (
                    SELECT namespace, recommendation_type, measurement_method,
                           co2e_saved_grams, cost_saved_dollars,
                           {share} AS share
                    FROM savings_accruals_v1
                    WHERE cluster_name = :cluster
                      AND superseded = 0{filters}
                ) AS entries
                GROUP BY namespace, recommendation_type, measurement_method
                """,
                params,
            )
            rows = await cursor.fetchall()

        return [
            SavingsAccrualTotal(
                namespace=row[0] or "",
                recommendation_type=row[1] or "unknown",
                method=normalize_savings_method(row[2]),
                co2e_saved_grams=float(row[3] or 0.0),
                cost_saved_dollars=float(row[4] or 0.0),
            )
            for row in rows
        ]

    async def get_lifetime_counters(self, cluster_name: str) -> List[SavingsCounterTotal]:
        async with self._db.connection_scope() as conn:
            cursor = await conn.execute(
                """
                SELECT recommendation_type, measurement_method,
                       SUM(CASE WHEN co2e_saved_grams > 0 THEN co2e_saved_grams ELSE 0 END)
                         + SUM(CASE WHEN superseded <> 0 AND co2e_saved_grams < 0
                                    THEN -co2e_saved_grams ELSE 0 END),
                       SUM(CASE WHEN cost_saved_dollars > 0 THEN cost_saved_dollars ELSE 0 END)
                         + SUM(CASE WHEN superseded <> 0 AND cost_saved_dollars < 0
                                    THEN -cost_saved_dollars ELSE 0 END),
                       SUM(CASE WHEN co2e_saved_grams < 0 THEN -co2e_saved_grams ELSE 0 END)
                         + SUM(CASE WHEN superseded <> 0 AND co2e_saved_grams > 0
                                    THEN co2e_saved_grams ELSE 0 END),
                       SUM(CASE WHEN cost_saved_dollars < 0 THEN -cost_saved_dollars ELSE 0 END)
                         + SUM(CASE WHEN superseded <> 0 AND cost_saved_dollars > 0
                                    THEN cost_saved_dollars ELSE 0 END)
                FROM savings_accruals_v1
                WHERE cluster_name = ?
                GROUP BY recommendation_type, measurement_method
                """,
                (cluster_name,),
            )
            rows = await cursor.fetchall()
        return [
            SavingsCounterTotal(
                recommendation_type=row[0] or "unknown",
                method=normalize_savings_method(row[1]),
                accrued_co2e_grams=float(row[2] or 0.0),
                accrued_cost_dollars=float(row[3] or 0.0),
                reversed_co2e_grams=float(row[4] or 0.0),
                reversed_cost_dollars=float(row[5] or 0.0),
            )
            for row in rows
        ]

    async def has_legacy_compressed_rows(
        self,
        cluster_name: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        namespace: Optional[str] = None,
    ) -> bool:
        params: list = [cluster_name]
        window = ""
        if start_time is not None and end_time is not None:
            window = " AND period_end > ? AND period_start < ?"
            params.extend([_bound(start_time), _bound(end_time)])
        if namespace == "":
            window += " AND (namespace IS NULL OR namespace = '')"
        elif namespace is not None:
            window += " AND namespace = ?"
            params.append(namespace)
        async with self._db.connection_scope() as conn:
            cursor = await conn.execute(
                f"""
                SELECT 1 FROM recommendation_savings_ledger_hourly
                WHERE cluster_name = ? AND legacy_compressed = 1{window}
                LIMIT 1
                """,
                params,
            )
            return await cursor.fetchone() is not None
