# tests/core/test_factory_coverage.py
"""
Tests for factory.py — verifies that all factory functions return the correct
repository type when DB_TYPE=sqlite (the test environment default).

The factory uses lru_cache, so clear_caches() must be called between tests
to ensure each test gets a fresh instance (handled by conftest.py autouse).
"""

import pytest
import typer

from greenkube.core import factory
from greenkube.core.factory import (
    clear_caches,
    get_combined_metrics_repository,
    get_embodied_repository,
    get_node_repository,
    get_processor,
    get_recommendation_repository,
    get_repository,
    get_savings_ledger_repository,
    get_summary_repository,
    get_timeseries_cache_repository,
)

# The conftest.py autouse fixture already sets DB_TYPE=sqlite and calls clear_caches()
# between tests, so we can call factory functions directly.


def _cached_stub(return_value=None, side_effect=None):
    def _stub():
        if side_effect is not None:
            raise side_effect
        return return_value

    _stub.cache_clear = lambda: None
    return _stub


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

    def test_unknown_combined_metrics_repository_raises(self, monkeypatch):
        from greenkube.core import config as config_module

        monkeypatch.setattr(config_module.config, "DB_TYPE", "unknown_backend")
        clear_caches()

        with pytest.raises(NotImplementedError):
            get_combined_metrics_repository()

        clear_caches()

    def test_unknown_backend_uses_sqlite_fallbacks(self, monkeypatch):
        from greenkube.core import config as config_module
        from greenkube.storage.sqlite.node_repository import SQLiteNodeRepository
        from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository
        from greenkube.storage.sqlite.summary_repository import SQLiteSummaryRepository
        from greenkube.storage.sqlite.timeseries_cache_repository import SQLiteTimeseriesCacheRepository

        monkeypatch.setattr(config_module.config, "DB_TYPE", "unknown_backend")
        clear_caches()

        assert isinstance(get_node_repository(), SQLiteNodeRepository)
        assert isinstance(get_recommendation_repository(), SQLiteRecommendationRepository)
        assert isinstance(get_summary_repository(), SQLiteSummaryRepository)
        assert isinstance(get_timeseries_cache_repository(), SQLiteTimeseriesCacheRepository)

        clear_caches()


class TestFactoryPostgres:
    def test_postgres_factories_return_postgres_implementations(self, monkeypatch):
        from greenkube.core import config as config_module
        from greenkube.storage.embodied_repository import EmbodiedRepository, PostgresEmbodiedRepository
        from greenkube.storage.postgres.node_repository import PostgresNodeRepository
        from greenkube.storage.postgres.recommendation_repository import PostgresRecommendationRepository
        from greenkube.storage.postgres.repository import (
            PostgresCarbonIntensityRepository,
            PostgresCombinedMetricsRepository,
        )
        from greenkube.storage.postgres.savings_repository import PostgresSavingsLedgerRepository
        from greenkube.storage.postgres.summary_repository import PostgresSummaryRepository
        from greenkube.storage.postgres.timeseries_cache_repository import PostgresTimeseriesCacheRepository

        monkeypatch.setattr(config_module.config, "DB_TYPE", "postgres")
        clear_caches()

        assert isinstance(get_repository(), PostgresCarbonIntensityRepository)
        assert isinstance(get_combined_metrics_repository(), PostgresCombinedMetricsRepository)
        assert isinstance(get_node_repository(), PostgresNodeRepository)
        assert isinstance(get_recommendation_repository(), PostgresRecommendationRepository)
        assert isinstance(get_savings_ledger_repository(), PostgresSavingsLedgerRepository)
        assert isinstance(get_summary_repository(), PostgresSummaryRepository)
        assert isinstance(get_timeseries_cache_repository(), PostgresTimeseriesCacheRepository)

        embodied = get_embodied_repository()
        assert isinstance(embodied, EmbodiedRepository)
        assert isinstance(embodied._impl, PostgresEmbodiedRepository)

        clear_caches()


class TestFactoryProcessor:
    def test_get_processor_constructs_data_processor(self, monkeypatch):
        components = {
            "repository": object(),
            "combined_metrics_repository": object(),
            "node_repository": object(),
            "embodied_repository": object(),
        }
        processor = object()

        monkeypatch.setattr(factory, "get_repository", _cached_stub(components["repository"]))
        monkeypatch.setattr(
            factory,
            "get_combined_metrics_repository",
            _cached_stub(components["combined_metrics_repository"]),
        )
        monkeypatch.setattr(factory, "get_node_repository", _cached_stub(components["node_repository"]))
        monkeypatch.setattr(factory, "get_embodied_repository", _cached_stub(components["embodied_repository"]))
        monkeypatch.setattr(factory, "PrometheusCollector", lambda cfg: "prometheus")
        monkeypatch.setattr(factory, "OpenCostCollector", lambda: "opencost")
        monkeypatch.setattr(factory, "NodeCollector", lambda: "node")
        monkeypatch.setattr(factory, "PodCollector", lambda: "pod")
        monkeypatch.setattr(factory, "ElectricityMapsCollector", lambda: "electricity")
        monkeypatch.setattr(factory, "BoaviztaCollector", lambda: "boavizta")
        monkeypatch.setattr(factory, "CarbonCalculator", lambda repository, config: "calculator")
        monkeypatch.setattr(factory, "BasicEstimator", lambda cfg: "estimator")
        monkeypatch.setattr(factory, "DataProcessor", lambda **kwargs: processor)
        clear_caches()

        assert get_processor() is processor

        clear_caches()

    def test_get_processor_clears_caches_and_exits_on_error(self, monkeypatch):
        monkeypatch.setattr(factory, "get_repository", _cached_stub(side_effect=RuntimeError("boom")))
        clear_caches()

        with pytest.raises(typer.Exit) as exc_info:
            get_processor()

        assert exc_info.value.exit_code == 1

        clear_caches()
