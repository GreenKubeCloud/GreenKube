# src/greenkube/storage/node_repository.py

"""
Repository for managing node data in the database.
Uses SCD Type 2 pattern — only inserts a new record when a node's
configuration actually changes, avoiding duplicate snapshots.
"""

import logging
import sqlite3
from datetime import datetime, timezone
from typing import List

import aiosqlite

from greenkube.core.exceptions import QueryError
from greenkube.models.node import NodeInfo
from greenkube.storage.base_repository import NodeRepository
from greenkube.utils.date_utils import parse_iso_date

logger = logging.getLogger(__name__)

# Columns that define a node's "identity" for SCD comparison.
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


class SQLiteNodeRepository(NodeRepository):
    """
    SQLite implementation of NodeRepository with SCD Type 2.
    """

    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def save_nodes(self, nodes: List[NodeInfo]) -> int:
        """Save nodes using SCD Type 2 — only create a new record on change."""
        if not nodes:
            return 0

        new_records = 0
        now_str = datetime.now(timezone.utc).isoformat()

        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                for node in nodes:
                    ts = node.timestamp.isoformat() if node.timestamp else now_str

                    # Fetch current active SCD record
                    async with conn.execute(
                        """
                        SELECT id, instance_type, cpu_capacity_cores, architecture,
                               cloud_provider, region, zone, node_pool,
                               memory_capacity_bytes, embodied_emissions_kg
                        FROM node_snapshots_scd
                        WHERE node_name = ? AND is_current = 1
                        """,
                        (node.name,),
                    ) as cursor:
                        current = await cursor.fetchone()

                    if current:
                        # Compare attributes to detect change
                        changed = False
                        for col in _SCD_COMPARE_COLS:
                            old_val = current[col]
                            new_val = getattr(node, col, None)
                            if old_val != new_val:
                                changed = True
                                break

                        if not changed:
                            continue

                        # Close the current record
                        await conn.execute(
                            """
                            UPDATE node_snapshots_scd
                            SET valid_to = ?, is_current = 0
                            WHERE id = ?
                            """,
                            (ts, current["id"]),
                        )

                    # Insert new current record
                    await conn.execute(
                        """
                        INSERT INTO node_snapshots_scd (
                            node_name, instance_type, cpu_capacity_cores,
                            architecture, cloud_provider, region, zone, node_pool,
                            memory_capacity_bytes, embodied_emissions_kg,
                            valid_from, valid_to, is_current
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1)
                        """,
                        (
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
                            ts,
                        ),
                    )
                    new_records += 1

                # Also write to legacy table for backward compatibility
                await self._save_to_legacy(conn, nodes, now_str)
                await conn.commit()

            if new_records:
                logger.info("SCD2: created %d new node record(s).", new_records)
            else:
                logger.debug("SCD2: no node configuration changes detected.")
            return new_records

        except sqlite3.Error as e:
            logging.error("Failed to save node snapshots (SCD2): %s", e)
            raise QueryError(f"Failed to save nodes: {e}") from e
        except Exception as e:
            logging.error("Unexpected error in save_nodes: %s", e)
            raise QueryError(f"Unexpected error in save_nodes: {e}") from e

    async def _save_to_legacy(self, conn, nodes: List[NodeInfo], fallback_ts: str) -> None:
        """Write to the legacy node_snapshots table for backward compatibility."""
        for node in nodes:
            ts = node.timestamp.isoformat() if node.timestamp else fallback_ts
            try:
                await conn.execute(
                    """
                    INSERT INTO node_snapshots
                        (timestamp, node_name, instance_type, cpu_capacity_cores, architecture,
                         cloud_provider, region, zone, node_pool, memory_capacity_bytes, embodied_emissions_kg)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(node_name, timestamp) DO NOTHING;
                    """,
                    (
                        ts,
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
                    ),
                )
            except sqlite3.Error as e:
                logging.error("Could not save legacy node snapshot for %s: %s", node.name, e)

    async def get_snapshots(self, start: datetime, end: datetime) -> List[tuple[str, NodeInfo]]:
        """Retrieves node snapshots valid within a time range using SCD2."""
        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.execute(
                    """
                    SELECT node_name, instance_type, cpu_capacity_cores,
                           architecture, cloud_provider, region, zone, node_pool,
                           memory_capacity_bytes, embodied_emissions_kg,
                           valid_from
                    FROM node_snapshots_scd
                    WHERE valid_from <= ?
                      AND (valid_to IS NULL OR valid_to >= ?)
                    ORDER BY valid_from
                    """,
                    (end.isoformat(), start.isoformat()),
                ) as cursor:
                    rows = await cursor.fetchall()

                if rows:
                    snapshots = []
                    for row in rows:
                        snapshots.append(
                            (
                                row["valid_from"],
                                NodeInfo(
                                    name=row["node_name"],
                                    instance_type=row["instance_type"],
                                    zone=row["zone"],
                                    region=row["region"],
                                    cloud_provider=row["cloud_provider"],
                                    architecture=row["architecture"],
                                    node_pool=row["node_pool"],
                                    cpu_capacity_cores=row["cpu_capacity_cores"],
                                    memory_capacity_bytes=row["memory_capacity_bytes"],
                                    timestamp=parse_iso_date(row["valid_from"]),
                                    embodied_emissions_kg=row["embodied_emissions_kg"],
                                ),
                            )
                        )
                    return snapshots

                # Fallback to legacy table during migration
                return await self._get_snapshots_legacy(conn, start, end)

        except sqlite3.Error as e:
            logging.error("Could not retrieve snapshots: %s", e)
            raise QueryError(f"Could not retrieve snapshots: {e}") from e
        except Exception as e:
            logging.error("Unexpected error in get_snapshots: %s", e)
            raise QueryError(f"Unexpected error in get_snapshots: {e}") from e

    async def _get_snapshots_legacy(self, conn, start: datetime, end: datetime) -> List[tuple[str, NodeInfo]]:
        """Fallback: read from legacy node_snapshots table."""
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT node_name, instance_type, zone, region, cloud_provider, architecture,
                   node_pool, cpu_capacity_cores, memory_capacity_bytes, timestamp, embodied_emissions_kg
            FROM node_snapshots
            WHERE timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp ASC
            """,
            (start.isoformat(), end.isoformat()),
        ) as cursor:
            rows = await cursor.fetchall()

        snapshots = []
        for row in rows:
            snapshots.append(
                (
                    row["timestamp"],
                    NodeInfo(
                        name=row["node_name"],
                        instance_type=row["instance_type"],
                        zone=row["zone"],
                        region=row["region"],
                        cloud_provider=row["cloud_provider"],
                        architecture=row["architecture"],
                        node_pool=row["node_pool"],
                        cpu_capacity_cores=row["cpu_capacity_cores"],
                        memory_capacity_bytes=row["memory_capacity_bytes"],
                        timestamp=parse_iso_date(row["timestamp"]),
                        embodied_emissions_kg=row["embodied_emissions_kg"],
                    ),
                )
            )
        return snapshots

    async def get_latest_snapshots_before(self, timestamp: datetime) -> List[NodeInfo]:
        """Retrieves the current SCD record for each node valid before the given timestamp."""
        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.execute(
                    """
                    SELECT node_name, instance_type, cpu_capacity_cores,
                           architecture, cloud_provider, region, zone, node_pool,
                           memory_capacity_bytes, embodied_emissions_kg,
                           valid_from
                    FROM node_snapshots_scd
                    WHERE valid_from <= ?
                      AND (valid_to IS NULL OR valid_to >= ?)
                    ORDER BY valid_from DESC
                    """,
                    (timestamp.isoformat(), timestamp.isoformat()),
                ) as cursor:
                    rows = await cursor.fetchall()

                if rows:
                    # Deduplicate: take only one record per node_name
                    seen = set()
                    results = []
                    for row in rows:
                        if row["node_name"] not in seen:
                            seen.add(row["node_name"])
                            results.append(
                                NodeInfo(
                                    name=row["node_name"],
                                    instance_type=row["instance_type"],
                                    zone=row["zone"],
                                    region=row["region"],
                                    cloud_provider=row["cloud_provider"],
                                    architecture=row["architecture"],
                                    node_pool=row["node_pool"],
                                    cpu_capacity_cores=row["cpu_capacity_cores"],
                                    memory_capacity_bytes=row["memory_capacity_bytes"],
                                    timestamp=parse_iso_date(row["valid_from"]),
                                    embodied_emissions_kg=row["embodied_emissions_kg"],
                                )
                            )
                    return results

                # Fallback to legacy table
                return await self._get_latest_snapshots_legacy(conn, timestamp)

        except sqlite3.Error as e:
            logging.error("Could not retrieve latest snapshots: %s", e)
            raise QueryError(f"Could not retrieve latest snapshots: {e}") from e
        except Exception as e:
            logging.error("Unexpected error in get_latest_snapshots_before: %s", e)
            raise QueryError(f"Unexpected error in get_latest_snapshots_before: {e}") from e

    async def _get_latest_snapshots_legacy(self, conn, timestamp: datetime) -> List[NodeInfo]:
        """Fallback: read from legacy node_snapshots table."""
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT ns.node_name, ns.instance_type, ns.zone, ns.region, ns.cloud_provider,
                   ns.architecture, ns.node_pool, ns.cpu_capacity_cores, ns.memory_capacity_bytes, ns.timestamp,
                   ns.embodied_emissions_kg
            FROM node_snapshots ns
            INNER JOIN (
                SELECT node_name, MAX(timestamp) as max_ts
                FROM node_snapshots
                WHERE timestamp <= ?
                GROUP BY node_name
            ) latest ON ns.node_name = latest.node_name AND ns.timestamp = latest.max_ts
            """,
            (timestamp.isoformat(),),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            NodeInfo(
                name=row["node_name"],
                instance_type=row["instance_type"],
                zone=row["zone"],
                region=row["region"],
                cloud_provider=row["cloud_provider"],
                architecture=row["architecture"],
                node_pool=row["node_pool"],
                cpu_capacity_cores=row["cpu_capacity_cores"],
                memory_capacity_bytes=row["memory_capacity_bytes"],
                timestamp=parse_iso_date(row["timestamp"]),
                embodied_emissions_kg=row["embodied_emissions_kg"],
            )
            for row in rows
        ]
