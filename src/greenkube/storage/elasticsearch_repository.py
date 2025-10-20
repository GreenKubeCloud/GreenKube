# src/greenkube/storage/elasticsearch_repository.py
from elasticsearch_dsl import Document, Date, Float, Text, Keyword, connections
from elasticsearch.helpers import bulk
from ..core.config import config
from .base_repository import CarbonIntensityRepository

def setup_connection():
    """
    Sets up the connection to Elasticsearch using configuration settings.
    Handles both simple and authenticated connections.
    """
    try:
        if config.ELASTICSEARCH_USER and config.ELASTICSEARCH_PASSWORD:
            print("Connecting to Elasticsearch with authentication.")
            connections.create_connection(
                hosts=[config.ELASTICSEARCH_HOSTS],
                basic_auth=(config.ELASTICSEARCH_USER, config.ELASTICSEARCH_PASSWORD),
                verify_certs=config.ELASTICSEARCH_VERIFY_CERTS
            )
        else:
            print("Connecting to Elasticsearch without authentication.")
            connections.create_connection(hosts=[config.ELASTICSEARCH_HOSTS])
        
        # Verify the connection
        if not connections.get_connection().ping():
            print("ERROR: Could not connect to Elasticsearch.")
            return False
            
        print("INFO: Successfully connected to Elasticsearch.")
        return True

    except Exception as e:
        print(f"ERROR: Failed to connect to Elasticsearch: {e}")
        return False

# Establish the connection when the module is loaded.
setup_connection()

class CarbonIntensityDoc(Document):
    """
    Elasticsearch Document representing a carbon intensity record.
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
        name = 'carbon_intensity'
        settings = {
            "number_of_shards": 1,
            "number_of_replicas": 0
        }

class ElasticsearchCarbonIntensityRepository(CarbonIntensityRepository):
    """
    Repository for handling carbon intensity data with Elasticsearch.
    """
    def __init__(self):
        """
        Initializes the repository and ensures the Elasticsearch index is created.
        """
        try:
            CarbonIntensityDoc.init()
            print("INFO: Elasticsearch index 'carbon_intensity' is ready.")
        except Exception as e:
            print(f"ERROR: Could not initialize Elasticsearch index 'carbon_intensity': {e}")

    def get_for_zone_at_time(self, zone: str, timestamp: str) -> float | None:
        """
        Retrieves the latest carbon intensity for a given zone at or before a specific timestamp.
        """
        try:
            s = CarbonIntensityDoc.search().filter('term', zone=zone).filter('range', datetime={'lte': timestamp}).sort('-datetime')
            response = s.execute()
            if response.hits:
                return response.hits[0].carbon_intensity
            return None
        except Exception as e:
            print(f"ERROR: Failed to retrieve data from Elasticsearch for zone {zone}: {e}")
            return None

    def save_history(self, history_data: list, zone: str) -> int:
        """
        Saves a batch of historical carbon intensity data to Elasticsearch.
        Uses the bulk helper for efficient insertion.
        """
        actions = [
            {
                "_index": CarbonIntensityDoc._index._name,
                "_id": f"{zone}-{record.get('datetime')}",
                "_source": {
                    'zone': zone,
                    'carbon_intensity': record.get('carbonIntensity'),
                    'datetime': record.get('datetime'),
                    'updated_at': record.get('updatedAt'),
                    'created_at': record.get('createdAt'),
                    'emission_factor_type': record.get('emissionFactorType'),
                    'is_estimated': record.get('isEstimated'),
                    'estimation_method': record.get('estimationMethod')
                }
            }
            for record in history_data
        ]

        try:
            success, _ = bulk(connections.get_connection(), actions, raise_on_error=True)
            return success
        except Exception as e:
            print(f"ERROR: Failed to bulk save to Elasticsearch: {e}")
            return 0

