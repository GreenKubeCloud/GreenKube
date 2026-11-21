# src/greenkube/storage/postgres/savings_repository.py
"""PostgreSQL implementation of the append-only SavingsLedgerRepository."""

import logging
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from ...models.savings import SavingsAccrualTotal, SavingsCounterTotal, SavingsLedgerRecord, normalize_savings_method
from ...utils.date_utils import ensure_utc
from ..base_savings_repository import ATTRIBUTION_STREAM, AccrualWatermarkConflict, SavingsLedgerRepository

logger = logging.getLogger(__name__)

_INSERT_SQL = """
    INSERT INTO recommendation_savings_ledger
        (recommendation_id, cluster_name, namespace,
         recommendation_type, co2e_saved_grams,
         cost_saved_dollars, period_seconds, timestamp,
         period_start, period_end, slot_start,
         measurement_method, baseline_value, actual_value,
         confidence, superseded, basis, row_kind)
    SELECT * FROM unnest(
        $1::INTEGER[], $2::TEXT[], $3::TEXT[], $4::TEXT[], $5::DOUBLE PRECISION[],
        $6::DOUBLE PRECISION[], $7::INTEGER[], $8::TIMESTAMPTZ[], $9::TIMESTAMPTZ[],
        $10::TIMESTAMPTZ[], $11::TIMESTAMPTZ[], $12::TEXT[], $13::DOUBLE PRECISION[],
        $14::DOUBLE PRECISION[], $15::DOUBLE PRECISION[], $16::BOOLEAN[], $17::TEXT[], $18::TEXT[]
    )
    ON CONFLICT DO NOTHING
"""
_INSERT_CHUNK = 5000


def _row_params(record: SavingsLedgerRecord) -> tuple:
    return (
        record.recommendation_id,
        record.cluster_name,
        record.namespace or "",
        record.recommendation_type,
        record.co2e_saved_grams,
        record.cost_saved_dollars,
        record.period_seconds,
        ensure_utc(record.timestamp),
        ensure_utc(record.period_start or record.timestamp),
        ensure_utc(record.period_end or record.timestamp),
        ensure_utc(record.slot_start) if record.slot_start is not None else None,
        normalize_savings_method(record.measurement_method),
        record.baseline_value,
        record.actual_value,
        record.confidence,
        bool(record.superseded),
        record.basis,
        record.row_kind,
    )


def _command_count(result: str | None) -> int:
    try:
        return int(result.split()[-1]) if result else 0
    except (ValueError, IndexError):
        return 0


class PostgresSavingsLedgerRepository(SavingsLedgerRepository):
    """Persists and queries the recommendation savings ledger in PostgreSQL."""

    def __init__(self, db_manager):
        self._db = db_manager

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    @staticmethod
    async def _insert(conn, records: List[SavingsLedgerRecord]) -> int:
        inserted = 0
        for offset in range(0, len(records), _INSERT_CHUNK):
            rows = [_row_params(record) for record in records[offset : offset + _INSERT_CHUNK]]
            columns = [list(column) for column in zip(*rows)]
            inserted += _command_count(await conn.execute(_INSERT_SQL, *columns))
        return inserted

    async def save_records(self, records: List[SavingsLedgerRecord]) -> int:
        """Append accrual rows; rows whose key already exists are left untouched."""
        if not records:
            return 0
        async with self._db.connection_scope() as conn:
            async with conn.transaction():
                inserted = await self._insert(conn, records)
        logger.debug("Appended %d/%d savings ledger rows to Postgres.", inserted, len(records))
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
        async with self._db.connection_scope() as conn:
            async with conn.transaction():
                if expected_watermark is None:
                    result = await conn.execute(
                        """
                        INSERT INTO accrual_watermarks (cluster_name, stream, watermark, updated_at)
                        VALUES ($1, $2, $3, NOW())
                        ON CONFLICT DO NOTHING
                        """,
                        cluster_name,
                        stream,
                        ensure_utc(new_watermark),
                    )
                else:
                    result = await conn.execute(
                        """
                        UPDATE accrual_watermarks
                        SET watermark = $1, updated_at = NOW()
                        WHERE cluster_name = $2 AND stream = $3 AND watermark = $4
                        """,
                        ensure_utc(new_watermark),
                        cluster_name,
                        stream,
                        ensure_utc(expected_watermark),
                    )
                if _command_count(result) != 1:
                    raise AccrualWatermarkConflict(
                        f"Accrual watermark for {cluster_name}/{stream} moved; another writer is attributing."
                    )
                return await self._insert(conn, records)

    async def supersede_for_recommendation(self, recommendation_id: int) -> int:
        """Flags every attribution row for a recommendation as superseded."""
        total = 0
        async with self._db.connection_scope() as conn:
            async with conn.transaction():
                for table in ("recommendation_savings_ledger", "recommendation_savings_ledger_hourly"):
                    result = await conn.execute(
                        f"UPDATE {table} SET superseded = TRUE WHERE recommendation_id = $1 AND NOT superseded",
                        recommendation_id,
                    )
                    total += _command_count(result)
        if total:
            logger.info("Superseded %d savings ledger row(s) for recommendation %d.", total, recommendation_id)
        return total

    # ------------------------------------------------------------------
    # Watermarks
    # ------------------------------------------------------------------

    async def get_accrual_watermark(self, cluster_name: str, stream: str = ATTRIBUTION_STREAM) -> Optional[datetime]:
        async with self._db.connection_scope() as conn:
            value = await conn.fetchval(
                "SELECT watermark FROM accrual_watermarks WHERE cluster_name = $1 AND stream = $2",
                cluster_name,
                stream,
            )
        return ensure_utc(value) if value is not None else None

    async def get_database_time(self) -> Optional[datetime]:
        """Return the PostgreSQL server clock (LED-06): an independent witness of the process clock."""
        async with self._db.connection_scope() as conn:
            value = await conn.fetchval("SELECT clock_timestamp()")
        return ensure_utc(value) if value is not None else None

    async def get_accrual_floors(self, recommendation_ids: Iterable[int]) -> Dict[int, datetime]:
        ids = sorted({int(i) for i in recommendation_ids})
        if not ids:
            return {}
        async with self._db.connection_scope() as conn:
            rows = await conn.fetch(
                """
                SELECT recommendation_id, MAX(period_end) AS period_end
                FROM savings_accruals_v1
                WHERE recommendation_id = ANY($1::INTEGER[])
                GROUP BY recommendation_id
                """,
                ids,
            )
        return {int(row["recommendation_id"]): ensure_utc(row["period_end"]) for row in rows if row["period_end"]}

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
        params: list = [cluster_name]
        share = "1.0"
        filters = ""
        if start_time is not None and end_time is not None:
            params.extend([ensure_utc(start_time), ensure_utc(end_time)])
            share = """
                CASE
                    WHEN period_start >= $2 AND period_end <= $3 THEN 1.0
                    ELSE GREATEST(
                        0.0,
                        EXTRACT(EPOCH FROM (LEAST(period_end, $3) - GREATEST(period_start, $2)))
                    ) / NULLIF(EXTRACT(EPOCH FROM (period_end - period_start)), 0)
                END
            """
            filters += " AND period_end > $2 AND period_start < $3"
        if namespace == "":
            filters += " AND (namespace IS NULL OR namespace = '')"
        elif namespace is not None:
            params.append(namespace)
            filters += f" AND namespace = ${len(params)}"

        async with self._db.connection_scope() as conn:
            rows = await conn.fetch(
                f"""
                SELECT namespace, recommendation_type, measurement_method,
                       SUM(co2e_saved_grams * share) AS co2e,
                       SUM(cost_saved_dollars * share) AS cost
                FROM (
                    SELECT namespace, recommendation_type, measurement_method,
                           co2e_saved_grams, cost_saved_dollars,
                           ({share})::DOUBLE PRECISION AS share
                    FROM savings_accruals_v1
                    WHERE cluster_name = $1
                      AND NOT superseded{filters}
                ) AS entries
                GROUP BY namespace, recommendation_type, measurement_method
                """,
                *params,
            )
        return [
            SavingsAccrualTotal(
                namespace=row["namespace"] or "",
                recommendation_type=row["recommendation_type"] or "unknown",
                method=normalize_savings_method(row["measurement_method"]),
                co2e_saved_grams=float(row["co2e"] or 0.0),
                cost_saved_dollars=float(row["cost"] or 0.0),
            )
            for row in rows
        ]

    async def get_lifetime_counters(self, cluster_name: str) -> List[SavingsCounterTotal]:
        async with self._db.connection_scope() as conn:
            rows = await conn.fetch(
                """
                SELECT recommendation_type, measurement_method,
                       SUM(GREATEST(co2e_saved_grams, 0))
                         + SUM(CASE WHEN superseded THEN GREATEST(-co2e_saved_grams, 0) ELSE 0 END)
                         AS accrued_co2e,
                       SUM(GREATEST(cost_saved_dollars, 0))
                         + SUM(CASE WHEN superseded THEN GREATEST(-cost_saved_dollars, 0) ELSE 0 END)
                         AS accrued_cost,
                       SUM(GREATEST(-co2e_saved_grams, 0))
                         + SUM(CASE WHEN superseded THEN GREATEST(co2e_saved_grams, 0) ELSE 0 END)
                         AS reversed_co2e,
                       SUM(GREATEST(-cost_saved_dollars, 0))
                         + SUM(CASE WHEN superseded THEN GREATEST(cost_saved_dollars, 0) ELSE 0 END)
                         AS reversed_cost
                FROM savings_accruals_v1
                WHERE cluster_name = $1
                GROUP BY recommendation_type, measurement_method
                """,
                cluster_name,
            )
        return [
            SavingsCounterTotal(
                recommendation_type=row["recommendation_type"] or "unknown",
                method=normalize_savings_method(row["measurement_method"]),
                accrued_co2e_grams=float(row["accrued_co2e"] or 0.0),
                accrued_cost_dollars=float(row["accrued_cost"] or 0.0),
                reversed_co2e_grams=float(row["reversed_co2e"] or 0.0),
                reversed_cost_dollars=float(row["reversed_cost"] or 0.0),
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
            window = " AND period_end > $2 AND period_start < $3"
            params.extend([ensure_utc(start_time), ensure_utc(end_time)])
        if namespace == "":
            window += " AND (namespace IS NULL OR namespace = '')"
        elif namespace is not None:
            params.append(namespace)
            window += f" AND namespace = ${len(params)}"
        async with self._db.connection_scope() as conn:
            value = await conn.fetchval(
                f"""
                SELECT 1 FROM recommendation_savings_ledger_hourly
                WHERE cluster_name = $1 AND legacy_compressed{window}
                LIMIT 1
                """,
                *params,
            )
        return value is not None
