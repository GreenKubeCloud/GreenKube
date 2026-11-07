# src/greenkube/storage/measurement_repository.py
"""Insert-only storage for decided savings measurements (C0.13).

The ``savings_measurements`` table is immutable: the migration installs
triggers that reject UPDATE and DELETE on both backends, and this repository
only exposes insert and read operations. A second insert for the same
``(recommendation_id, sequence)`` returns the measurement already decided for
that cycle, so a verification that crashed after deciding is replayed with its
original decision instead of being re-decided.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any, List, Optional

from greenkube.core.exceptions import QueryError
from greenkube.models.measurement import SavingsMeasurement
from greenkube.utils.date_utils import ensure_utc, to_iso_z

logger = logging.getLogger(__name__)

_COLUMNS = (
    "measurement_id",
    "recommendation_id",
    "group_id",
    "group_recommendation_ids",
    "plan_id",
    "change_id",
    "sequence",
    "strategy_key",
    "strategy_version",
    "impact_model_version",
    "baseline_snapshot_id",
    "scope",
    "windows",
    "estimates",
    "health",
    "confounders",
    "ledger_input",
    "outcome",
    "reasons",
    "evidence_sha256",
    "supersedes_measurement_id",
    "decided_at",
    "created_by",
)
_JSON_COLUMNS = frozenset(
    {"group_recommendation_ids", "windows", "estimates", "health", "confounders", "ledger_input", "reasons"}
)


class SavingsMeasurementRepository:
    """Reads and appends immutable savings measurements on SQLite or PostgreSQL."""

    def __init__(self, db_manager):
        self.db_manager = db_manager

    @property
    def _postgres(self) -> bool:
        return getattr(self.db_manager, "db_type", "sqlite") == "postgres"

    async def insert(self, measurement: SavingsMeasurement) -> SavingsMeasurement:
        """Persist a measurement, or return the one already decided for its cycle."""
        row = self._row(measurement)
        columns = ", ".join(_COLUMNS)
        try:
            async with self.db_manager.connection_scope() as conn:
                if self._postgres:
                    placeholders = ", ".join(
                        f"${index}::jsonb" if column in _JSON_COLUMNS else f"${index}"
                        for index, column in enumerate(_COLUMNS, start=1)
                    )
                    inserted = await conn.fetchrow(
                        f"INSERT INTO savings_measurements ({columns}) VALUES ({placeholders}) "
                        "ON CONFLICT (recommendation_id, sequence) DO NOTHING RETURNING measurement_id",
                        *[row[column] for column in _COLUMNS],
                    )
                    if inserted is not None:
                        return measurement
                else:
                    placeholders = ", ".join("?" for _ in _COLUMNS)
                    try:
                        cursor = await conn.execute(
                            f"INSERT INTO savings_measurements ({columns}) VALUES ({placeholders}) "
                            "ON CONFLICT (recommendation_id, sequence) DO NOTHING",
                            [row[column] for column in _COLUMNS],
                        )
                        inserted_count = cursor.rowcount
                        await conn.commit()
                    except BaseException:
                        await conn.rollback()
                        raise
                    if inserted_count:
                        return measurement
        except (sqlite3.Error, OSError) as exc:
            raise QueryError(f"Could not persist savings measurement: {exc}") from exc

        existing = await self.get_for_cycle(measurement.recommendation_id, measurement.sequence)
        if existing is None:
            raise QueryError("Savings measurement conflict without an existing row.")
        logger.info(
            "Measurement %s of recommendation %s was already decided; reusing it.",
            existing.measurement_id,
            existing.recommendation_id,
        )
        return existing

    async def get(self, measurement_id: str) -> Optional[SavingsMeasurement]:
        rows = await self._select("measurement_id = {}", [measurement_id])
        return rows[0] if rows else None

    async def get_for_cycle(self, recommendation_id: int, sequence: int) -> Optional[SavingsMeasurement]:
        rows = await self._select("recommendation_id = {} AND sequence = {}", [recommendation_id, sequence])
        return rows[0] if rows else None

    async def list_for_recommendation(self, recommendation_id: int) -> List[SavingsMeasurement]:
        """Return every measurement of a recommendation, oldest first."""
        return await self._select("recommendation_id = {}", [recommendation_id])

    async def _select(self, where: str, params: list[Any]) -> List[SavingsMeasurement]:
        if self._postgres:
            clause = where.format(*[f"${index}" for index in range(1, len(params) + 1)])
        else:
            clause = where.format(*["?" for _ in params])
        query = f"SELECT * FROM savings_measurements WHERE {clause} ORDER BY sequence ASC, decided_at ASC"
        async with self.db_manager.connection_scope() as conn:
            if self._postgres:
                rows = await conn.fetch(query, *params)
            else:
                cursor = await conn.execute(query, params)
                rows = await cursor.fetchall()
        return [self._from_row(dict(row)) for row in rows]

    def _row(self, measurement: SavingsMeasurement) -> dict[str, Any]:
        data = measurement.model_dump(mode="json")
        row: dict[str, Any] = {}
        for column in _COLUMNS:
            value = data.get(column)
            if column in _JSON_COLUMNS:
                row[column] = json.dumps(value if value is not None else ([] if column != "windows" else {}))
            elif column == "decided_at":
                decided_at = ensure_utc(measurement.decided_at)
                row[column] = decided_at if self._postgres else to_iso_z(decided_at)
            else:
                row[column] = value
        return row

    @staticmethod
    def _from_row(row: dict[str, Any]) -> SavingsMeasurement:
        for column in _JSON_COLUMNS:
            value = row.get(column)
            if isinstance(value, (str, bytes)):
                row[column] = json.loads(value)
        decided_at = row.get("decided_at")
        if isinstance(decided_at, str):
            row["decided_at"] = datetime.fromisoformat(decided_at.replace("Z", "+00:00"))
        return SavingsMeasurement.model_validate(row)


def get_savings_measurement_repository(db_manager=None) -> SavingsMeasurementRepository:
    """Return a measurement repository bound to ``db_manager`` (the shared manager by default)."""
    if db_manager is None:
        from greenkube.core.db import get_db_manager

        db_manager = get_db_manager()
    return SavingsMeasurementRepository(db_manager)
