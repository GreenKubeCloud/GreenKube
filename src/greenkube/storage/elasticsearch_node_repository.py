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
                        "timestamp": node.timestamp if node.timestamp else now,
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

    def get_snapshots(self, start: datetime, end: datetime) -> List[tuple[str, NodeInfo]]:
        """
        Retrieves node snapshots within a time range.
        """
        try:
            s = NodeSnapshotDoc.search().filter("range", timestamp={"gte": start, "lte": end}).sort("timestamp")
            # Use scan to get all results
            hits = s.scan()

            results = []
            for hit in hits:
                node_info = NodeInfo(
                    name=hit.node_name,
                    instance_type=hit.instance_type,
                    zone=hit.zone,
                    region=hit.region,
                    cloud_provider=hit.cloud_provider,
                    architecture=hit.architecture,
                    node_pool=hit.node_pool,
                    cpu_capacity_cores=hit.cpu_capacity_cores,
                    memory_capacity_bytes=hit.memory_capacity_bytes,
                    timestamp=hit.timestamp,
                )
                # hit.timestamp is usually a datetime object or string depending on deserialization
                # elasticsearch-dsl usually returns datetime if mapped as Date
                ts = hit.timestamp
                if isinstance(ts, datetime):
                    ts_str = ts.isoformat()
                else:
                    ts_str = str(ts)

                results.append((ts_str, node_info))
            return results

        except Exception as e:
            logging.error(f"Failed to retrieve snapshots from Elasticsearch: {e}")
            return []

    def get_latest_snapshots_before(self, timestamp: datetime) -> List[NodeInfo]:
        """
        Retrieves the latest snapshot for each node before the given timestamp.
        """
        try:
            # This is tricky in ES. We want the latest document per node_name where timestamp < timestamp.
            # A terms aggregation on node_name with a top_hits sub-aggregation sorted by timestamp desc, size 1.

            s = NodeSnapshotDoc.search()
            s = s.filter("range", timestamp={"lt": timestamp})

            # We need to aggregate by node_name
            # Since we don't know how many nodes there are, we might need a large size for terms agg
            # or use composite agg. For simplicity, let's assume a reasonable max nodes (e.g. 10000).

            s.aggs.bucket("nodes", "terms", field="node_name", size=10000).metric(
                "latest_snapshot", "top_hits", size=1, sort=[{"timestamp": {"order": "desc"}}]
            )

            # We don't need hits, just aggs
            response = s.execute()

            results = []
            for bucket in response.aggregations.nodes.buckets:
                hits = bucket.latest_snapshot.hits.hits
                if hits:
                    hit = hits[0]["_source"]
                    # Map dict to NodeInfo
                    node_info = NodeInfo(
                        name=hit.get("node_name"),
                        instance_type=hit.get("instance_type"),
                        zone=hit.get("zone"),
                        region=hit.get("region"),
                        cloud_provider=hit.get("cloud_provider"),
                        architecture=hit.get("architecture"),
                        node_pool=hit.get("node_pool"),
                        cpu_capacity_cores=hit.get("cpu_capacity_cores"),
                        memory_capacity_bytes=hit.get("memory_capacity_bytes"),
                        timestamp=hit.get("timestamp"),
                    )
                    results.append(node_info)

            return results

        except Exception as e:
            logging.error(f"Failed to retrieve latest snapshots from Elasticsearch: {e}")
            return []
