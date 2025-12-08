import logging
from datetime import datetime
from typing import List

import psycopg2
import psycopg2.extras

from ..core.exceptions import QueryError
from ..models.node import NodeInfo
from ..storage.base_repository import NodeRepository

logger = logging.getLogger(__name__)


class PostgresNodeRepository(NodeRepository):
    def __init__(self, db_manager):
        self.db_manager = db_manager

    def save_nodes(self, nodes: List[NodeInfo]) -> int:
        """
        Saves node snapshots to the repository.
        """
        if not nodes:
            return 0

        try:
            with self.db_manager.connection_scope() as conn:
                with conn.cursor() as cursor:
                    query = """
                        INSERT INTO node_snapshots (
                            timestamp, node_name, instance_type, cpu_capacity_cores,
                            architecture, cloud_provider, region, zone, node_pool,
                            memory_capacity_bytes
                        ) VALUES (
                            %(timestamp)s, %(node_name)s, %(instance_type)s, %(cpu_capacity_cores)s,
                            %(architecture)s, %(cloud_provider)s, %(region)s, %(zone)s, %(node_pool)s,
                            %(memory_capacity_bytes)s
                        )
                        ON CONFLICT (node_name, timestamp) DO NOTHING
                    """

                    # Convert Pydantic models to dicts for insertion and map 'name' to 'node_name'
                    nodes_data = []
                    for node in nodes:
                        data = node.model_dump()
                        data["node_name"] = data.pop("name")
                        nodes_data.append(data)

                    cursor.executemany(query, nodes_data)
                    conn.commit()
                    logger.info(f"Saved snapshot of {len(nodes)} nodes to Postgres.")
                    return len(nodes)
        except Exception as e:
            logger.error(f"Error saving node snapshot to Postgres: {e}")
            raise QueryError(f"Error saving node snapshot: {e}") from e

    def get_snapshots(self, start: datetime, end: datetime) -> List[tuple[str, NodeInfo]]:
        """
        Retrieves node snapshots within a time range.
        """
        try:
            with self.db_manager.connection_scope() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                    query = """
                        SELECT * FROM node_snapshots
                        WHERE timestamp >= %s AND timestamp <= %s
                    """
                    cursor.execute(query, (start, end))
                    results = cursor.fetchall()

                    snapshots = []
                    for row in results:
                        timestamp_str = row.pop("timestamp").isoformat()
                        # Remove id if present as it's not in NodeInfo
                        row.pop("id", None)
                        # Map 'node_name' back to 'name'
                        row["name"] = row.pop("node_name")
                        snapshots.append((timestamp_str, NodeInfo(**row)))

                    return snapshots
        except Exception as e:
            logger.error(f"Error getting snapshots from Postgres: {e}")
            raise QueryError(f"Error getting snapshots: {e}") from e

    def get_latest_snapshots_before(self, timestamp: datetime) -> List[NodeInfo]:
        """
        Retrieves the latest snapshot for each node before the given timestamp.
        """
        try:
            with self.db_manager.connection_scope() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                    # Subquery to find the latest timestamp for each node before the cutoff
                    query = """
                        SELECT DISTINCT ON (node_name) *
                        FROM node_snapshots
                        WHERE timestamp <= %s
                        ORDER BY node_name, timestamp DESC
                    """
                    cursor.execute(query, (timestamp,))
                    results = cursor.fetchall()

                    nodes = []
                    for row in results:
                        # Remove id and timestamp (if not needed in NodeInfo, but NodeInfo has timestamp)
                        row.pop("id", None)
                        # Map 'node_name' back to 'name'
                        row["name"] = row.pop("node_name")
                        nodes.append(NodeInfo(**row))

                    return nodes
        except Exception as e:
            logger.error(f"Error getting latest snapshots from Postgres: {e}")
            raise QueryError(f"Error getting latest snapshots: {e}") from e
