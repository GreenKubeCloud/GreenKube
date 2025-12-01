# src/greenkube/storage/elasticsearch_node_repository.py

"""
Repository for managing node data in Elasticsearch.
"""

import logging
from datetime import datetime, timezone
from typing import List

from elasticsearch_dsl import Date, Document, Float, Keyword, Long

# Import connections and bulk conditionally
if "connections" not in globals():
    try:
        from elasticsearch_dsl import connections
    except Exception:
        connections = None

if "bulk" not in globals():
    try:
        from elasticsearch.helpers import bulk
    except Exception:
        bulk = None

from elasticsearch.exceptions import (
    ConnectionError,
)

from greenkube.models.node import NodeInfo

logger = logging.getLogger(__name__)


class NodeSnapshotDoc(Document):
    """
    Elasticsearch Document representing a node snapshot.
    """

    timestamp = Date(required=True)
    node_name = Keyword(required=True)
    instance_type = Keyword()
    cpu_capacity_cores = Float()
    architecture = Keyword()
    cloud_provider = Keyword()
    region = Keyword()
    zone = Keyword()
    node_pool = Keyword()
    memory_capacity_bytes = Long()

    class Index:
        name = "greenkube_node_snapshots"
        settings = {"number_of_shards": 1, "number_of_replicas": 0}


class ElasticsearchNodeRepository:
    """
    Repository for managing node data in Elasticsearch.
    """

    def __init__(self):
        """
        Initializes the repository and ensures the Elasticsearch index exists.
        """
        try:
            conn = connections.get_connection("default")
            if not conn.ping():
                raise ConnectionError("Elasticsearch connection lost before init.")

            NodeSnapshotDoc.init()
            logging.info(f"Elasticsearch index '{NodeSnapshotDoc.Index.name}' is ready.")
        except Exception as e:
            logging.error(f"Failed to initialize ElasticsearchNodeRepository: {e}")

    def save_nodes(self, nodes: List[NodeInfo]) -> int:
        """
        Saves node snapshots to Elasticsearch.
        """
        if not nodes:
            return 0

        actions = []
        now = datetime.now(timezone.utc)

        for node in nodes:
            doc_id = f"{node.name}-{now.isoformat()}"

            actions.append(
                {
                    "_op_type": "index",
                    "_index": NodeSnapshotDoc.Index.name,
                    "_id": doc_id,
                    "_source": {
                        "timestamp": now,
                        "node_name": node.name,
                        "instance_type": node.instance_type,
                        "cpu_capacity_cores": node.cpu_capacity_cores,
                        "architecture": node.architecture,
                        "cloud_provider": node.cloud_provider,
                        "region": node.region,
                        "zone": node.zone,
                        "node_pool": node.node_pool,
                        "memory_capacity_bytes": node.memory_capacity_bytes,
                    },
                }
            )

        if not actions:
            return 0

        try:
            conn = connections.get_connection("default")
            success_count, errors = bulk(
                client=conn,
                actions=actions,
                raise_on_error=True,
                stats_only=False,
                request_timeout=60,
            )
            logging.info(f"Successfully saved {success_count} node snapshots to Elasticsearch.")
            return success_count

        except Exception as e:
            logging.error(f"Failed to save node snapshots to Elasticsearch: {e}")
            return 0
