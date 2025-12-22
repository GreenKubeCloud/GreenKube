import logging
from datetime import datetime
from typing import List

from ..core.exceptions import QueryError
from ..models.node import NodeInfo
from ..storage.base_repository import NodeRepository

logger = logging.getLogger(__name__)


class PostgresNodeRepository(NodeRepository):
    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def save_nodes(self, nodes: List[NodeInfo]) -> int:
        """
        Saves node snapshots to the repository.
        """
        if not nodes:
            return 0

        try:
            async with self.db_manager.connection_scope() as conn:
                query = """
                    INSERT INTO node_snapshots (
                        timestamp, node_name, instance_type, cpu_capacity_cores,
                        architecture, cloud_provider, region, zone, node_pool,
                        memory_capacity_bytes, embodied_emissions_kg
                    ) VALUES (
                        $1, $2, $3, $4,
                        $5, $6, $7, $8, $9,
                        $10, $11
                    )
                    ON CONFLICT (node_name, timestamp) DO NOTHING
                """

                # Convert Pydantic models to tuples for insertion
                nodes_data = []
                for node in nodes:
                    nodes_data.append(
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
                    )

                await conn.executemany(query, nodes_data)
                logger.info(f"Saved snapshot of {len(nodes)} nodes to Postgres.")
                return len(nodes)
        except Exception as e:
            logger.error(f"Error saving node snapshot to Postgres: {e}")
            raise QueryError(f"Error saving node snapshot: {e}") from e

    async def get_snapshots(self, start: datetime, end: datetime) -> List[tuple[str, NodeInfo]]:
        """
        Retrieves node snapshots within a time range.
        """
        try:
            async with self.db_manager.connection_scope() as conn:
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
                    # Extract timestamp (datetime object)
                    ts = data["timestamp"]
                    timestamp_str = ts.isoformat()

                    # Map 'node_name' back to 'name' and remove from dict
                    data["name"] = data.pop("node_name")

                    # Create NodeInfo. Dictionary still contains 'timestamp' which is valid for NodeInfo.
                    snapshots.append((timestamp_str, NodeInfo(**data)))

                return snapshots
        except Exception as e:
            logger.error(f"Error getting snapshots from Postgres: {e}")
            raise QueryError(f"Error getting snapshots: {e}") from e

    async def get_latest_snapshots_before(self, timestamp: datetime) -> List[NodeInfo]:
        """
        Retrieves the latest snapshot for each node before the given timestamp.
        """
        try:
            async with self.db_manager.connection_scope() as conn:
                # Subquery to find the latest timestamp for each node before the cutoff
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
            logger.error(f"Error getting latest snapshots from Postgres: {e}")
            raise QueryError(f"Error getting latest snapshots: {e}") from e
