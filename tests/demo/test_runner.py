# tests/demo/test_runner.py
"""
Unit tests for the demo runner module.
Tests configuration setup and database population without starting the server.
"""

import os
from unittest.mock import AsyncMock, patch

import pytest

from greenkube.demo.runner import _configure_demo_environment, _populate_database


class TestConfigureDemoEnvironment:
    """Tests for _configure_demo_environment."""

    def test_sets_sqlite_db_type(self, monkeypatch):
        """Demo mode must use SQLite."""
        from greenkube.core.config import config

        db_path = "/tmp/test-demo.db"
        _configure_demo_environment(db_path, port=8000)

        assert os.environ["DB_TYPE"] == "sqlite"
        assert config.DB_TYPE == "sqlite"

    def test_sets_db_path(self, monkeypatch):
        """Demo DB path should match the provided path."""
        db_path = "/tmp/test-demo-custom.db"
        _configure_demo_environment(db_path, port=9000)

        assert os.environ["DB_PATH"] == db_path
        # After _configure_demo_environment, config has been reloaded
        # so verify via env var directly (autouse fixture may re-override later).
        assert os.environ.get("DB_PATH") == db_path

    def test_sets_api_port(self, monkeypatch):
        """API port should be configurable."""
        _configure_demo_environment("/tmp/test.db", port=9999)

        assert os.environ["API_PORT"] == "9999"

    def test_disables_api_key(self, monkeypatch):
        """Demo mode should have no API key."""
        _configure_demo_environment("/tmp/test.db", port=8000)

        assert os.environ["GREENKUBE_API_KEY"] == ""

    def test_sets_localhost(self, monkeypatch):
        """Demo should bind to localhost only when browser is enabled."""
        _configure_demo_environment("/tmp/test.db", port=8000)

        assert os.environ["API_HOST"] == "127.0.0.1"

    def test_sets_all_interfaces_when_no_browser(self, monkeypatch):
        """Demo should bind to 0.0.0.0 when --no-browser is used (K8s pod)."""
        _configure_demo_environment("/tmp/test.db", port=9000, no_browser=True)

        assert os.environ["API_HOST"] == "0.0.0.0"


class TestPopulateDatabase:
    """Tests for _populate_database."""

    @pytest.mark.asyncio
    async def test_populates_all_tables(self, monkeypatch):
        """Database population should return counts for all 4 categories."""
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", ":memory:")

        from greenkube.core.config import config
        from greenkube.core.factory import clear_caches

        config.reload()
        clear_caches()

        mock_repo = AsyncMock()
        mock_repo.save_history = AsyncMock(return_value=48)

        mock_node_repo = AsyncMock()
        mock_node_repo.save_nodes = AsyncMock(return_value=6)

        mock_combined_repo = AsyncMock()
        mock_combined_repo.write_combined_metrics = AsyncMock(return_value=500)

        mock_reco_repo = AsyncMock()
        mock_reco_repo.save_recommendations = AsyncMock(return_value=8)

        mock_db_manager = AsyncMock()
        mock_db_manager.connect = AsyncMock()

        mock_compressor = AsyncMock()
        mock_compressor.run = AsyncMock(return_value={"rows_compressed": 400, "hours_compressed": 400})

        mock_refresher = AsyncMock()
        mock_refresher.run = AsyncMock(return_value=30)

        with (
            patch("greenkube.core.factory.get_repository", return_value=mock_repo),
            patch("greenkube.core.factory.get_node_repository", return_value=mock_node_repo),
            patch("greenkube.core.factory.get_combined_metrics_repository", return_value=mock_combined_repo),
            patch("greenkube.core.factory.get_recommendation_repository", return_value=mock_reco_repo),
            patch("greenkube.core.db.db_manager", mock_db_manager),
            patch("greenkube.core.metrics_compressor.MetricsCompressor", return_value=mock_compressor),
            patch("greenkube.core.summary_refresher.SummaryRefresher", return_value=mock_refresher),
        ):
            counts = await _populate_database(days=2)

        assert "carbon_intensity" in counts
        assert "node_snapshots" in counts
        assert "combined_metrics" in counts
        assert "recommendations" in counts
        assert "hourly_compressed" in counts
        assert "timeseries_cache_rows" in counts

    @pytest.mark.asyncio
    async def test_populates_with_positive_counts(self, monkeypatch):
        """All populated categories should have positive record counts."""
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", ":memory:")

        from greenkube.core.config import config
        from greenkube.core.factory import clear_caches

        config.reload()
        clear_caches()

        mock_repo = AsyncMock()
        mock_repo.save_history = AsyncMock(return_value=48)

        mock_node_repo = AsyncMock()
        mock_node_repo.save_nodes = AsyncMock(return_value=6)

        mock_combined_repo = AsyncMock()
        mock_combined_repo.write_combined_metrics = AsyncMock(return_value=500)

        mock_reco_repo = AsyncMock()
        mock_reco_repo.save_recommendations = AsyncMock(return_value=8)

        mock_db_manager = AsyncMock()
        mock_db_manager.connect = AsyncMock()

        mock_compressor = AsyncMock()
        mock_compressor.run = AsyncMock(return_value={"rows_compressed": 400, "hours_compressed": 400})

        mock_refresher = AsyncMock()
        mock_refresher.run = AsyncMock(return_value=30)

        with (
            patch("greenkube.core.factory.get_repository", return_value=mock_repo),
            patch("greenkube.core.factory.get_node_repository", return_value=mock_node_repo),
            patch("greenkube.core.factory.get_combined_metrics_repository", return_value=mock_combined_repo),
            patch("greenkube.core.factory.get_recommendation_repository", return_value=mock_reco_repo),
            patch("greenkube.core.db.db_manager", mock_db_manager),
            patch("greenkube.core.metrics_compressor.MetricsCompressor", return_value=mock_compressor),
            patch("greenkube.core.summary_refresher.SummaryRefresher", return_value=mock_refresher),
        ):
            counts = await _populate_database(days=2)

        for category, count in counts.items():
            assert count > 0, f"Expected positive count for {category}, got {count}"
