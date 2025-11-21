import hashlib
import logging
from typing import List

from elasticsearch_dsl import Date, Document, Float, Keyword, Text

# Import connections and bulk conditionally so test fixtures that patch
# `src.greenkube.storage.elasticsearch_repository.connections` or
# `...bulk` are not overwritten on module reload.
if "connections" not in globals():
    try:
        from elasticsearch_dsl import connections
    except Exception:
        connections = None

# Bulk helper
if "bulk" not in globals():
    try:
        from elasticsearch.helpers import bulk
    except Exception:
        bulk = None
# Import the base TransportError and specific exceptions from .exceptions
from elasticsearch.exceptions import (
    ConnectionError,
    NotFoundError,
    RequestError,
    TransportError,
)

from ..core.config import config
from ..models.metrics import CombinedMetric
from .base_repository import CarbonIntensityRepository

logger = logging.getLogger(__name__)


def setup_connection():
    """
    Sets up the connection to Elasticsearch using configuration settings.
    Handles both simple and authenticated connections.
    Raises ConnectionError if connection fails.
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

        # Create the connection alias 'default'
        connections.create_connection("default", **connection_args)

        # Verify the connection using the created alias
        if not connections.get_connection("default").ping():
            # Log and raise an error if ping fails
            logging.error("Could not ping Elasticsearch.")
            raise ConnectionError("Failed to connect to Elasticsearch: Ping failed.")

        logging.info("Successfully connected to Elasticsearch.")
        return True

    except ConnectionError as ce:
        # Re-raise specific connection errors
        logging.error(f"Failed to connect to Elasticsearch: {ce}")
        raise
    # Catch TransportError as the base Elasticsearch exception during setup
    except TransportError as te:
        logging.error(f"Elasticsearch transport error during connection setup: {te}")
        raise ConnectionError(f"Elasticsearch transport error during connection setup: {te}")
    except Exception as e:
        # Catch any other unexpected exceptions during setup
        logging.error(f"Unexpected error during Elasticsearch connection setup: {e}")
        raise ConnectionError(f"Unexpected error during Elasticsearch connection setup: {e}")


# Establish the connection when the module is loaded.
# Do not establish the connection at module import time. Tests patch
# `setup_connection` and reload the module; calling it here interferes
# with those tests and causes unpredictable behavior. Connections
# should be established explicitly by the application when needed.


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
    is_estimated = Keyword()  # Or Boolean() if data is consistently true/false
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
    pue = Float()
    grid_intensity = Float()
    joules = Float()
    cpu_request = Float()
    memory_request = Float()
    period = Text()
    timestamp = Date()
    duration_seconds = Float()
    grid_intensity_timestamp = Date()
    node_instance_type = Keyword()
    node_zone = Keyword()
    emaps_zone = Keyword()

    class Index:
        name = "greenkube_combined_metrics"
        settings = {"number_of_shards": 1, "number_of_replicas": 0}


class ElasticsearchCarbonIntensityRepository(CarbonIntensityRepository):
    """
    Repository for handling carbon intensity data with Elasticsearch.
    """

    def __init__(self):
        """
        Initializes the repository and ensures the Elasticsearch index exists and is mapped.
        """
        try:
            # Check if connection exists before proceeding
            conn = connections.get_connection("default")
            if not conn.ping():
                raise ConnectionError("Elasticsearch connection lost before init.")

            # init() ensures the index exists with the correct mapping.
            # Call without `using` to avoid delegating to the connection internals
            # which tests may mock.
            CarbonIntensityDoc.init()
            CombinedMetricDoc.init()
            # Use configured index name for logging to avoid depending on
            # elasticsearch-dsl's internal _index attribute during tests.
            logging.info(f"Elasticsearch index '{config.ELASTICSEARCH_INDEX_NAME}' is ready.")
            logging.info(f"Elasticsearch index '{CombinedMetricDoc.Index.name}' is ready.")
        except ConnectionError as ce:
            logging.error(f"Elasticsearch connection error during index initialization: {ce}")
        except RequestError as re:
            # Handle specific Elasticsearch request errors (e.g., mapping conflicts)
            logging.error(f"Elasticsearch request error during index initialization: {re.info}")
            # You might need manual intervention if there's a mapping conflict
        # Catch TransportError as the base Elasticsearch exception
        except TransportError as te:
            logging.error(f"Elasticsearch transport error during index initialization: {te}")
        except Exception as e:
            # Catch any other unexpected errors
            logging.error(f"Unexpected error during Elasticsearch index initialization: {e}")
            # Consider re-raising for critical failures

    def get_for_zone_at_time(self, zone: str, timestamp: str) -> float | None:
        """
        Retrieves the latest carbon intensity for a given zone at or before a specific timestamp.
        """
        try:
            conn = connections.get_connection("default")
            if not conn.ping():
                raise ConnectionError("Elasticsearch connection lost before search.")

            # Call search without using=conn to ensure test patches on
            # CarbonIntensityDoc.search are exercised. The mock search
            # object provided by tests handles filter/sort/execute chaining.
            s = (
                CarbonIntensityDoc.search()
                .filter("term", zone=zone)
                .filter("range", datetime={"lte": timestamp})
                .sort("-datetime")
            )

            response = s.execute()

            if response.hits:
                return response.hits[0].carbon_intensity
            else:
                logging.debug(f"No carbon intensity data found for zone {zone} at or before {timestamp}")
                return None
        except ConnectionError as ce:
            logging.error(f"Elasticsearch connection error during get_for_zone_at_time: {ce}")
            return None
        except NotFoundError:
            logging.error(
                f"Elasticsearch index '{CarbonIntensityDoc._index._name}' not found during search."
            )  # Uses configured name
            return None
        except RequestError as re:
            logging.error(f"Elasticsearch query error for zone {zone} at {timestamp}: {re.info}")
            return None
        # Catch TransportError as the base Elasticsearch exception
        except TransportError as te:
            logging.error(f"Elasticsearch transport error during search for zone {zone}: {te}")
            return None
        except Exception as e:
            logging.error(f"Unexpected error retrieving data from Elasticsearch for zone {zone}: {e}")
            return None

    def save_history(self, history_data: list, zone: str) -> int:
        """
        Saves a batch of historical carbon intensity data to Elasticsearch.
        Uses the bulk helper for efficient insertion and handles potential errors.
        """
        if not history_data:
            return 0

        actions = []
        for record in history_data:
            if not isinstance(record, dict):
                logging.warning(f"Skipping invalid record (not a dict) for zone {zone}: {record}")
                continue
            doc_id = f"{zone}-{record.get('datetime')}"
            if not record.get("datetime"):
                logging.warning(f"Skipping record with missing datetime for zone {zone}: {record}")
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
            logging.info(f"No valid actions to save for zone {zone} after filtering.")
            return 0

        try:
            conn = connections.get_connection("default")
            if not conn.ping():
                raise ConnectionError("Elasticsearch connection lost before bulk save.")

            success_count, errors = bulk(
                client=conn,
                actions=actions,
                raise_on_error=True,
                stats_only=False,
                request_timeout=60,
            )
            logging.info(f"Successfully saved {success_count} records to Elasticsearch for zone {zone}.")

            return success_count

        except ConnectionError as ce:
            logging.error(f"Elasticsearch connection error during bulk save: {ce}")
            return 0
        # Catch TransportError as the base Elasticsearch exception for bulk operations
        except TransportError as te:
            logging.error(f"Failed to bulk save to Elasticsearch for zone {zone}: {te}")
            # If te contains detailed errors (like from BulkIndexError which inherits TransportError), log them
            # if hasattr(te, 'errors'): logging.error(f"Bulk errors: {te.errors}")
            return 0
        except Exception as e:
            logging.error(f"Unexpected error during bulk save to Elasticsearch for zone {zone}: {e}")
            return 0

    def write_combined_metrics(self, metrics: List[CombinedMetric]) -> int:
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
                    "_id": doc_id,  # This ensures documents are idempotent
                    "_source": metric.model_dump(),
                }
            )

        if not actions:
            return 0

        try:
            conn = connections.get_connection("default")
            if not conn.ping():
                raise ConnectionError("Elasticsearch connection lost before bulk save.")

            success_count, errors = bulk(
                client=conn,
                actions=actions,
                raise_on_error=True,
                stats_only=False,
                request_timeout=60,
            )
            logging.info(f"Successfully saved {success_count} combined metrics to Elasticsearch.")
            return success_count
        except ConnectionError as ce:
            logging.error(f"Elasticsearch connection error during bulk save of combined metrics: {ce}")
            return 0
        except TransportError as te:
            logging.error(f"Failed to bulk save combined metrics to Elasticsearch: {te}")
            return 0
        except Exception as e:
            logging.error(f"Unexpected error during bulk save of combined metrics to Elasticsearch: {e}")
            return 0

    def read_combined_metrics(self, start_time, end_time) -> List[CombinedMetric]:
        try:
            conn = connections.get_connection("default")
            if not conn.ping():
                raise ConnectionError("Elasticsearch connection lost before search.")

            s = CombinedMetricDoc.search().filter("range", timestamp={"gte": start_time, "lte": end_time})

            # Use .scan() to retrieve all documents matching the query, not just the first page.
            all_hits = s.scan()

            return [CombinedMetric.model_validate(hit.to_dict()) for hit in all_hits]
        except ConnectionError as ce:
            logging.error(f"Elasticsearch connection error during read_combined_metrics: {ce}")
            return []
        except NotFoundError:
            logging.error(f"Elasticsearch index '{CombinedMetricDoc.Index.name}' not found during search.")
            return []
        except RequestError as re:
            logging.error(f"Elasticsearch query error for combined metrics: {re.info}")
            return []
        except TransportError as te:
            logging.error(f"Elasticsearch transport error during search for combined metrics: {te}")
            return []
        except Exception as e:
            logging.error(f"Unexpected error retrieving combined metrics from Elasticsearch: {e}")
            return []
