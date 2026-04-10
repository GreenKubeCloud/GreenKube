# src/greenkube/storage/postgres_node_repository.py
"""PostgreSQL node repository with SCD Type 2 pattern.

Only inserts a new record when a node's configuration actually changes,
avoiding thousands of duplicate snapshots.
"""

import logging
from datetime import datetime
from typing import List

from ...core.exceptions import QueryError
from ...models.node import NodeInfo
from ..base_repository import NodeRepository

logger = logging.getLogger(__name__)

# Columns that define a node's "identity" for SCD comparison.
# If any of these change, a new SCD record is created.
_SCD_COMPARE_COLS = (
    "instance_type",
    "cpu_capacity_cores",
    "architecture",
    "cloud_provider",
    "region",
    "zone",
    "node_pool",
    "memory_capacity_bytes",
    "embodied_emissions_kg",
)


class PostgresNodeRepository(NodeRepository):
    """PostgreSQL implementation using SCD Type 2 for node snapshots."""

    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def save_nodes(self, nodes: List[NodeInfo]) -> int:
        """Save nodes using SCD Type 2 — only create a new record on change."""
        if not nodes:
            return 0

        new_records = 0
        try:
            async with self.db_manager.connection_scope() as conn:
                for node in nodes:
                    now = node.timestamp or datetime.utcnow()
                    # Fetch the current active SCD record for this node
                    current = await conn.fetchrow(
                        """
                        SELECT id, instance_type, cpu_capacity_cores, architecture,
                               cloud_provider, region, zone, node_pool,
                               memory_capacity_bytes, embodied_emissions_kg
                        FROM node_snapshots_scd
                        WHERE node_name = $1 AND is_current = TRUE
                        """,
                        node.name,
                    )

                    if current:
                        # Compare attributes to detect change
                        changed = False
                        for col in _SCD_COMPARE_COLS:
                            old_val = current[col]
                            new_val = getattr(node, col, None)
                            # Normalize None vs None comparison
                            if old_val != new_val:
                                changed = True
                                break

                        if not changed:
                            # No change — skip this node
                            continue

                        # Close the current record
                        await conn.execute(
                            """
                            UPDATE node_snapshots_scd
                            SET valid_to = $1, is_current = FALSE
                            WHERE id = $2
                            """,
                            now,
                            current["id"],
                        )

                    # Insert new current record
                    await conn.execute(
                        """
                        INSERT INTO node_snapshots_scd (
                            node_name, instance_type, cpu_capacity_cores,
                            architecture, cloud_provider, region, zone, node_pool,
                            memory_capacity_bytes, embodied_emissions_kg,
                            valid_from, valid_to, is_current
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NULL, TRUE)
                        """,
                        node.name,
                        node.instance_type,
                        node.cpu_capacity_cores,
                        node.architecture,
                        node.cloud_provider,
                        node.region,
                        node.zone,
                        node.node_pool,
                        node.memory_capacity_bytes,
                        node.embodied_emissions_kg,
                        now,
                    )
                    new_records += 1

                # Also write to legacy table for backward compatibility
                await self._save_to_legacy(conn, nodes)

            if new_records:
                logger.info("SCD2: created %d new node record(s).", new_records)
            else:
                logger.debug("SCD2: no node configuration changes detected.")
            return new_records
        except Exception as e:
            logger.error("Error saving node snapshots (SCD2): %s", e)
            raise QueryError(f"Error saving node snapshots: {e}") from e

    async def _save_to_legacy(self, conn, nodes: List[NodeInfo]) -> None:
        """Write to the legacy node_snapshots table for backward compatibility."""
        query = """
            INSERT INTO node_snapshots (
                timestamp, node_name, instance_type, cpu_capacity_cores,
                architecture, cloud_provider, region, zone, node_pool,
                memory_capacity_bytes, embodied_emissions_kg
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT (node_name, timestamp) DO NOTHING
        """
        data = [
            (
                node.timestamp,
                node.name,
                node.instance_type,
                node.cpu_capacity_cores,
                node.architecture,
                node.cloud_provider,
                node.region,
                node.zone,
                node.node_pool,
                node.memory_capacity_bytes,
                node.embodied_emissions_kg,
            )
            for node in nodes
        ]
        await conn.executemany(query, data)

    async def get_snapshots(self, start: datetime, end: datetime) -> List[tuple[str, NodeInfo]]:
        """Retrieves node snapshots valid within a time range using SCD2."""
        try:
            async with self.db_manager.connection_scope() as conn:
                query = """
                    SELECT node_name, instance_type, cpu_capacity_cores,
                           architecture, cloud_provider, region, zone, node_pool,
                           memory_capacity_bytes, embodied_emissions_kg,
                           valid_from
                    FROM node_snapshots_scd
                    WHERE valid_from <= $2
                      AND (valid_to IS NULL OR valid_to >= $1)
                    ORDER BY valid_from
                """
                results = await conn.fetch(query, start, end)

                snapshots = []
                for row in results:
                    data = dict(row)
                    ts = data.pop("valid_from")
                    data["name"] = data.pop("node_name")
                    data["timestamp"] = ts
                    snapshots.append((ts.isoformat(), NodeInfo(**data)))

                if not snapshots:
                    # Fallback to legacy table during migration
                    return await self._get_snapshots_legacy(conn, start, end)

                return snapshots
        except Exception as e:
            logger.error("Error getting snapshots from Postgres (SCD2): %s", e)
            raise QueryError(f"Error getting snapshots: {e}") from e

    async def _get_snapshots_legacy(self, conn, start: datetime, end: datetime) -> List[tuple[str, NodeInfo]]:
        """Fallback: read from legacy node_snapshots table."""
        query = """
            SELECT timestamp, node_name, instance_type, cpu_capacity_cores,
                   architecture, cloud_provider, region, zone, node_pool,
                   memory_capacity_bytes, embodied_emissions_kg
            FROM node_snapshots
            WHERE timestamp >= $1 AND timestamp <= $2
        """
        results = await conn.fetch(query, start, end)
        snapshots = []
        for row in results:
            data = dict(row)
            ts = data["timestamp"]
            data["name"] = data.pop("node_name")
            snapshots.append((ts.isoformat(), NodeInfo(**data)))
        return snapshots

    async def get_latest_snapshots_before(self, timestamp: datetime) -> List[NodeInfo]:
        """Retrieves the current active snapshot for each node."""
        try:
            async with self.db_manager.connection_scope() as conn:
                # Use SCD2 table — just fetch all current records
                query = """
                    SELECT node_name, instance_type, cpu_capacity_cores,
                           architecture, cloud_provider, region, zone, node_pool,
                           memory_capacity_bytes, embodied_emissions_kg,
                           valid_from
                    FROM node_snapshots_scd
                    WHERE is_current = TRUE AND valid_from <= $1
                """
                results = await conn.fetch(query, timestamp)

                if results:
                    nodes = []
                    for row in results:
                        data = dict(row)
                        data["name"] = data.pop("node_name")
                        data["timestamp"] = data.pop("valid_from")
                        nodes.append(NodeInfo(**data))
                    return nodes

                # Fallback to legacy table if SCD2 table is empty
                query = """
                    SELECT DISTINCT ON (node_name)
                           timestamp, node_name, instance_type, cpu_capacity_cores,
                           architecture, cloud_provider, region, zone, node_pool,
                           memory_capacity_bytes, embodied_emissions_kg
                    FROM node_snapshots
                    WHERE timestamp <= $1
                    ORDER BY node_name, timestamp DESC
                """
                results = await conn.fetch(query, timestamp)

                nodes = []
                for row in results:
                    data = dict(row)
                    data["name"] = data.pop("node_name")
                    nodes.append(NodeInfo(**data))
                return nodes
        except Exception as e:
            logger.error("Error getting latest snapshots from Postgres: %s", e)
            raise QueryError(f"Error getting latest snapshots: {e}") from e
