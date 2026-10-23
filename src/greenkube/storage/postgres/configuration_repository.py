"""PostgreSQL persistence for runtime configuration overrides."""

import json

from ..base_configuration_repository import BaseConfigurationRepository


class PostgresConfigurationRepository(BaseConfigurationRepository):
    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def load(self) -> tuple[int, dict[str, str]]:
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow("SELECT version, values_json FROM configuration_overrides WHERE id = 1")
        if row is None:
            return 0, {}
        values = row["values_json"]
        if isinstance(values, str):
            values = json.loads(values)
        return int(row["version"]), values

    async def save(self, values: dict[str, str], version: int) -> None:
        async with self.db_manager.connection_scope() as conn:
            await conn.execute(
                """
                INSERT INTO configuration_overrides (id, version, values_json)
                VALUES (1, $1, $2::jsonb)
                ON CONFLICT (id) DO UPDATE SET
                    version = EXCLUDED.version,
                    values_json = EXCLUDED.values_json,
                    updated_at = NOW()
                """,
                version,
                json.dumps(values, sort_keys=True),
            )

    async def clear(self, version: int) -> None:
        await self.save({}, version)
