# src/greenkube/storage/postgres/summary_repository.py
"""PostgreSQL implementation of SummaryRepository."""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from greenkube.models.metrics import MetricsSummaryRow

from ...core.exceptions import QueryError
from ..base_repository import SummaryRepository

logger = logging.getLogger(__name__)


class PostgresSummaryRepository(SummaryRepository):
    """Persists and retrieves pre-computed dashboard summary rows in PostgreSQL."""

    def __init__(self, db_manager) -> None:
        self.db_manager = db_manager

    async def upsert_row(self, row: MetricsSummaryRow) -> None:
        """Insert or update a summary row identified by (window_slug, namespace).

        PostgreSQL treats NULL as not-equal-to-NULL in UNIQUE constraints, so
        ``ON CONFLICT(window_slug, namespace)`` never fires for cluster-wide
        rows (namespace IS NULL).  We work around this with an explicit
        DELETE-then-INSERT wrapped in a single round-trip.
        """
        updated_at = row.updated_at or datetime.now(timezone.utc)
        try:
            async with self.db_manager.connection_scope() as conn:
                if row.namespace is None:
                    # Cluster-wide row: delete all existing rows for this slug
                    # then insert the fresh one.
                    await conn.execute(
                        "DELETE FROM metrics_summary WHERE window_slug = $1 AND namespace IS NULL",
                        row.window_slug,
                    )
                    await conn.execute(
                        """
                        INSERT INTO metrics_summary
                            (window_slug, namespace,
                             total_co2e_grams, total_embodied_co2e_grams,
                             total_cost, total_energy_joules,
                             pod_count, namespace_count, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        """,
                        row.window_slug,
                        None,
                        row.total_co2e_grams,
                        row.total_embodied_co2e_grams,
                        row.total_cost,
                        row.total_energy_joules,
                        row.pod_count,
                        row.namespace_count,
                        updated_at,
                    )
                else:
                    await conn.execute(
                        """
                        INSERT INTO metrics_summary
                            (window_slug, namespace,
                             total_co2e_grams, total_embodied_co2e_grams,
                             total_cost, total_energy_joules,
                             pod_count, namespace_count, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        ON CONFLICT(window_slug, namespace) DO UPDATE SET
                            total_co2e_grams          = EXCLUDED.total_co2e_grams,
                            total_embodied_co2e_grams = EXCLUDED.total_embodied_co2e_grams,
                            total_cost                = EXCLUDED.total_cost,
                            total_energy_joules       = EXCLUDED.total_energy_joules,
                            pod_count                 = EXCLUDED.pod_count,
                            namespace_count           = EXCLUDED.namespace_count,
                            updated_at                = EXCLUDED.updated_at
                        """,
                        row.window_slug,
                        row.namespace,
                        row.total_co2e_grams,
                        row.total_embodied_co2e_grams,
                        row.total_cost,
                        row.total_energy_joules,
                        row.pod_count,
                        row.namespace_count,
                        updated_at,
                    )
        except Exception as e:
            logger.error("upsert_row failed for slug '%s': %s", row.window_slug, e)
            raise QueryError(f"upsert_row failed: {e}") from e

    async def get_rows(self, namespace: Optional[str] = None) -> List[MetricsSummaryRow]:
        """Return the latest summary row per window_slug for the given namespace.

        When *namespace* is ``None``, only cluster-wide rows (namespace IS NULL)
        are returned — one per slug, selecting the most recently updated.
        """
        try:
            async with self.db_manager.connection_scope() as conn:
                if namespace is None:
                    rows = await conn.fetch(
                        """
                        SELECT DISTINCT ON (window_slug) *
                        FROM metrics_summary
                        WHERE namespace IS NULL
                        ORDER BY window_slug, updated_at DESC
                        """
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT DISTINCT ON (window_slug) *
                        FROM metrics_summary
                        WHERE namespace = $1
                        ORDER BY window_slug, updated_at DESC
                        """,
                        namespace,
                    )
                return [
                    MetricsSummaryRow(
                        window_slug=row["window_slug"],
                        namespace=row["namespace"],
                        total_co2e_grams=row["total_co2e_grams"],
                        total_embodied_co2e_grams=row["total_embodied_co2e_grams"],
                        total_co2e_all_scopes=row["total_co2e_grams"] + row["total_embodied_co2e_grams"],
                        total_cost=row["total_cost"],
                        total_energy_joules=row["total_energy_joules"],
                        pod_count=row["pod_count"],
                        namespace_count=row["namespace_count"],
                        updated_at=row["updated_at"],
                    )
                    for row in rows
                ]
        except Exception as e:
            logger.error("get_rows failed: %s", e)
            raise QueryError(f"get_rows failed: {e}") from e
