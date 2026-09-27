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
                r.measurement_method,
                r.baseline_value,
                r.actual_value,
                r.confidence,
                bool(r.superseded),
            )
            for r in records
        ]

        async with self._db.connection_scope() as conn:
            await conn.executemany(
                """
                INSERT INTO recommendation_savings_ledger
                    (recommendation_id, cluster_name, namespace,
                     recommendation_type, co2e_saved_grams,
                     cost_saved_dollars, period_seconds, timestamp,
                     measurement_method, baseline_value, actual_value,
                     confidence, superseded)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                """,
                rows,
            )
        logger.debug("Saved %d savings ledger records to Postgres.", len(records))
        return len(records)

    async def get_cumulative_totals(
        self,
        cluster_name: str,
        group_by_method: bool = False,
    ) -> Dict[str, Dict[str, float]]:
        """Combine raw + hourly totals into a single cumulative dict."""
        column = "measurement_method" if group_by_method else "recommendation_type"
        result: Dict[str, Dict[str, float]] = {}

        async with self._db.connection_scope() as conn:
            for table in ("recommendation_savings_ledger", "recommendation_savings_ledger_hourly"):
                rows = await conn.fetch(
                    f"""
                    SELECT {column} AS group_key,
                           SUM(co2e_saved_grams)   AS co2e,
                           SUM(cost_saved_dollars) AS cost
                    FROM {table}
                    WHERE cluster_name = $1
                      AND COALESCE(superseded, FALSE) = FALSE
                    GROUP BY {column}
                    """,
                    cluster_name,
                )
                for row in rows:
                    key = row["group_key"] or ("prorated" if group_by_method else "unknown")
                    result.setdefault(key, {"co2e_saved_grams": 0.0, "cost_saved_dollars": 0.0})
                    result[key]["co2e_saved_grams"] += row["co2e"] or 0.0
                    result[key]["cost_saved_dollars"] += row["cost"] or 0.0

        return result

    async def get_window_totals(
        self,
        cluster_name: str,
        start_time: datetime,
        end_time: datetime,
        namespace: str | None = None,
        group_by_method: bool = False,
    ) -> Dict[str, Dict[str, float]]:
        """Combine raw + hourly totals for an exact time window."""
        column = "measurement_method" if group_by_method else "recommendation_type"
        params = [cluster_name, start_time, end_time]
        namespace_filter = ""
        if namespace == "":
            namespace_filter = "\n                      AND (namespace IS NULL OR namespace = '')"
        elif namespace is not None:
            params.append(namespace)
            namespace_filter = f"\n                      AND namespace = ${len(params)}"

        async with self._db.connection_scope() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {column} AS group_key,
                       COALESCE(SUM(co2e_saved_grams), 0)   AS co2e,
                       COALESCE(SUM(cost_saved_dollars), 0) AS cost
                FROM (
                    SELECT {column}, co2e_saved_grams, cost_saved_dollars
                    FROM recommendation_savings_ledger
                    WHERE cluster_name = $1
                      AND COALESCE(superseded, FALSE) = FALSE
                      AND timestamp >= $2
                                            AND timestamp <= $3{namespace_filter}
                    UNION ALL
                    SELECT {column}, co2e_saved_grams, cost_saved_dollars
                    FROM recommendation_savings_ledger_hourly
                    WHERE cluster_name = $1
                      AND COALESCE(superseded, FALSE) = FALSE
                      AND hour_bucket >= $2
                                            AND hour_bucket <= $3{namespace_filter}
                ) AS savings
                GROUP BY {column}
                """,
                *params,
            )

        return {
            (row["group_key"] or ("prorated" if group_by_method else "unknown")): {
                "co2e_saved_grams": row["co2e"] or 0.0,
                "cost_saved_dollars": row["cost"] or 0.0,
            }
            for row in rows
        }

    async def supersede_for_recommendation(self, recommendation_id: int) -> int:
        """Flags every attribution row for a recommendation as superseded."""
        total = 0
        async with self._db.connection_scope() as conn:
            for table in ("recommendation_savings_ledger", "recommendation_savings_ledger_hourly"):
                result = await conn.execute(
                    f"UPDATE {table} SET superseded = TRUE "
                    "WHERE recommendation_id = $1 AND COALESCE(superseded, FALSE) = FALSE",
                    recommendation_id,
                )
                total += int(result.split()[-1]) if result else 0
        if total:
            logger.info("Superseded %d savings ledger row(s) for recommendation %d.", total, recommendation_id)
        return total

    async def compress_to_hourly(self, cutoff_hours: int = 24) -> int:
        """Aggregate raw records older than cutoff_hours into hourly buckets."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=cutoff_hours)

        async with self._db.connection_scope() as conn:
            result = await conn.execute(
                """
                INSERT INTO recommendation_savings_ledger_hourly
                    (recommendation_id, cluster_name, namespace,
                     recommendation_type, co2e_saved_grams,
                     cost_saved_dollars, sample_count, hour_bucket,
                     measurement_method, baseline_value, actual_value,
                     confidence, superseded)
                SELECT
                    recommendation_id,
                    cluster_name,
                    namespace,
                    recommendation_type,
                    SUM(co2e_saved_grams)   AS co2e_saved_grams,
                    SUM(cost_saved_dollars) AS cost_saved_dollars,
                    COUNT(*)                AS sample_count,
                    date_trunc('hour', timestamp) AS hour_bucket,
                    COALESCE(measurement_method, 'prorated') AS measurement_method,
                    MAX(baseline_value),
                    MAX(actual_value),
                    MAX(confidence),
                    BOOL_OR(COALESCE(superseded, FALSE))
                FROM recommendation_savings_ledger
                WHERE timestamp < $1
                GROUP BY recommendation_id, cluster_name, namespace,
                         recommendation_type, date_trunc('hour', timestamp),
                         COALESCE(measurement_method, 'prorated')
                ON CONFLICT (recommendation_id, hour_bucket, measurement_method) DO UPDATE SET
                    co2e_saved_grams   = EXCLUDED.co2e_saved_grams,
                    cost_saved_dollars = EXCLUDED.cost_saved_dollars,
                    sample_count       = EXCLUDED.sample_count,
                    superseded         = EXCLUDED.superseded
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

    async def prune_hourly(self, retention_days: int = -1) -> int:
        """Delete hourly-compressed savings ledger rows older than retention_days.

        Set retention_days to -1 to disable pruning (keep indefinitely).
        """
        if retention_days < 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        async with self._db.connection_scope() as conn:
            result = await conn.execute(
                "DELETE FROM recommendation_savings_ledger_hourly WHERE hour_bucket < $1",
                cutoff,
            )
        count = int(result.split()[-1]) if result else 0
        if count:
            logger.info("Pruned %d hourly savings ledger rows older than %d days.", count, retention_days)
        return count
