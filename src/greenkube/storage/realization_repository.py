# src/greenkube/storage/realization_repository.py
"""The last resolved realization context, kept across restarts (IMP-12).

One row (``id = 1``) of ``realization_context`` (migration 0141) holds the
JSON form of :class:`greenkube.impact.realization.RealizationContext`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Tuple

from greenkube.storage.change_repository import ChangeRepository, _dumps, _loads, _parse_time


class RealizationContextRepository(ChangeRepository):
    """Reads and replaces the stored realization context (SQL helpers shared with the change repository)."""

    async def save_context(self, observed_at: datetime, payload: dict[str, Any]) -> None:
        """Replace the stored context."""
        async with self.db_manager.transaction() as conn:
            await self._execute(
                conn,
                "INSERT INTO realization_context (id, observed_at, payload) VALUES (1, ?, ?j) "
                "ON CONFLICT (id) DO UPDATE SET observed_at = excluded.observed_at, payload = excluded.payload",
                [self._ts(observed_at), _dumps(payload)],
            )

    async def load_context(self) -> Optional[Tuple[datetime, dict[str, Any]]]:
        """The stored context and when it was observed, or ``None``."""
        rows = await self._select("SELECT observed_at, payload FROM realization_context WHERE id = 1")
        if not rows:
            return None
        observed = _parse_time(rows[0].get("observed_at"))
        payload = _loads(rows[0].get("payload"))
        if observed is None or not isinstance(payload, dict):
            return None
        return observed, payload


def get_realization_context_repository(db_manager=None) -> RealizationContextRepository:
    """Return the repository bound to ``db_manager`` (the shared manager by default)."""
    if db_manager is None:
        from greenkube.core.db import get_db_manager

        db_manager = get_db_manager()
    return RealizationContextRepository(db_manager)
