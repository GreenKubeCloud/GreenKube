# src/greenkube/storage/sqlite/savings_repository.py
"""SQLite implementation of the SavingsLedgerRepository."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from ...models.savings import SavingsLedgerRecord
from ...utils.date_utils import to_iso_z
from ..base_savings_repository import SavingsLedgerRepository

logger = logging.getLogger(__name__)


class SQLiteSavingsLedgerRepository(SavingsLedgerRepository):
    """Persists and queries the recommendation savings ledger in SQLite."""

    def __init__(self, db_manager):
        self._db = db_manager

    async def save_records(self, records: List[SavingsLedgerRecord]) -> int:
        """Bulk-insert raw savings records for the current collection period."""
        if not records:
            return 0

        rows = [
            (
                r.recommendation_id,
                r.cluster_name,
                r.namespace,
                r.recommendation_type,
                r.co2e_saved_grams,
                r.cost_saved_dollars,
                r.period_seconds,
                r.timestamp.isoformat(),
            )
            for r in records
        ]

        async with self._db.connection_scope() as conn:
            await conn.executemany(
                """
                INSERT INTO recommendation_savings_ledger
                    (recommendation_id, cluster_name, namespace,
                     recommendation_type, co2e_saved_grams,
                     cost_saved_dollars, period_seconds, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            await conn.commit()
        logger.debug("Saved %d savings ledger records to SQLite.", len(records))
        return len(records)

    async def get_cumulative_totals(self, cluster_name: str) -> Dict[str, Dict[str, float]]:
        """Combine raw + hourly totals into a single cumulative dict by type."""
        result: Dict[str, Dict[str, float]] = {}

        async with self._db.connection_scope() as conn:
            cursor = await conn.execute(
                """
                SELECT recommendation_type,
                       SUM(co2e_saved_grams)   AS co2e,
                       SUM(cost_saved_dollars) AS cost
                FROM recommendation_savings_ledger
                WHERE cluster_name = ?
                GROUP BY recommendation_type
                """,
                (cluster_name,),
            )
            for row in await cursor.fetchall():
                t = row[0]
                result.setdefault(t, {"co2e_saved_grams": 0.0, "cost_saved_dollars": 0.0})
                result[t]["co2e_saved_grams"] += row[1] or 0.0
                result[t]["cost_saved_dollars"] += row[2] or 0.0

            cursor = await conn.execute(
                """
                SELECT recommendation_type,
                       SUM(co2e_saved_grams)   AS co2e,
                       SUM(cost_saved_dollars) AS cost
                FROM recommendation_savings_ledger_hourly
                WHERE cluster_name = ?
                GROUP BY recommendation_type
                """,
                (cluster_name,),
            )
            for row in await cursor.fetchall():
                t = row[0]
                result.setdefault(t, {"co2e_saved_grams": 0.0, "cost_saved_dollars": 0.0})
                result[t]["co2e_saved_grams"] += row[1] or 0.0
                result[t]["cost_saved_dollars"] += row[2] or 0.0

        return result

    async def get_window_totals(
        self,
        cluster_name: str,
        start_time: datetime,
        end_time: datetime,
        namespace: str | None = None,
    ) -> Dict[str, Dict[str, float]]:
        """Combine raw + hourly totals for an exact time window."""
        start = to_iso_z(start_time)
        end = to_iso_z(end_time)
        result: Dict[str, Dict[str, float]] = {}
        namespace_filter = ""
        raw_params: list = [cluster_name, start, end]
        hourly_params: list = [cluster_name, start, end]
        if namespace == "":
            namespace_filter = "\n                  AND (namespace IS NULL OR namespace = '')"
        elif namespace is not None:
            namespace_filter = "\n                  AND namespace = ?"
            raw_params.append(namespace)
            hourly_params.append(namespace)

        async with self._db.connection_scope() as conn:
            cursor = await conn.execute(
                f"""
                SELECT recommendation_type,
                       COALESCE(SUM(co2e_saved_grams), 0)   AS co2e,
                       COALESCE(SUM(cost_saved_dollars), 0) AS cost
                FROM recommendation_savings_ledger
                WHERE cluster_name = ?
                  AND timestamp >= ?
                                    AND timestamp <= ?{namespace_filter}
                GROUP BY recommendation_type
                """,
                tuple(raw_params),
            )
            for row in await cursor.fetchall():
                rec_type = row[0]
                result.setdefault(rec_type, {"co2e_saved_grams": 0.0, "cost_saved_dollars": 0.0})
                result[rec_type]["co2e_saved_grams"] += row[1] or 0.0
                result[rec_type]["cost_saved_dollars"] += row[2] or 0.0

            cursor = await conn.execute(
                f"""
                SELECT recommendation_type,
                       COALESCE(SUM(co2e_saved_grams), 0)   AS co2e,
                       COALESCE(SUM(cost_saved_dollars), 0) AS cost
                FROM recommendation_savings_ledger_hourly
                WHERE cluster_name = ?
                  AND hour_bucket >= ?
                                    AND hour_bucket <= ?{namespace_filter}
                GROUP BY recommendation_type
                """,
                tuple(hourly_params),
            )
            for row in await cursor.fetchall():
                rec_type = row[0]
                result.setdefault(rec_type, {"co2e_saved_grams": 0.0, "cost_saved_dollars": 0.0})
                result[rec_type]["co2e_saved_grams"] += row[1] or 0.0
                result[rec_type]["cost_saved_dollars"] += row[2] or 0.0

        return result

    async def compress_to_hourly(self, cutoff_hours: int = 24) -> int:
        """Aggregate raw records older than cutoff_hours into hourly buckets."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=cutoff_hours)).isoformat()

        async with self._db.connection_scope() as conn:
            cursor = await conn.execute(
                """
                INSERT OR REPLACE INTO recommendation_savings_ledger_hourly
                    (recommendation_id, cluster_name, namespace,
                     recommendation_type, co2e_saved_grams,
                     cost_saved_dollars, sample_count, hour_bucket)
                SELECT
                    recommendation_id,
                    cluster_name,
                    namespace,
                    recommendation_type,
                    SUM(co2e_saved_grams),
                    SUM(cost_saved_dollars),
                    COUNT(*),
                    strftime('%Y-%m-%dT%H:00:00Z', timestamp) AS hour_bucket
                FROM recommendation_savings_ledger
                WHERE timestamp < ?
                GROUP BY recommendation_id, cluster_name, namespace,
                         recommendation_type,
                         strftime('%Y-%m-%dT%H:00:00Z', timestamp)
                """,
                (cutoff,),
            )
            count = cursor.rowcount
            await conn.execute(
                "DELETE FROM recommendation_savings_ledger WHERE timestamp < ?",
                (cutoff,),
            )
            await conn.commit()

        logger.debug("Compressed %d savings ledger records to hourly.", count)
        return count

    async def prune_raw(self, retention_days: int = 7) -> int:
        """Delete raw savings records older than retention_days."""
        if retention_days < 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        async with self._db.connection_scope() as conn:
            cursor = await conn.execute(
                "DELETE FROM recommendation_savings_ledger WHERE timestamp < ?",
                (cutoff,),
            )
            count = cursor.rowcount
            await conn.commit()
        return count
