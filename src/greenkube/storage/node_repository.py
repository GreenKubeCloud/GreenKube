# src/greenkube/storage/node_repository.py

"""
Repository for managing node data in the database.
"""

import logging
import sqlite3
from datetime import datetime, timezone
from typing import List

from greenkube.models.node import NodeInfo
from greenkube.utils.date_utils import parse_iso_date

logger = logging.getLogger(__name__)


class NodeRepository:
    """
    Repository for managing node data in the database.
    """

    def __init__(self, connection):
        self.conn = connection
        if not self.conn:
            logging.error("SQLite connection is not available upon initialization.")

    def save_nodes(self, nodes: List[NodeInfo]) -> int:
        """
        Saves node snapshots to the database.
        """
        if not self.conn:
            logging.error("SQLite connection is not available for save_nodes.")
            return 0

        cursor = self.conn.cursor()
        saved_count = 0
        now = datetime.now(timezone.utc).isoformat()

        for node in nodes:
            try:
                # Use node.timestamp if available, otherwise use current time
                ts = node.timestamp.isoformat() if node.timestamp else now
                cursor.execute(
                    """
                    INSERT INTO node_snapshots
                        (timestamp, node_name, instance_type, cpu_capacity_cores, architecture,
                         cloud_provider, region, zone, node_pool, memory_capacity_bytes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    ),
                )
                saved_count += cursor.rowcount
            except sqlite3.Error as e:
                logging.error(f"Could not save node snapshot for {node.name}: {e}")
            except Exception as e:
                logging.error(f"Unexpected error processing node {node.name}: {e}")

        try:
            self.conn.commit()
        except sqlite3.Error as e:
            logging.error(f"Failed to commit transaction for nodes: {e}")
            return 0

        return saved_count

    def get_snapshots(self, start: datetime, end: datetime) -> List[NodeInfo]:
        """
        Retrieves node snapshots within a time range.
        """
        if not self.conn:
            logging.error("SQLite connection is not available for get_snapshots.")
            return []

        cursor = self.conn.cursor()
        try:
            cursor.execute(
                """
                SELECT node_name, instance_type, zone, region, cloud_provider, architecture,
                       node_pool, cpu_capacity_cores, memory_capacity_bytes, timestamp
                FROM node_snapshots
                WHERE timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp ASC
            """,
                (start.isoformat(), end.isoformat()),
            )
            rows = cursor.fetchall()
            # Return a list of (timestamp, NodeInfo) tuples

            return [
                (
                    row[9],  # timestamp string
                    NodeInfo(
                        name=row[0],
                        instance_type=row[1],
                        zone=row[2],
                        region=row[3],
                        cloud_provider=row[4],
                        architecture=row[5],
                        node_pool=row[6],
                        cpu_capacity_cores=row[7],
                        memory_capacity_bytes=row[8],
                        timestamp=parse_iso_date(row[9]),  # Parse timestamp string to datetime
                    ),
                )
                for row in rows
            ]

        except sqlite3.Error as e:
            logging.error(f"Could not retrieve snapshots: {e}")
            return []

    def get_latest_snapshots_before(self, timestamp: datetime) -> List[NodeInfo]:
        """
        Retrieves the latest snapshot for each node before the given timestamp.
        """
        if not self.conn:
            logging.error("SQLite connection is not available for get_latest_snapshots_before.")
            return []

        cursor = self.conn.cursor()
        try:
            # SQL to get the latest snapshot per node before timestamp
            cursor.execute(
                """
                SELECT ns.node_name, ns.instance_type, ns.zone, ns.region, ns.cloud_provider,
                       ns.architecture, ns.node_pool, ns.cpu_capacity_cores, ns.memory_capacity_bytes, ns.timestamp
                FROM node_snapshots ns
                INNER JOIN (
                    SELECT node_name, MAX(timestamp) as max_ts
                    FROM node_snapshots
                    WHERE timestamp < ?
                    GROUP BY node_name
                ) latest ON ns.node_name = latest.node_name AND ns.timestamp = latest.max_ts
            """,
                (timestamp.isoformat(),),
            )
            rows = cursor.fetchall()
            return [
                NodeInfo(
                    name=row[0],
                    instance_type=row[1],
                    zone=row[2],
                    region=row[3],
                    cloud_provider=row[4],
                    architecture=row[5],
                    node_pool=row[6],
                    cpu_capacity_cores=row[7],
                    memory_capacity_bytes=row[8],
                    timestamp=parse_iso_date(row[9]),  # Parse timestamp string to datetime
                )
                for row in rows
            ]
        except sqlite3.Error as e:
            logging.error(f"Could not retrieve latest snapshots: {e}")
            return []
