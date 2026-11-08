"""Repository contract for per-source collection watermarks (roadmap A2)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class CollectionWatermark:
    """Exclusive end of the last window whose rows were fully written."""

    cluster_uid: str
    source: str
    last_complete_end: datetime
    updated_at: datetime


class CollectionWatermarkRepository(ABC):
    """Stores one monotonic watermark per ``(cluster_uid, source)``."""

    @abstractmethod
    async def get(self, cluster_uid: str, source: str) -> CollectionWatermark | None:
        """Return the watermark, or ``None`` when the source was never collected."""

    @abstractmethod
    async def advance(self, cluster_uid: str, source: str, last_complete_end: datetime) -> CollectionWatermark:
        """Move the watermark forward to ``last_complete_end``; never moves it backwards."""

    @abstractmethod
    async def list_all(self, cluster_uid: str | None = None) -> list[CollectionWatermark]:
        """Return every watermark, optionally for one cluster."""


def create_collection_watermark_repository(db_manager: Any, db_type: str) -> CollectionWatermarkRepository:
    """Build the watermark repository matching ``DB_TYPE``."""
    if db_type == "postgres":
        from .postgres.collection_watermark_repository import PostgresCollectionWatermarkRepository

        return PostgresCollectionWatermarkRepository(db_manager)
    if db_type == "sqlite":
        from .sqlite.collection_watermark_repository import SQLiteCollectionWatermarkRepository

        return SQLiteCollectionWatermarkRepository(db_manager)
    raise NotImplementedError(f"CollectionWatermarkRepository for DB_TYPE '{db_type}' not implemented.")
