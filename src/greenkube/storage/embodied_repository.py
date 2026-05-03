import logging
from datetime import datetime, timezone
from typing import Optional

from greenkube.core.db import DatabaseManager
from greenkube.core.exceptions import QueryError
from greenkube.utils.date_utils import to_iso_z

from .base_embodied_repository import BaseEmbodiedRepository

# Conditional imports for Elasticsearch
try:
    from elasticsearch_dsl import Date, Document, Float, Integer, Keyword
except ImportError:
    pass

logger = logging.getLogger(__name__)

# Define ES Document for Embodied Profile
if "Document" in globals():

    class InstanceCarbonProfileDoc(Document):
        """
        Elasticsearch Document representing an instance carbon profile.
        """

        provider = Keyword(required=True)
        instance_type = Keyword(required=True)
        gwp_manufacture = Float(required=True)
        lifespan_hours = Integer(required=True)
        source = Keyword()
        last_updated = Date()

        class Index:
            name = "greenkube_instance_carbon_profiles"
            settings = {"number_of_shards": 1, "number_of_replicas": 0}
else:
    # Dummy class to avoid NameError if elasticsearch-dsl is not installed
    class InstanceCarbonProfileDoc:
        class Index:
            name = "greenkube_instance_carbon_profiles"

        @classmethod
        async def init(cls):
            pass


# ---------------------------------------------------------------------------
# SQLite implementation
# ---------------------------------------------------------------------------


class SQLiteEmbodiedRepository(BaseEmbodiedRepository):
    """Embodied carbon profile repository backed by SQLite."""

    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager

    async def get_profile(self, provider: str, instance_type: str) -> Optional[dict]:
        query = """
            SELECT gwp_manufacture, lifespan_hours, source, last_updated
            FROM instance_carbon_profiles
            WHERE provider = ? AND instance_type = ?
        """
        try:
            async with self.db_manager.connection_scope() as conn:
                async with conn.execute(query, (provider, instance_type)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return {
                            "gwp_manufacture": row["gwp_manufacture"],
                            "lifespan_hours": row["lifespan_hours"],
                            "source": row["source"],
                            "last_updated": row["last_updated"],
                        }
                    return None
        except Exception as e:
            logger.error("Error fetching embodied profile for %s/%s: %s", provider, instance_type, e)
            raise QueryError(f"Database error in get_profile: {e}") from e

    async def save_profile(
        self, provider: str, instance_type: str, gwp: float, lifespan: int, source: str = "boavizta_api"
    ):
        now_iso = to_iso_z(datetime.now(timezone.utc))
        query = """
            INSERT INTO instance_carbon_profiles (
                provider, instance_type, gwp_manufacture, lifespan_hours, source, last_updated
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, instance_type)
            DO UPDATE SET
                gwp_manufacture = excluded.gwp_manufacture,
                lifespan_hours = excluded.lifespan_hours,
                source = excluded.source,
                last_updated = excluded.last_updated
        """
        try:
            async with self.db_manager.connection_scope() as conn:
                await conn.execute(query, (provider, instance_type, gwp, lifespan, source, now_iso))
                await conn.commit()
        except Exception as e:
            logger.error("Error saving embodied profile for %s/%s: %s", provider, instance_type, e)
            raise QueryError(f"Database error in save_profile: {e}") from e


# ---------------------------------------------------------------------------
# PostgreSQL implementation
# ---------------------------------------------------------------------------


class PostgresEmbodiedRepository(BaseEmbodiedRepository):
    """Embodied carbon profile repository backed by PostgreSQL."""

    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager

    async def get_profile(self, provider: str, instance_type: str) -> Optional[dict]:
        query = """
            SELECT gwp_manufacture, lifespan_hours, source, last_updated
            FROM instance_carbon_profiles
            WHERE provider = $1 AND instance_type = $2
        """
        try:
            async with self.db_manager.connection_scope() as conn:
                row = await conn.fetchrow(query, provider, instance_type)
                if row:
                    return {
                        "gwp_manufacture": row["gwp_manufacture"],
                        "lifespan_hours": row["lifespan_hours"],
                        "source": row["source"],
                        "last_updated": row["last_updated"],
                    }
                return None
        except Exception as e:
            logger.error("Error fetching embodied profile for %s/%s: %s", provider, instance_type, e)
            raise QueryError(f"Database error in get_profile: {e}") from e

    async def save_profile(
        self, provider: str, instance_type: str, gwp: float, lifespan: int, source: str = "boavizta_api"
    ):
        now = datetime.now(timezone.utc)
        query = """
            INSERT INTO instance_carbon_profiles (
                provider, instance_type, gwp_manufacture, lifespan_hours, source, last_updated
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (provider, instance_type)
            DO UPDATE SET
                gwp_manufacture = EXCLUDED.gwp_manufacture,
                lifespan_hours = EXCLUDED.lifespan_hours,
                source = EXCLUDED.source,
                last_updated = EXCLUDED.last_updated
        """
        try:
            async with self.db_manager.connection_scope() as conn:
                await conn.execute(query, provider, instance_type, gwp, lifespan, source, now)
        except Exception as e:
            logger.error("Error saving embodied profile for %s/%s: %s", provider, instance_type, e)
            raise QueryError(f"Database error in save_profile: {e}") from e


# ---------------------------------------------------------------------------
# Elasticsearch implementation
# ---------------------------------------------------------------------------


class ElasticsearchEmbodiedRepository(BaseEmbodiedRepository):
    """Embodied carbon profile repository backed by Elasticsearch."""

    async def get_profile(self, provider: str, instance_type: str) -> Optional[dict]:
        try:
            doc_id = f"{provider}-{instance_type}"
            doc = await InstanceCarbonProfileDoc.get(id=doc_id, ignore=404)
            if doc:
                return {
                    "gwp_manufacture": doc.gwp_manufacture,
                    "lifespan_hours": doc.lifespan_hours,
                    "source": doc.source,
                    "last_updated": doc.last_updated,
                }
            return None
        except Exception as e:
            logger.error("Error fetching embodied profile from ES for %s/%s: %s", provider, instance_type, e)
            return None

    async def save_profile(
        self, provider: str, instance_type: str, gwp: float, lifespan: int, source: str = "boavizta_api"
    ):
        now_iso = to_iso_z(datetime.now(timezone.utc))
        try:
            doc_id = f"{provider}-{instance_type}"
            doc = InstanceCarbonProfileDoc(
                meta={"id": doc_id},
                provider=provider,
                instance_type=instance_type,
                gwp_manufacture=gwp,
                lifespan_hours=lifespan,
                source=source,
                last_updated=now_iso,
            )
            await doc.save()
            logger.info("Saved ES profile for %s", doc_id)
        except Exception as e:
            logger.error("Error saving embodied profile to ES for %s/%s: %s", provider, instance_type, e)
            raise QueryError(f"ES error in save_profile: {e}") from e


# ---------------------------------------------------------------------------
# Backward-compatible alias — delegates to the db_type at construction time
# ---------------------------------------------------------------------------


class EmbodiedRepository(BaseEmbodiedRepository):
    """Factory-style wrapper that delegates to the correct backend.

    Kept for backward compatibility; prefer using the concrete classes
    via the factory functions in ``factory.py``.
    """

    def __init__(self, db_manager: DatabaseManager):
        if db_manager.db_type == "elasticsearch":
            self._impl: BaseEmbodiedRepository = ElasticsearchEmbodiedRepository()
        elif db_manager.db_type == "postgres":
            self._impl = PostgresEmbodiedRepository(db_manager)
        else:
            self._impl = SQLiteEmbodiedRepository(db_manager)

    async def get_profile(self, provider: str, instance_type: str) -> Optional[dict]:
        return await self._impl.get_profile(provider, instance_type)

    async def save_profile(
        self, provider: str, instance_type: str, gwp: float, lifespan: int, source: str = "boavizta_api"
    ):
        await self._impl.save_profile(provider, instance_type, gwp, lifespan, source)
