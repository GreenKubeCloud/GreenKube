# tests/collectors/test_node_collector.py

from src.greenkube.collectors.node_collector import NodeCollector

def test_get_zones_returns_unique_zones():
    """
    Tests that the get_zones method of the NodeCollector correctly
    deduplicates the list of zones.
    """
    # Arrange
    collector = NodeCollector()

    # Act
    unique_zones = collector.get_zones()

    # Assert
    # The hardcoded list is ["FR", "DE", "FR"]. The unique list should be ["FR", "DE"] or ["DE", "FR"].
    assert len(unique_zones) == 2
    assert "FR" in unique_zones
    assert "DE" in unique_zones
    # Verify that the conversion to a set and back to a list worked as expected
    assert isinstance(unique_zones, list)

