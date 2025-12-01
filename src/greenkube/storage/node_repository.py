# src/greenkube/storage/node_repository.py

"""
Repository for managing node data in the database.
"""

import logging
import sqlite3
from datetime import datetime, timezone
from typing import List

from greenkube.models.node import NodeInfo

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
                cursor.execute(
                    """
                    INSERT INTO node_snapshots
                        (timestamp, node_name, instance_type, cpu_capacity_cores, architecture,
                         cloud_provider, region, zone, node_pool, memory_capacity_bytes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(node_name, timestamp) DO NOTHING;
                """,
                    (
                        now,
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
