import hashlib
import logging
from datetime import datetime
from typing import List

from elasticsearch import AsyncElasticsearch
from elasticsearch_dsl import Date, Document, Float, Keyword, Text

# Import connections conditionally
if "connections" not in globals():
    try:
        from elasticsearch_dsl import connections
    except Exception:
        connections = None

# Import async_bulk helper
try:
    from elasticsearch.helpers import async_bulk
except ImportError:
    async_bulk = None

from elasticsearch.exceptions import (
    ConnectionError,
    TransportError,
)

from ..core.config import config
from ..models.metrics import CombinedMetric
from .base_repository import CarbonIntensityRepository

logger = logging.getLogger(__name__)


class CarbonIntensityDoc(Document):
    """
    Elasticsearch Document representing a carbon intensity record.
    Uses the index name from the config.
    """

    zone = Keyword(required=True)
    carbon_intensity = Float(required=True)
    datetime = Date(required=True)
    updated_at = Date()
    created_at = Date()
    emission_factor_type = Text()
    is_estimated = Keyword()
    estimation_method = Text()

    class Index:
        # Use the index name from the config object
        name = config.ELASTICSEARCH_INDEX_NAME
        settings = {"number_of_shards": 1, "number_of_replicas": 0}


class CombinedMetricDoc(Document):
    """
    Elasticsearch Document representing a combined metric record.
    """

    pod_name = Keyword(required=True)
    namespace = Keyword(required=True)
    total_cost = Float()
    co2e_grams = Float()
    embodied_co2e_grams = Float()
    pue = Float()
    grid_intensity = Float()
    joules = Float()
    cpu_request = Float()
    memory_request = Float()
    cpu_usage_millicores = Float()
    memory_usage_bytes = Float()
    network_receive_bytes = Float()
    network_transmit_bytes = Float()
    disk_read_bytes = Float()
    disk_write_bytes = Float()
    storage_request_bytes = Float()
    storage_usage_bytes = Float()
    ephemeral_storage_request_bytes = Float()
    ephemeral_storage_usage_bytes = Float()
    gpu_usage_millicores = Float()
    restart_count = Float()
    period = Text()
    timestamp = Date()
    duration_seconds = Float()
    grid_intensity_timestamp = Date()
    node_instance_type = Keyword()
    node_zone = Keyword()
    emaps_zone = Keyword()
    is_estimated = Keyword()
    estimation_reasons = Text()

    class Index:
        name = "greenkube_combined_metrics"
        settings = {"number_of_shards": 1, "number_of_replicas": 0}


async def setup_elasticsearch():
    """
    Sets up the async connection to Elasticsearch using configuration settings.
    Initializes indices.
    """
    try:
        connection_args = {
            "hosts": [config.ELASTICSEARCH_HOSTS],
            "verify_certs": config.ELASTICSEARCH_VERIFY_CERTS,
        }
        if config.ELASTICSEARCH_USER and config.ELASTICSEARCH_PASSWORD:
            logging.info("Connecting to Elasticsearch with authentication.")
            connection_args["basic_auth"] = (
                config.ELASTICSEARCH_USER,
                config.ELASTICSEARCH_PASSWORD,
            )
        else:
            logging.info("Connecting to Elasticsearch without authentication.")

        # Create the connection alias 'default' using AsyncElasticsearch
        connections.create_connection("default", class_=AsyncElasticsearch, **connection_args)

        conn = connections.get_connection("default")
        if not await conn.ping():
            logging.error("Could not ping Elasticsearch.")
            raise ConnectionError("Failed to connect to Elasticsearch: Ping failed.")

        logging.info("Successfully connected to Elasticsearch.")

        # Initialize indices
        # We need to import NodeSnapshotDoc here to avoid circular dependencies at top level if any,
        # or just to keep initialization logic centralized.
        from .elasticsearch_node_repository import NodeSnapshotDoc
        from .embodied_repository import InstanceCarbonProfileDoc

        await CarbonIntensityDoc.init()
        await CombinedMetricDoc.init()
        await NodeSnapshotDoc.init()
        await InstanceCarbonProfileDoc.init()

        logging.info(f"Elasticsearch index '{config.ELASTICSEARCH_INDEX_NAME}' is ready.")
        logging.info(f"Elasticsearch index '{CombinedMetricDoc.Index.name}' is ready.")
        logging.info(f"Elasticsearch index '{NodeSnapshotDoc.Index.name}' is ready.")
        logging.info(f"Elasticsearch index '{InstanceCarbonProfileDoc.Index.name}' is ready.")

        return True

    except ConnectionError as ce:
        logging.error(f"Failed to connect to Elasticsearch: {ce}")
        raise
    except TransportError as te:
        logging.error(f"Elasticsearch transport error during connection setup: {te}")
        raise ConnectionError(f"Elasticsearch transport error during connection setup: {te}")
    except Exception as e:
        logging.error(f"Unexpected error during Elasticsearch connection setup: {e}")
        raise ConnectionError(f"Unexpected error during Elasticsearch connection setup: {e}")


class ElasticsearchCarbonIntensityRepository(CarbonIntensityRepository):
    """
    Repository for handling carbon intensity data with Elasticsearch.
    """

    def __init__(self):
        """
        Initializes the repository. Connection setup is handled separately.
        """
        pass

    async def get_for_zone_at_time(self, zone: str, timestamp: str) -> float | None:
        """
        Retrieves the latest carbon intensity for a given zone at or before a specific timestamp.
        """
        try:
            # Check connection implicitly via execute or explicit ping if desired,
            # but usually rely on client handling.

            # search() creates a Search object which is lazy
            s = (
                CarbonIntensityDoc.search()
                .filter("term", zone=zone)
                .filter("range", datetime={"lte": timestamp})
                .sort("-datetime")
            )

            response = await s.execute()

            if response.hits:
                return response.hits[0].carbon_intensity
            else:
                logging.debug(f"No carbon intensity data found for zone {zone} at or before {timestamp}")
                return None
        except Exception as e:
            # Broad exception handling to catch async errors or transport errors
            logging.error(f"Error retrieving data from Elasticsearch for zone {zone}: {e}")
            return None

    async def save_history(self, history_data: list, zone: str) -> int:
        """
        Saves a batch of historical carbon intensity data to Elasticsearch.
        Uses the async_bulk helper for efficient insertion.
        """
        if not history_data:
            return 0

        actions = []
        for record in history_data:
            if not isinstance(record, dict):
                logging.warning(f"Skipping invalid record for zone {zone}: {record}")
                continue
            doc_id = f"{zone}-{record.get('datetime')}"
            if not record.get("datetime"):
                continue

            actions.append(
                {
                    "_op_type": "index",
                    "_index": config.ELASTICSEARCH_INDEX_NAME,
                    "_id": doc_id,
                    "_source": {
                        "zone": zone,
                        "carbon_intensity": record.get("carbonIntensity"),
                        "datetime": record.get("datetime"),
                        "updated_at": record.get("updatedAt"),
                        "created_at": record.get("createdAt"),
                        "emission_factor_type": record.get("emissionFactorType"),
                        "is_estimated": record.get("isEstimated"),
                        "estimation_method": record.get("estimationMethod"),
                    },
                }
            )

        if not actions:
            return 0

        try:
            conn = connections.get_connection("default")
            # async_bulk returns (success_count, list_of_errors) if raise_on_error=False
            # or number of successes if raise_on_error=True (default) ?
            # Actually async_bulk signature matches bulk.

            success_count, errors = await async_bulk(
                client=conn,
                actions=actions,
                raise_on_error=True,
                stats_only=False,
                request_timeout=60,
            )
            logging.info(f"Successfully saved {success_count} records to Elasticsearch for zone {zone}.")
            return success_count
        except Exception as e:
            logging.error(f"Failed to bulk save to Elasticsearch for zone {zone}: {e}")
            return 0

    async def write_combined_metrics(self, metrics: List[CombinedMetric]) -> int:
        if not metrics:
            return 0

        actions = []
        for metric in metrics:
            namespace = metric.namespace or "default"
            pod_name = metric.pod_name or "unknown"
            timestamp = metric.timestamp or ""
            duration = metric.duration_seconds or 0

            id_string = f"{namespace}-{pod_name}-{timestamp.isoformat() if timestamp else ''}-{duration}"
            doc_id = hashlib.sha256(id_string.encode("utf-8")).hexdigest()

            actions.append(
                {
                    "_op_type": "index",
                    "_index": CombinedMetricDoc.Index.name,
                    "_id": doc_id,
                    "_source": metric.model_dump(),
                }
            )

        if not actions:
            return 0

        try:
            conn = connections.get_connection("default")
            success_count, errors = await async_bulk(
                client=conn,
                actions=actions,
                raise_on_error=True,
                stats_only=False,
                request_timeout=60,
            )
            logging.info(f"Successfully saved {success_count} combined metrics to Elasticsearch.")
            return success_count
        except Exception as e:
            logging.error(f"Unexpected error during bulk save of combined metrics to Elasticsearch: {e}")
            return 0

    async def read_combined_metrics(self, start_time: datetime, end_time: datetime) -> List[CombinedMetric]:
        try:
            # Check connection if needed or just execute
            s = CombinedMetricDoc.search().filter("range", timestamp={"gte": start_time, "lte": end_time})

            metrics = []
            async for hit in s.scan():
                metrics.append(CombinedMetric.model_validate(hit.to_dict()))

            return metrics
        except Exception as e:
            logging.error(f"Unexpected error retrieving combined metrics from Elasticsearch: {e}")
            return []
