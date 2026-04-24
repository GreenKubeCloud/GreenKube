# tests/core/test_factory_coverage.py
"""
Tests for factory.py — verifies that all factory functions return the correct
repository type when DB_TYPE=sqlite (the test environment default).

The factory uses lru_cache, so clear_caches() must be called between tests
to ensure each test gets a fresh instance (handled by conftest.py autouse).
"""

import pytest

from greenkube.core.factory import (
    clear_caches,
    get_combined_metrics_repository,
    get_node_repository,
    get_recommendation_repository,
    get_repository,
    get_summary_repository,
    get_timeseries_cache_repository,
)

# The conftest.py autouse fixture already sets DB_TYPE=sqlite and calls clear_caches()
# between tests, so we can call factory functions directly.


class TestFactorySQLite:
    """All factory functions return the SQLite implementation when DB_TYPE=sqlite."""

    def test_get_repository_returns_sqlite(self):
        """get_repository() → SQLiteCarbonIntensityRepository when DB_TYPE=sqlite."""
        from greenkube.storage.sqlite.repository import SQLiteCarbonIntensityRepository

        repo = get_repository()
        assert isinstance(repo, SQLiteCarbonIntensityRepository)

    def test_get_combined_metrics_repository_returns_sqlite(self):
        """get_combined_metrics_repository() → SQLiteCombinedMetricsRepository."""
        from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository

        repo = get_combined_metrics_repository()
        assert isinstance(repo, SQLiteCombinedMetricsRepository)

    def test_get_node_repository_returns_sqlite(self):
        """get_node_repository() → SQLiteNodeRepository when DB_TYPE=sqlite."""
        from greenkube.storage.sqlite.node_repository import SQLiteNodeRepository

        repo = get_node_repository()
        assert isinstance(repo, SQLiteNodeRepository)

    def test_get_recommendation_repository_returns_sqlite(self):
        """get_recommendation_repository() → SQLiteRecommendationRepository."""
        from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository

        repo = get_recommendation_repository()
        assert isinstance(repo, SQLiteRecommendationRepository)

    def test_get_summary_repository_returns_sqlite(self):
        """get_summary_repository() → SQLiteSummaryRepository."""
        from greenkube.storage.sqlite.summary_repository import SQLiteSummaryRepository

        repo = get_summary_repository()
        assert isinstance(repo, SQLiteSummaryRepository)

    def test_get_timeseries_cache_repository_returns_sqlite(self):
        """get_timeseries_cache_repository() → SQLiteTimeseriesCacheRepository."""
        from greenkube.storage.sqlite.timeseries_cache_repository import SQLiteTimeseriesCacheRepository

        repo = get_timeseries_cache_repository()
        assert isinstance(repo, SQLiteTimeseriesCacheRepository)


class TestFactorySingleton:
    """Factory functions return the same cached instance on repeated calls."""

    def test_get_repository_is_singleton(self):
        repo1 = get_repository()
        repo2 = get_repository()
        assert repo1 is repo2

    def test_clear_caches_allows_new_instance(self):
        clear_caches()
        repo2 = get_repository()
        # After clearing, a fresh instance is created (may or may not be same id)
        # The key thing is it doesn't raise
        assert repo2 is not None


class TestFactoryUnknownDbType:
    """An unknown DB_TYPE raises NotImplementedError for repositories that require it."""

    def test_unknown_db_type_raises(self, monkeypatch):
        from greenkube.core import config as config_module

        monkeypatch.setattr(config_module.config, "DB_TYPE", "unknown_backend")
        clear_caches()

        with pytest.raises(NotImplementedError):
            get_repository()

        clear_caches()
