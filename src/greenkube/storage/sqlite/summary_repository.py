# src/greenkube/storage/sqlite/summary_repository.py
"""SQLite implementation of SummaryRepository."""

import logging
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

import aiosqlite

from greenkube.models.metrics import MetricsSummaryRow

from ...core.exceptions import QueryError
from ..base_repository import SummaryRepository

logger = logging.getLogger(__name__)


class SQLiteSummaryRepository(SummaryRepository):
    """Persists and retrieves pre-computed dashboard summary rows in SQLite."""

    def __init__(self, db_manager) -> None:
        self.db_manager = db_manager

    async def upsert_row(self, row: MetricsSummaryRow) -> None:
        """Insert or update a summary row identified by (window_slug, namespace)."""
        updated_at = (row.updated_at or datetime.now(timezone.utc)).isoformat()
        try:
            async with self.db_manager.connection_scope() as conn:
                if row.namespace is None:
                    await conn.execute(
                        "DELETE FROM metrics_summary WHERE window_slug = ? AND namespace IS NULL",
                        (row.window_slug,),
                    )
                    await conn.execute(
                        """
                        INSERT INTO metrics_summary
                            (window_slug, namespace,
                             total_co2e_grams, total_embodied_co2e_grams,
                             total_cost, total_energy_joules,
                             pod_count, namespace_count, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row.window_slug,
                            None,
                            row.total_co2e_grams,
                            row.total_embodied_co2e_grams,
                            row.total_cost,
                            row.total_energy_joules,
                            row.pod_count,
                            row.namespace_count,
                            updated_at,
                        ),
                    )
                else:
                    await conn.execute(
                        """
                        INSERT INTO metrics_summary
                            (window_slug, namespace,
                             total_co2e_grams, total_embodied_co2e_grams,
                             total_cost, total_energy_joules,
                             pod_count, namespace_count, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(window_slug, namespace) DO UPDATE SET
                            total_co2e_grams          = excluded.total_co2e_grams,
                            total_embodied_co2e_grams = excluded.total_embodied_co2e_grams,
                            total_cost                = excluded.total_cost,
                            total_energy_joules       = excluded.total_energy_joules,
                            pod_count                 = excluded.pod_count,
                            namespace_count           = excluded.namespace_count,
                            updated_at                = excluded.updated_at
                        """,
                        (
                            row.window_slug,
                            row.namespace,
                            row.total_co2e_grams,
                            row.total_embodied_co2e_grams,
                            row.total_cost,
                            row.total_energy_joules,
                            row.pod_count,
                            row.namespace_count,
                            updated_at,
                        ),
                    )
                await conn.commit()
        except sqlite3.Error as e:
            logger.error("upsert_row failed for slug '%s': %s", row.window_slug, e)
            raise QueryError(f"upsert_row failed: {e}") from e

    async def get_rows(self, namespace: Optional[str] = None) -> List[MetricsSummaryRow]:
        """Return all summary rows matching the given namespace filter.

        When *namespace* is ``None``, only cluster-wide rows (stored namespace
        IS NULL) are returned.
        """
        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                if namespace is None:
                    query = """
                        SELECT * FROM metrics_summary
                        WHERE namespace IS NULL
                        ORDER BY window_slug
                    """
                    params: tuple = ()
                else:
                    query = """
                        SELECT * FROM metrics_summary
                        WHERE namespace = ?
                        ORDER BY window_slug
                    """
                    params = (namespace,)

                async with conn.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
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
                            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
                        )
                        for row in rows
                    ]
        except sqlite3.Error as e:
            logger.error("get_rows failed: %s", e)
            raise QueryError(f"get_rows failed: {e}") from e
