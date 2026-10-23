"""SQLite persistence for runtime configuration overrides."""

import json

from ..base_configuration_repository import BaseConfigurationRepository


class SQLiteConfigurationRepository(BaseConfigurationRepository):
    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def load(self) -> tuple[int, dict[str, str]]:
        async with self.db_manager.connection_scope() as conn:
            async with conn.execute("SELECT version, values_json FROM configuration_overrides WHERE id = 1") as cursor:
                row = await cursor.fetchone()
        if row is None:
            return 0, {}
        values = json.loads(row["values_json"] if hasattr(row, "keys") else row[1])
        return int(row["version"] if hasattr(row, "keys") else row[0]), values

    async def save(self, values: dict[str, str], version: int) -> None:
        async with self.db_manager.connection_scope() as conn:
            await conn.execute(
                """
                INSERT INTO configuration_overrides (id, version, values_json)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    version = excluded.version,
                    values_json = excluded.values_json,
                    updated_at = datetime('now')
                """,
                (version, json.dumps(values, sort_keys=True)),
            )
            await conn.commit()

    async def clear(self, version: int) -> None:
        await self.save({}, version)
