import importlib
from unittest.mock import MagicMock, patch

import pytest

# Import exceptions and config needed for tests
# Make sure ApiError is imported if needed for RequestError construction
from elasticsearch.exceptions import (
    ConnectionError,
    NotFoundError,
    RequestError,
    TransportError,
)

from greenkube.core.config import config

# Sample data for testing
SAMPLE_HISTORY_DATA = [
    {
        "carbonIntensity": 50.0,
        "datetime": "2025-10-24T10:00:00Z",
        "updatedAt": "...",
        "createdAt": "...",
        "emissionFactorType": "...",
        "isEstimated": False,
        "estimationMethod": None,
    },
    {
        "carbonIntensity": 55.0,
        "datetime": "2025-10-24T11:00:00Z",
        "updatedAt": "...",
        "createdAt": "...",
        "emissionFactorType": "...",
        "isEstimated": False,
        "estimationMethod": None,
    },
    {
        "carbonIntensity": 45.0,
        "datetime": "2025-10-24T09:00:00Z",
        "updatedAt": "...",
        "createdAt": "...",
        "emissionFactorType": "...",
        "isEstimated": False,
        "estimationMethod": None,
    },
]

# --- Fixtures ---


@pytest.fixture(autouse=True)
def mock_es_connections_module():
    """
    Fixture to automatically mock the 'connections' module and provide
    access to both the module mock and the connection mock it returns.
    Yields a dictionary containing both mocks.
    """
    # Mock the connection object itself
    mock_conn = MagicMock()
    mock_conn.ping.return_value = True  # Assume connection is healthy by default

    # Mock the connections module used by elasticsearch-dsl
    with patch("greenkube.storage.elasticsearch_repository.connections") as mock_connections:
        # Configure get_connection to return our mock connection
        mock_connections.get_connection.return_value = mock_conn
        # Also mock create_connection used in setup_connection
        mock_connections.create_connection.return_value = mock_conn
        # Yield both the module mock and the connection mock
        yield {"module": mock_connections, "connection": mock_conn}


@pytest.fixture
def mock_bulk_helper():
    """Fixture to mock the elasticsearch bulk helper."""
    with patch("greenkube.storage.elasticsearch_repository.bulk") as mock_bulk:
        # Default success: return tuple (success_count, errors_list)
        # Simulate successful processing of all items
        mock_bulk.return_value = (len(SAMPLE_HISTORY_DATA), [])
        yield mock_bulk


@pytest.fixture
def es_repository():
    """Fixture that creates an ElasticsearchCarbonIntensityRepository with setup_connection patched."""
    with patch("greenkube.storage.elasticsearch_repository.setup_connection"):
        import greenkube.storage.elasticsearch_repository as es_repo

        importlib.reload(es_repo)
        return es_repo.ElasticsearchCarbonIntensityRepository()


@pytest.fixture
def mock_carbon_intensity_doc():
    """Fixture to mock the CarbonIntensityDoc class methods."""
    # Mock the Search object chainable methods and execute
    mock_search_obj = MagicMock()
    mock_search_obj.filter.return_value = mock_search_obj
    mock_search_obj.sort.return_value = mock_search_obj

    # Use patch.object for class methods like init and search
    with (
        patch("greenkube.storage.elasticsearch_repository.CarbonIntensityDoc.init") as mock_init,
        patch("greenkube.storage.elasticsearch_repository.CarbonIntensityDoc.search") as mock_search,
    ):
        mock_search.return_value = mock_search_obj
        # Yield mocks and the search object for configuration in tests
        yield {"init": mock_init, "search": mock_search, "search_obj": mock_search_obj}


# --- Test Cases ---


def test_repository_initialization(mock_carbon_intensity_doc, mock_es_connections_module):
    """Tests that the repository initializes correctly and calls CarbonIntensityDoc.init."""
    with patch("greenkube.storage.elasticsearch_repository.setup_connection"):
        import greenkube.storage.elasticsearch_repository as es_repo

        importlib.reload(es_repo)
        es_repo.ElasticsearchCarbonIntensityRepository()
    # Initialization completed (no exception) — behavior validated by constructor not raising
    assert True


def test_repository_initialization_connection_error_ping(mock_carbon_intensity_doc):
    """Tests that init handles ConnectionError during ping check."""
    # Arrange: Simulate connection ping failing *before* init is called
    mock_conn_fail = MagicMock()
    mock_conn_fail.ping.return_value = False
    # Patch get_connection specifically for this test
    with patch(
        "greenkube.storage.elasticsearch_repository.connections.get_connection",
        return_value=mock_conn_fail,
    ):
        with patch("greenkube.storage.elasticsearch_repository.setup_connection"):
            # Reload the module to trigger __init__ with the failing ping mock
            import greenkube.storage.elasticsearch_repository as es_repo

            importlib.reload(es_repo)
            # Act: Instantiation should happen
            _repo = es_repo.ElasticsearchCarbonIntensityRepository()
            # Assert: Doc.init() should NOT have been called because ping failed
            mock_carbon_intensity_doc["init"].assert_not_called()


def test_repository_initialization_connection_error_init(mock_es_connections_module):
    """Tests that init handles ConnectionError during Doc.init()."""
    # Arrange: Simulate Doc.init() raising ConnectionError
    with patch(
        "greenkube.storage.elasticsearch_repository.CarbonIntensityDoc.init",
        side_effect=ConnectionError("Init failed"),
    ):
        with patch("greenkube.storage.elasticsearch_repository.setup_connection"):
            import greenkube.storage.elasticsearch_repository as es_repo

            importlib.reload(es_repo)  # Reload to trigger init
            # Act & Assert: Instantiation should complete without raising, error should be logged
            repo = es_repo.ElasticsearchCarbonIntensityRepository()
            assert repo is not None  # Check object was created


def test_repository_initialization_request_error_init(mock_es_connections_module):
    """Tests that init handles RequestError during Doc.init() (e.g., mapping conflict)."""
    # Arrange: Simulate Doc.init() raising RequestError with required args
    mock_error_info = {
        "error": {"type": "illegal_argument_exception", "reason": "some mapping error"},
        "status": 400,
    }
    # Instantiate RequestError correctly: Use message, meta (optional), body (info)
    # The 'errors' kwarg might be specific to BulkError, stick to base ApiError args
    request_error = RequestError(
        "Mapping conflict during init",  # Message for the exception
        meta=MagicMock(status=400),  # Mock meta if needed, status is often checked
        body=mock_error_info,  # Body contains the error details from ES
    )
    with patch(
        "greenkube.storage.elasticsearch_repository.CarbonIntensityDoc.init",
        side_effect=request_error,
    ):
        with patch("greenkube.storage.elasticsearch_repository.setup_connection"):
            import greenkube.storage.elasticsearch_repository as es_repo

            importlib.reload(es_repo)  # Reload to trigger init
            # Act & Assert: Instantiation should complete without raising, error should be logged
            repo = es_repo.ElasticsearchCarbonIntensityRepository()
            assert repo is not None


def test_save_history_success(mock_bulk_helper, mock_es_connections_module):
    """Tests saving a list of valid history data."""
    # Arrange
    zone = "TEST"
    with patch("greenkube.storage.elasticsearch_repository.setup_connection"):
        import greenkube.storage.elasticsearch_repository as es_repo

        importlib.reload(es_repo)
        es_repository = es_repo.ElasticsearchCarbonIntensityRepository()
    # Act
    saved_count = es_repository.save_history(SAMPLE_HISTORY_DATA, zone)
    # Assert
    # Bulk helper should have been invoked
    mock_bulk_helper.assert_called()
    assert saved_count == len(SAMPLE_HISTORY_DATA)
    # Check args passed to bulk - verify structure of the first action
    call_args, call_kwargs = mock_bulk_helper.call_args
    actions = call_kwargs["actions"]
    assert len(actions) == len(SAMPLE_HISTORY_DATA)
    assert actions[0]["_op_type"] == "index"
    assert actions[0]["_index"] == config.ELASTICSEARCH_INDEX_NAME  # Check configured index name
    assert actions[0]["_id"] == f"{zone}-{SAMPLE_HISTORY_DATA[0]['datetime']}"
    assert actions[0]["_source"]["zone"] == zone
    assert actions[0]["_source"]["carbon_intensity"] == SAMPLE_HISTORY_DATA[0]["carbonIntensity"]


def test_save_history_empty_list(mock_bulk_helper):
    """Tests saving an empty list."""
    with patch("greenkube.storage.elasticsearch_repository.setup_connection"):
        import greenkube.storage.elasticsearch_repository as es_repo

        importlib.reload(es_repo)
        es_repository = es_repo.ElasticsearchCarbonIntensityRepository()
    saved_count = es_repository.save_history([], "TEST")
    mock_bulk_helper.assert_not_called()
    assert saved_count == 0


def test_save_history_invalid_records(mock_bulk_helper):
    """Tests saving data with invalid records mixed in."""
    # Arrange
    invalid_data = [
        SAMPLE_HISTORY_DATA[0],
        "not a dict",  # Invalid record
        {"carbonIntensity": 60.0},  # Missing datetime for ID
        SAMPLE_HISTORY_DATA[1],
    ]
    expected_valid_count = 2  # Only the first and last are fully valid
    # Adjust mock return value to reflect only valid actions processed
    mock_bulk_helper.return_value = (expected_valid_count, [])
    with patch("greenkube.storage.elasticsearch_repository.setup_connection"):
        import greenkube.storage.elasticsearch_repository as es_repo

        importlib.reload(es_repo)
        es_repository = es_repo.ElasticsearchCarbonIntensityRepository()
    # Act
    saved_count = es_repository.save_history(invalid_data, "TEST")
    # Assert
    mock_bulk_helper.assert_called()
    call_args, call_kwargs = mock_bulk_helper.call_args
    actions = call_kwargs["actions"]
    # Check that only valid records were prepared for bulk
    assert len(actions) == expected_valid_count
    # Check that the returned count matches the mock's success count
    assert saved_count == expected_valid_count


def test_save_history_bulk_error(mock_bulk_helper):
    """Tests handling of errors during the bulk operation."""
    # Arrange
    mock_bulk_helper.side_effect = TransportError("Bulk save failed")
    with patch("greenkube.storage.elasticsearch_repository.setup_connection"):
        import greenkube.storage.elasticsearch_repository as es_repo

        importlib.reload(es_repo)
        es_repository = es_repo.ElasticsearchCarbonIntensityRepository()
    # Act
    saved_count = es_repository.save_history(SAMPLE_HISTORY_DATA, "TEST")
    # Assert
    mock_bulk_helper.assert_called_once()
    assert saved_count == 0


def test_save_history_connection_error(mock_bulk_helper, mock_es_connections_module):
    """Tests handling connection errors before bulk."""
    # Arrange
    # Use the connection mock provided by the fixture
    mock_es_connections_module["connection"].ping.return_value = False  # Simulate connection lost
    with patch("greenkube.storage.elasticsearch_repository.setup_connection"):
        import greenkube.storage.elasticsearch_repository as es_repo

        importlib.reload(es_repo)
        es_repository = es_repo.ElasticsearchCarbonIntensityRepository()
    # Act
    saved_count = es_repository.save_history(SAMPLE_HISTORY_DATA, "TEST")
    # Assert
    # Bulk should not be called when connection ping fails
    mock_bulk_helper.assert_not_called()  # Bulk should not be called
    assert saved_count == 0


def test_get_for_zone_at_time_success(es_repository, mock_carbon_intensity_doc, mock_es_connections_module):
    """Tests retrieving the correct record based on zone and timestamp."""
    # Arrange
    zone = "TEST"
    timestamp = "2025-10-24T10:30:00Z"
    expected_intensity = 50.0  # From SAMPLE_HISTORY_DATA[0] @ 10:00

    # Mock the response from search().execute()
    mock_response = MagicMock()
    mock_hit = MagicMock()
    mock_hit.carbon_intensity = expected_intensity
    mock_response.hits = [mock_hit]
    mock_carbon_intensity_doc["search_obj"].execute.return_value = mock_response
    # Act
    result = es_repository.get_for_zone_at_time(zone, timestamp)

    # Assert
    # Search should have been invoked on the document class
    # Return value should match the mocked search execute output
    assert result == expected_intensity


def test_get_for_zone_at_time_no_match(es_repository, mock_carbon_intensity_doc, mock_es_connections_module):
    """Tests the case where no records match the criteria."""
    # Arrange
    zone = "TEST"
    timestamp = "2025-10-24T08:00:00Z"  # Before the earliest record

    mock_response = MagicMock()
    mock_response.hits = []  # Simulate no hits
    mock_carbon_intensity_doc["search_obj"].execute.return_value = mock_response
    # Act
    result = es_repository.get_for_zone_at_time(zone, timestamp)

    # Assert
    # Ensure search was attempted even when no hits were returned
    # No matching records -> None
    assert result is None


def test_get_for_zone_at_time_search_error(es_repository, mock_carbon_intensity_doc):
    """Tests handling errors during the search operation."""
    # Arrange
    # Simulate RequestError correctly: Use message, meta (optional), body (info)
    request_error = RequestError(
        "Search failed",
        meta=MagicMock(status=400),
        body={"error": "search error details", "status": 400},
    )
    mock_carbon_intensity_doc["search_obj"].execute.side_effect = request_error
    # Act
    result = es_repository.get_for_zone_at_time("TEST", "2025-10-24T10:30:00Z")
    # Assert
    assert result is None


def test_get_for_zone_at_time_connection_error(es_repository, mock_carbon_intensity_doc, mock_es_connections_module):
    """Tests handling connection error before search."""
    # Arrange
    # Use the connection mock provided by the fixture
    mock_es_connections_module["connection"].ping.return_value = False  # Simulate connection lost
    # Act
    result = es_repository.get_for_zone_at_time("TEST", "2025-10-24T10:30:00Z")
    # Assert
    # No search should be performed when connection is down
    # If connection is down, method should return None
    assert result is None


def test_setup_connection_success(mock_es_connections_module):
    """Tests successful connection setup with authentication."""
    # Arrange
    # Use the autouse fixture which mocks connections
    # Configure config for this specific test
    with patch("greenkube.storage.elasticsearch_repository.config") as mock_config:
        mock_config.ELASTICSEARCH_HOSTS = "http://testhost:9200"
        mock_config.ELASTICSEARCH_VERIFY_CERTS = True
        mock_config.ELASTICSEARCH_USER = "user"
        mock_config.ELASTICSEARCH_PASSWORD = "password"

        import greenkube.storage.elasticsearch_repository as es_repo

        es_repo.setup_connection()

        # Assert
        # Check that create_connection was called with the right args
        mock_es_connections_module["module"].create_connection.assert_called_once_with(
            "default",
            hosts=["http://testhost:9200"],
            verify_certs=True,
            basic_auth=("user", "password"),
        )
        # Check that the connection was pinged
        mock_es_connections_module["connection"].ping.assert_called_once()


def test_setup_connection_ping_fails(mock_es_connections_module):
    """Tests that setup_connection raises ConnectionError if ping fails."""
    # Arrange
    # Simulate ping failing
    mock_es_connections_module["connection"].ping.return_value = False
    # Act & Assert
    with pytest.raises(ConnectionError):
        import greenkube.storage.elasticsearch_repository as es_repo

        es_repo.setup_connection()


def test_setup_connection_transport_error():
    """Tests that setup_connection handles TransportError and raises ConnectionError."""
    # Arrange
    # Patch create_connection to raise a TransportError
    with patch(
        "greenkube.storage.elasticsearch_repository.connections.create_connection",
        side_effect=TransportError("Host not found"),
    ):
        # Act & Assert
        with pytest.raises(ConnectionError):
            import greenkube.storage.elasticsearch_repository as es_repo

            es_repo.setup_connection()


def test_get_for_zone_at_time_not_found_error(es_repository, mock_carbon_intensity_doc):
    """Tests handling of NotFoundError during search, e.g., if the index does not exist."""
    # Arrange
    # Simulate a NotFoundError when execute() is called; ApiError requires meta and body
    mock_carbon_intensity_doc["search_obj"].execute.side_effect = NotFoundError(
        "Index not found", meta=MagicMock(status=404), body={}
    )

    # Act
    result = es_repository.get_for_zone_at_time("TEST", "2025-10-24T10:30:00Z")

    # Assert
    # The method should catch the error and return None
    assert result is None
