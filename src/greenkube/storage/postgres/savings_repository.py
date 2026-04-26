# src/greenkube/storage/postgres/savings_repository.py
"""PostgreSQL implementation of the SavingsLedgerRepository."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from ...models.savings import SavingsLedgerRecord
from ..base_savings_repository import SavingsLedgerRepository

logger = logging.getLogger(__name__)


class PostgresSavingsLedgerRepository(SavingsLedgerRepository):
    """Persists and queries the recommendation savings ledger in PostgreSQL."""

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
                r.timestamp,
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
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                rows,
            )
        logger.debug("Saved %d savings ledger records to Postgres.", len(records))
        return len(records)

    async def get_cumulative_totals(self, cluster_name: str) -> Dict[str, Dict[str, float]]:
        """Combine raw + hourly totals into a single cumulative dict by type."""
        result: Dict[str, Dict[str, float]] = {}

        async with self._db.connection_scope() as conn:
            # Raw (uncompressed) records
            raw_rows = await conn.fetch(
                """
                SELECT recommendation_type,
                       SUM(co2e_saved_grams)   AS co2e,
                       SUM(cost_saved_dollars) AS cost
                FROM recommendation_savings_ledger
                WHERE cluster_name = $1
                GROUP BY recommendation_type
                """,
                cluster_name,
            )
            for row in raw_rows:
                t = row["recommendation_type"]
                result.setdefault(t, {"co2e_saved_grams": 0.0, "cost_saved_dollars": 0.0})
                result[t]["co2e_saved_grams"] += row["co2e"] or 0.0
                result[t]["cost_saved_dollars"] += row["cost"] or 0.0

            # Hourly aggregates
            hourly_rows = await conn.fetch(
                """
                SELECT recommendation_type,
                       SUM(co2e_saved_grams)   AS co2e,
                       SUM(cost_saved_dollars) AS cost
                FROM recommendation_savings_ledger_hourly
                WHERE cluster_name = $1
                GROUP BY recommendation_type
                """,
                cluster_name,
            )
            for row in hourly_rows:
                t = row["recommendation_type"]
                result.setdefault(t, {"co2e_saved_grams": 0.0, "cost_saved_dollars": 0.0})
                result[t]["co2e_saved_grams"] += row["co2e"] or 0.0
                result[t]["cost_saved_dollars"] += row["cost"] or 0.0

        return result

    async def compress_to_hourly(self, cutoff_hours: int = 24) -> int:
        """Aggregate raw records older than cutoff_hours into hourly buckets."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=cutoff_hours)

        async with self._db.connection_scope() as conn:
            result = await conn.execute(
                """
                INSERT INTO recommendation_savings_ledger_hourly
                    (recommendation_id, cluster_name, namespace,
                     recommendation_type, co2e_saved_grams,
                     cost_saved_dollars, sample_count, hour_bucket)
                SELECT
                    recommendation_id,
                    cluster_name,
                    namespace,
                    recommendation_type,
                    SUM(co2e_saved_grams)   AS co2e_saved_grams,
                    SUM(cost_saved_dollars) AS cost_saved_dollars,
                    COUNT(*)                AS sample_count,
                    date_trunc('hour', timestamp) AS hour_bucket
                FROM recommendation_savings_ledger
                WHERE timestamp < $1
                GROUP BY recommendation_id, cluster_name, namespace,
                         recommendation_type, date_trunc('hour', timestamp)
                ON CONFLICT (recommendation_id, hour_bucket) DO UPDATE SET
                    co2e_saved_grams   = EXCLUDED.co2e_saved_grams,
                    cost_saved_dollars = EXCLUDED.cost_saved_dollars,
                    sample_count       = EXCLUDED.sample_count
                """,
                cutoff,
            )
            count = int(result.split()[-1]) if result else 0

        if count:
            # Prune the raw rows we just compressed
            async with self._db.connection_scope() as conn:
                await conn.execute(
                    "DELETE FROM recommendation_savings_ledger WHERE timestamp < $1",
                    cutoff,
                )

        logger.debug("Compressed %d savings ledger records to hourly.", count)
        return count

    async def prune_raw(self, retention_days: int = 7) -> int:
        """Delete raw savings records older than retention_days."""
        if retention_days < 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        async with self._db.connection_scope() as conn:
            result = await conn.execute(
                "DELETE FROM recommendation_savings_ledger WHERE timestamp < $1",
                cutoff,
            )
        count = int(result.split()[-1]) if result else 0
        logger.debug("Pruned %d old raw savings ledger records.", count)
        return count
