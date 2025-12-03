# tests/core/test_factory.py

from unittest.mock import MagicMock, patch

from greenkube.core.factory import get_node_repository
from greenkube.storage.node_repository import NodeRepository


def test_get_node_repository_sqlite(monkeypatch):
    """Test that get_node_repository returns a NodeRepository when DB_TYPE is sqlite."""
    monkeypatch.setenv("DB_TYPE", "sqlite")

    # Mock db_manager to avoid actual DB connection
    with patch("greenkube.core.db.db_manager") as mock_db_manager:
        mock_db_manager.get_connection.return_value = MagicMock()

        repo = get_node_repository()
        assert isinstance(repo, NodeRepository)


def test_get_node_repository_singleton(monkeypatch):
    """Test that get_node_repository returns the same instance (singleton behavior)."""
    monkeypatch.setenv("DB_TYPE", "sqlite")

    with patch("greenkube.core.db.db_manager") as mock_db_manager:
        mock_db_manager.get_connection.return_value = MagicMock()

        repo1 = get_node_repository()
        repo2 = get_node_repository()
        assert repo1 is repo2
