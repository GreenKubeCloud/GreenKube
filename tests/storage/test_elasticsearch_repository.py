import importlib
from unittest.mock import patch, MagicMock

# Mock the setup_connection function before it's called at module-level
# This prevents actual connection attempts during test discovery.
@patch('src.greenkube.storage.elasticsearch_repository.setup_connection', return_value=True)
def test_module_import(mock_setup):
    """
    This is not a real test but a holder for the initial module-level mock
    to prevent connection errors before tests run.
    """
    pass

@patch('src.greenkube.storage.elasticsearch_repository.config')
@patch('src.greenkube.storage.elasticsearch_repository.connections.create_connection')
def test_repository_initialization(mock_create_connection, mock_config, mocker):
    """ Tests that the repository initializes correctly. """
    # Mock the Document.init method
    mock_init = mocker.patch('src.greenkube.storage.elasticsearch_repository.CarbonIntensityDoc.init')
    
    # We need to reload the module for the top-level code to be re-evaluated
    # with our mocks in place.
    import src.greenkube.storage.elasticsearch_repository as es_repo
    importlib.reload(es_repo)

    # Now, instantiate the repository
    repo = es_repo.ElasticsearchCarbonIntensityRepository()
    
    # Assertions
    mock_init.assert_called_once()
    # Note: create_connection is called at the module level upon reload
    mock_create_connection.assert_called_once()


def test_get_for_zone_at_time(mocker):
    """
    Tests that get_for_zone_at_time correctly queries Elasticsearch
    and returns the right data.
    """
    # Arrange
    # Mock the entire search chain
    mock_search_obj = MagicMock()
    mock_filter_obj = MagicMock()
    mock_sort_obj = MagicMock()
    
    mocker.patch(
        'src.greenkube.storage.elasticsearch_repository.CarbonIntensityDoc.search', 
        return_value=mock_search_obj
    )
    mock_search_obj.filter.return_value = mock_filter_obj
    mock_filter_obj.filter.return_value = mock_filter_obj # Second filter call
    mock_filter_obj.sort.return_value = mock_sort_obj

    # Create a mock response with one hit
    mock_hit = MagicMock()
    mock_hit.carbon_intensity = 123.45
    mock_response = MagicMock()
    mock_response.hits = [mock_hit]
    mock_sort_obj.execute.return_value = mock_response

    # Act
    import src.greenkube.storage.elasticsearch_repository as es_repo
    repo = es_repo.ElasticsearchCarbonIntensityRepository()
    result = repo.get_for_zone_at_time('FR', '2023-10-27T10:00:00Z')

    # Assert
    assert result == 123.45
    es_repo.CarbonIntensityDoc.search.assert_called_once()
    mock_search_obj.filter.assert_called_with('term', zone='FR')
    mock_filter_obj.sort.assert_called_with('-datetime')
    mock_sort_obj.execute.assert_called_once()
    
    # Test case with no hits
    mock_response.hits = []
    mock_sort_obj.execute.return_value = mock_response
    result_none = repo.get_for_zone_at_time('FR', '2023-10-27T10:00:00Z')
    assert result_none is None

