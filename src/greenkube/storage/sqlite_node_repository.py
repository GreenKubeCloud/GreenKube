# src/greenkube/storage/node_repository.py

"""
Repository for managing node data in the database.
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


class SQLiteNodeRepository(NodeRepository):
    """
    SQLite implementation of NodeRepository.
    """

    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def save_nodes(self, nodes: List[NodeInfo]) -> int:
        """
        Saves node snapshots to the database.
        """
        saved_count = 0
        now = datetime.now(timezone.utc).isoformat()

        try:
            async with self.db_manager.connection_scope() as conn:
                for node in nodes:
                    try:
                        # Use node.timestamp if available, otherwise use current time
                        ts = node.timestamp.isoformat() if node.timestamp else now
                        cursor = await conn.execute(
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
                        saved_count += cursor.rowcount
                    except sqlite3.Error as e:
                        logging.error(f"Could not save node snapshot for {node.name}: {e}")
                    except Exception as e:
                        logging.error(f"Unexpected error processing node {node.name}: {e}")

                await conn.commit()
                return saved_count
        except sqlite3.Error as e:
            logging.error(f"Failed to commit transaction for nodes: {e}")
            raise QueryError(f"Failed to commit nodes: {e}") from e
        except Exception as e:
            logging.error(f"Unexpected error in save_nodes: {e}")
            raise QueryError(f"Unexpected error in save_nodes: {e}") from e

    async def get_snapshots(self, start: datetime, end: datetime) -> List[tuple[str, NodeInfo]]:
        """
        Retrieves node snapshots within a time range.
        """
        try:
            async with self.db_manager.connection_scope() as conn:
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

        except sqlite3.Error as e:
            logging.error(f"Could not retrieve snapshots: {e}")
            raise QueryError(f"Could not retrieve snapshots: {e}") from e
        except Exception as e:
            logging.error(f"Unexpected error in get_snapshots: {e}")
            raise QueryError(f"Unexpected error in get_snapshots: {e}") from e

    async def get_latest_snapshots_before(self, timestamp: datetime) -> List[NodeInfo]:
        """
        Retrieves the latest snapshot for each node before the given timestamp.
        """
        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                # SQL to get the latest snapshot per node before timestamp
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
        except sqlite3.Error as e:
            logging.error(f"Could not retrieve latest snapshots: {e}")
            raise QueryError(f"Could not retrieve latest snapshots: {e}") from e
        except Exception as e:
            logging.error(f"Unexpected error in get_latest_snapshots_before: {e}")
            raise QueryError(f"Unexpected error in get_latest_snapshots_before: {e}") from e
