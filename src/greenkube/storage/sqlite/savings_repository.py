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
                to_iso_z(r.timestamp),
                to_iso_z(r.period_start or r.timestamp),
                to_iso_z(r.period_end or r.timestamp),
                r.measurement_method,
                r.baseline_value,
                r.actual_value,
                r.confidence,
                int(bool(r.superseded)),
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
                     period_start, period_end,
                     measurement_method, baseline_value, actual_value,
                     confidence, superseded)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (recommendation_id, period_start, period_end, measurement_method)
                DO UPDATE SET
                     cluster_name = excluded.cluster_name,
                     namespace = excluded.namespace,
                     recommendation_type = excluded.recommendation_type,
                     co2e_saved_grams = excluded.co2e_saved_grams,
                     cost_saved_dollars = excluded.cost_saved_dollars,
                     period_seconds = excluded.period_seconds,
                     timestamp = excluded.timestamp,
                     baseline_value = excluded.baseline_value,
                     actual_value = excluded.actual_value,
                     confidence = excluded.confidence,
                     superseded = excluded.superseded
                """,
                rows,
            )
            for record in records:
                if record.measurement_method == "measured":
                    period_start = record.period_start or record.timestamp
                    period_end = record.period_end or record.timestamp
                    await conn.execute(
                        """
                         UPDATE recommendation_savings_ledger
                         SET superseded = 1
                         WHERE recommendation_id = ?
                           AND measurement_method = 'prorated'
                           AND period_start < ?
                           AND period_end > ?
                         """,
                        (record.recommendation_id, to_iso_z(period_end), to_iso_z(period_start)),
                    )
                    await conn.execute(
                        """
                         UPDATE recommendation_savings_ledger_hourly
                         SET superseded = 1
                         WHERE recommendation_id = ?
                           AND measurement_method = 'prorated'
                           AND period_start < ?
                           AND period_end > ?
                         """,
                        (record.recommendation_id, to_iso_z(period_end), to_iso_z(period_start)),
                    )
            await conn.commit()
        logger.debug("Saved %d savings ledger records to SQLite.", len(records))
        return len(records)

    async def get_cumulative_totals(
        self,
        cluster_name: str,
        group_by_method: bool = False,
    ) -> Dict[str, Dict[str, float]]:
        """Combine raw + hourly totals into a single cumulative dict.

        Groups by recommendation type by default, or by measurement method when
        ``group_by_method`` is set. Superseded rows are always excluded.
        """
        column = "measurement_method" if group_by_method else "recommendation_type"
        result: Dict[str, Dict[str, float]] = {}

        async with self._db.connection_scope() as conn:
            for table in ("recommendation_savings_ledger", "recommendation_savings_ledger_hourly"):
                cursor = await conn.execute(
                    f"""
                    SELECT {column},
                           SUM(co2e_saved_grams)   AS co2e,
                           SUM(cost_saved_dollars) AS cost
                    FROM {table}
                    WHERE cluster_name = ?
                      AND COALESCE(superseded, 0) = 0
                    GROUP BY {column}
                    """,
                    (cluster_name,),
                )
                for row in await cursor.fetchall():
                    key = row[0] or ("prorated" if group_by_method else "unknown")
                    result.setdefault(key, {"co2e_saved_grams": 0.0, "cost_saved_dollars": 0.0})
                    result[key]["co2e_saved_grams"] += row[1] or 0.0
                    result[key]["cost_saved_dollars"] += row[2] or 0.0

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
        start = to_iso_z(start_time)
        end = to_iso_z(end_time)
        column = "measurement_method" if group_by_method else "recommendation_type"
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
                SELECT {column},
                       COALESCE(SUM(co2e_saved_grams), 0)   AS co2e,
                       COALESCE(SUM(cost_saved_dollars), 0) AS cost
                FROM recommendation_savings_ledger
                WHERE cluster_name = ?
                  AND COALESCE(superseded, 0) = 0
                  AND timestamp >= ?
                                    AND timestamp <= ?{namespace_filter}
                GROUP BY {column}
                """,
                tuple(raw_params),
            )
            for row in await cursor.fetchall():
                key = row[0] or ("prorated" if group_by_method else "unknown")
                result.setdefault(key, {"co2e_saved_grams": 0.0, "cost_saved_dollars": 0.0})
                result[key]["co2e_saved_grams"] += row[1] or 0.0
                result[key]["cost_saved_dollars"] += row[2] or 0.0

            cursor = await conn.execute(
                f"""
                SELECT {column},
                       COALESCE(SUM(co2e_saved_grams), 0)   AS co2e,
                       COALESCE(SUM(cost_saved_dollars), 0) AS cost
                FROM recommendation_savings_ledger_hourly
                WHERE cluster_name = ?
                  AND COALESCE(superseded, 0) = 0
                  AND hour_bucket >= ?
                                    AND hour_bucket <= ?{namespace_filter}
                GROUP BY {column}
                """,
                tuple(hourly_params),
            )
            for row in await cursor.fetchall():
                key = row[0] or ("prorated" if group_by_method else "unknown")
                result.setdefault(key, {"co2e_saved_grams": 0.0, "cost_saved_dollars": 0.0})
                result[key]["co2e_saved_grams"] += row[1] or 0.0
                result[key]["cost_saved_dollars"] += row[2] or 0.0

        return result

    async def supersede_for_recommendation(self, recommendation_id: int) -> int:
        """Flags every attribution row for a recommendation as superseded."""
        total = 0
        async with self._db.connection_scope() as conn:
            for table in ("recommendation_savings_ledger", "recommendation_savings_ledger_hourly"):
                cursor = await conn.execute(
                    f"UPDATE {table} SET superseded = 1 WHERE recommendation_id = ? AND COALESCE(superseded, 0) = 0",
                    (recommendation_id,),
                )
                total += cursor.rowcount
            await conn.commit()
        if total:
            logger.info("Superseded %d savings ledger row(s) for recommendation %d.", total, recommendation_id)
        return total

    async def compress_to_hourly(self, cutoff_hours: int = 24) -> int:
        """Aggregate raw records older than cutoff_hours into hourly buckets."""
        cutoff = to_iso_z(datetime.now(timezone.utc) - timedelta(hours=cutoff_hours))

        async with self._db.connection_scope() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO recommendation_savings_ledger_hourly
                    (recommendation_id, cluster_name, namespace,
                     recommendation_type, co2e_saved_grams,
                     cost_saved_dollars, sample_count, hour_bucket,
                     measurement_method, baseline_value, actual_value,
                     confidence, superseded, period_start, period_end)
                SELECT
                    recommendation_id,
                    cluster_name,
                    namespace,
                    recommendation_type,
                    SUM(co2e_saved_grams),
                    SUM(cost_saved_dollars),
                    COUNT(*),
                    strftime('%Y-%m-%dT%H:00:00Z', timestamp) AS hour_bucket,
                    COALESCE(measurement_method, 'prorated'),
                    MAX(baseline_value),
                    MAX(actual_value),
                    MAX(confidence),
                    MAX(COALESCE(superseded, 0)),
                    strftime('%Y-%m-%dT%H:00:00Z', timestamp),
                    strftime('%Y-%m-%dT%H:00:00Z', timestamp, '+1 hour')
                FROM recommendation_savings_ledger
                WHERE timestamp < ?
                GROUP BY recommendation_id, cluster_name, namespace,
                         recommendation_type,
                         COALESCE(measurement_method, 'prorated'),
                         strftime('%Y-%m-%dT%H:00:00Z', timestamp)
                ON CONFLICT (recommendation_id, hour_bucket, measurement_method)
                DO UPDATE SET
                    co2e_saved_grams = excluded.co2e_saved_grams,
                    cost_saved_dollars = excluded.cost_saved_dollars,
                    sample_count = excluded.sample_count,
                    superseded = excluded.superseded,
                    period_start = excluded.period_start,
                    period_end = excluded.period_end
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
        cutoff = to_iso_z(datetime.now(timezone.utc) - timedelta(days=retention_days))
        async with self._db.connection_scope() as conn:
            cursor = await conn.execute(
                "DELETE FROM recommendation_savings_ledger WHERE timestamp < ?",
                (cutoff,),
            )
            count = cursor.rowcount
            await conn.commit()
        return count
