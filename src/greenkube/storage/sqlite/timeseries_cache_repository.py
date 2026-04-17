# src/greenkube/storage/sqlite/timeseries_cache_repository.py
"""SQLite implementation of TimeseriesCacheRepository."""

import logging
import sqlite3
from typing import List, Optional

import aiosqlite

from greenkube.models.metrics import TimeseriesCachePoint

from ...core.exceptions import QueryError
from ..base_repository import TimeseriesCacheRepository

logger = logging.getLogger(__name__)


class SQLiteTimeseriesCacheRepository(TimeseriesCacheRepository):
    """Persists and retrieves pre-computed timeseries chart buckets in SQLite."""

    def __init__(self, db_manager) -> None:
        self.db_manager = db_manager

    async def upsert_points(self, points: List[TimeseriesCachePoint]) -> None:
        """Replace all cached points for the (window_slug, namespace) pair.

        Deletes existing rows for that pair then bulk-inserts the new ones
        inside a single transaction.
        """
        if not points:
            return

        window_slug = points[0].window_slug
        namespace = points[0].namespace

        try:
            async with self.db_manager.connection_scope() as conn:
                # Delete stale data for this window+namespace combination
                if namespace is None:
                    await conn.execute(
                        "DELETE FROM metrics_timeseries_cache WHERE window_slug = ? AND namespace IS NULL",
                        (window_slug,),
                    )
                else:
                    await conn.execute(
                        "DELETE FROM metrics_timeseries_cache WHERE window_slug = ? AND namespace = ?",
                        (window_slug, namespace),
                    )

                # Bulk-insert new points
                await conn.executemany(
                    """
                    INSERT INTO metrics_timeseries_cache
                        (window_slug, namespace, bucket_ts,
                         co2e_grams, embodied_co2e_grams, total_cost, joules)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(window_slug, namespace, bucket_ts) DO UPDATE SET
                        co2e_grams          = excluded.co2e_grams,
                        embodied_co2e_grams = excluded.embodied_co2e_grams,
                        total_cost          = excluded.total_cost,
                        joules              = excluded.joules
                    """,
                    [
                        (
                            p.window_slug,
                            p.namespace,
                            p.bucket_ts,
                            p.co2e_grams,
                            p.embodied_co2e_grams,
                            p.total_cost,
                            p.joules,
                        )
                        for p in points
                    ],
                )
                await conn.commit()
        except sqlite3.Error as e:
            logger.error("upsert_points failed for slug='%s': %s", window_slug, e)
            raise QueryError(f"upsert_points failed: {e}") from e

    async def get_points(
        self,
        window_slug: str,
        namespace: Optional[str] = None,
    ) -> List[TimeseriesCachePoint]:
        """Return ordered cached buckets for the given window and namespace."""
        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                if namespace is None:
                    query = """
                        SELECT window_slug, namespace, bucket_ts,
                               co2e_grams, embodied_co2e_grams, total_cost, joules
                        FROM metrics_timeseries_cache
                        WHERE window_slug = ? AND namespace IS NULL
                        ORDER BY bucket_ts
                    """
                    params: tuple = (window_slug,)
                else:
                    query = """
                        SELECT window_slug, namespace, bucket_ts,
                               co2e_grams, embodied_co2e_grams, total_cost, joules
                        FROM metrics_timeseries_cache
                        WHERE window_slug = ? AND namespace = ?
                        ORDER BY bucket_ts
                    """
                    params = (window_slug, namespace)

                async with conn.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    return [
                        TimeseriesCachePoint(
                            window_slug=row["window_slug"],
                            namespace=row["namespace"],
                            bucket_ts=row["bucket_ts"],
                            co2e_grams=row["co2e_grams"],
                            embodied_co2e_grams=row["embodied_co2e_grams"],
                            total_cost=row["total_cost"],
                            joules=row["joules"],
                        )
                        for row in rows
                    ]
        except sqlite3.Error as e:
            logger.error("get_points failed for slug='%s': %s", window_slug, e)
            raise QueryError(f"get_points failed: {e}") from e
