# src/greenkube/storage/postgres/timeseries_cache_repository.py
"""PostgreSQL implementation of TimeseriesCacheRepository."""

import logging
from datetime import datetime
from typing import List, Optional

from greenkube.models.metrics import TimeseriesCachePoint

from ...core.exceptions import QueryError
from ..base_repository import TimeseriesCacheRepository

logger = logging.getLogger(__name__)


class PostgresTimeseriesCacheRepository(TimeseriesCacheRepository):
    """Persists and retrieves pre-computed timeseries chart buckets in PostgreSQL."""

    def __init__(self, db_manager) -> None:
        self.db_manager = db_manager

    async def upsert_points(self, points: List[TimeseriesCachePoint]) -> None:
        """Replace all cached points for the (window_slug, namespace) pair."""
        if not points:
            return

        window_slug = points[0].window_slug
        namespace = points[0].namespace

        try:
            async with self.db_manager.connection_scope() as conn:
                # Delete stale rows for this window+namespace
                if namespace is None:
                    await conn.execute(
                        "DELETE FROM metrics_timeseries_cache WHERE window_slug = $1 AND namespace IS NULL",
                        window_slug,
                    )
                else:
                    await conn.execute(
                        "DELETE FROM metrics_timeseries_cache WHERE window_slug = $1 AND namespace = $2",
                        window_slug,
                        namespace,
                    )

                # Bulk-insert new points
                await conn.executemany(
                    """
                    INSERT INTO metrics_timeseries_cache
                        (window_slug, namespace, bucket_ts,
                         co2e_grams, embodied_co2e_grams, total_cost, joules)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT(window_slug, namespace, bucket_ts) DO UPDATE SET
                        co2e_grams          = EXCLUDED.co2e_grams,
                        embodied_co2e_grams = EXCLUDED.embodied_co2e_grams,
                        total_cost          = EXCLUDED.total_cost,
                        joules              = EXCLUDED.joules
                    """,
                    [
                        (
                            p.window_slug,
                            p.namespace,
                            # Parse ISO string to datetime for asyncpg
                            datetime.fromisoformat(p.bucket_ts.replace("Z", "+00:00")),
                            p.co2e_grams,
                            p.embodied_co2e_grams,
                            p.total_cost,
                            p.joules,
                        )
                        for p in points
                    ],
                )
        except Exception as e:
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
                if namespace is None:
                    rows = await conn.fetch(
                        """
                        SELECT window_slug, namespace, bucket_ts,
                               co2e_grams, embodied_co2e_grams, total_cost, joules
                        FROM metrics_timeseries_cache
                        WHERE window_slug = $1 AND namespace IS NULL
                        ORDER BY bucket_ts
                        """,
                        window_slug,
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT window_slug, namespace, bucket_ts,
                               co2e_grams, embodied_co2e_grams, total_cost, joules
                        FROM metrics_timeseries_cache
                        WHERE window_slug = $1 AND namespace = $2
                        ORDER BY bucket_ts
                        """,
                        window_slug,
                        namespace,
                    )
                return [
                    TimeseriesCachePoint(
                        window_slug=row["window_slug"],
                        namespace=row["namespace"],
                        # asyncpg returns datetime objects for TIMESTAMPTZ; format to ISO-Z string
                        bucket_ts=row["bucket_ts"].strftime("%Y-%m-%dT%H:%M:%SZ")
                        if isinstance(row["bucket_ts"], datetime)
                        else str(row["bucket_ts"]),
                        co2e_grams=row["co2e_grams"],
                        embodied_co2e_grams=row["embodied_co2e_grams"],
                        total_cost=row["total_cost"],
                        joules=row["joules"],
                    )
                    for row in rows
                ]
        except Exception as e:
            logger.error("get_points failed for slug='%s': %s", window_slug, e)
            raise QueryError(f"get_points failed: {e}") from e
