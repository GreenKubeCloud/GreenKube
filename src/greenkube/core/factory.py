# src/greenkube/core/factory.py
"""
Factory functions to instantiate core components like the DataProcessor
and the correct CarbonIntensityRepository.
"""

import logging
import traceback
from functools import lru_cache

import typer

# --- GreenKube Collector Imports ---
from ..collectors.boavizta_collector import BoaviztaCollector
from ..collectors.electricity_maps_collector import ElectricityMapsCollector
from ..collectors.node_collector import NodeCollector
from ..collectors.opencost_collector import OpenCostCollector
from ..collectors.pod_collector import PodCollector
from ..collectors.prometheus_collector import PrometheusCollector
from ..core.calculator import CarbonCalculator
from ..core.config import get_config
from ..core.processor import DataProcessor

# --- GreenKube Core Imports ---
from ..energy.estimator import BasicEstimator

# --- GreenKube Storage Imports ---
from ..storage.base_repository import (
    CarbonIntensityRepository,
    CombinedMetricsRepository,
    NodeRepository,
    RecommendationRepository,
)
from ..storage.elasticsearch_repository import (
    ElasticsearchCarbonIntensityRepository,
    ElasticsearchCombinedMetricsRepository,
)
from ..storage.embodied_repository import EmbodiedRepository
from ..storage.postgres_node_repository import PostgresNodeRepository
from ..storage.postgres_repository import PostgresCarbonIntensityRepository, PostgresCombinedMetricsRepository
from ..storage.sqlite_node_repository import SQLiteNodeRepository
from ..storage.sqlite_repository import SQLiteCarbonIntensityRepository, SQLiteCombinedMetricsRepository

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_repository() -> CarbonIntensityRepository:
    """
    Factory function to get the appropriate repository based on config.
    Uses lru_cache to act as a singleton.
    """
    cfg = get_config()
    db_type = cfg.DB_TYPE

    if db_type == "elasticsearch":
        logger.info("Using Elasticsearch repository.")
        return ElasticsearchCarbonIntensityRepository()
    elif db_type == "sqlite":
        logger.info("Using SQLite repository.")
        from ..core.db import get_db_manager

        return SQLiteCarbonIntensityRepository(get_db_manager())
    elif db_type == "postgres":
        logger.info("Using PostgreSQL repository.")
        from ..core.db import get_db_manager

        return PostgresCarbonIntensityRepository(get_db_manager())
    else:
        raise NotImplementedError(f"Repository for DB_TYPE '{db_type}' not implemented.")


@lru_cache(maxsize=1)
def get_combined_metrics_repository() -> CombinedMetricsRepository:
    """
    Factory function to get the appropriate combined metrics repository based on config.
    Uses lru_cache to act as a singleton.
    """
    cfg = get_config()
    db_type = cfg.DB_TYPE

    if db_type == "elasticsearch":
        logger.info("Using Elasticsearch combined metrics repository.")
        return ElasticsearchCombinedMetricsRepository()
    elif db_type == "sqlite":
        logger.info("Using SQLite combined metrics repository.")
        from ..core.db import get_db_manager

        return SQLiteCombinedMetricsRepository(get_db_manager())
    elif db_type == "postgres":
        logger.info("Using PostgreSQL combined metrics repository.")
        from ..core.db import get_db_manager

        return PostgresCombinedMetricsRepository(get_db_manager())
    else:
        raise NotImplementedError(f"CombinedMetricsRepository for DB_TYPE '{db_type}' not implemented.")


@lru_cache(maxsize=1)
def get_node_repository() -> NodeRepository:
    """
    Factory function to get the node repository.
    """
    cfg = get_config()
    if cfg.DB_TYPE == "sqlite":
        from ..core.db import get_db_manager

        return SQLiteNodeRepository(get_db_manager())
    elif cfg.DB_TYPE == "elasticsearch":
        from ..storage.elasticsearch_node_repository import ElasticsearchNodeRepository

        return ElasticsearchNodeRepository()
    elif cfg.DB_TYPE == "postgres":
        from ..core.db import get_db_manager

        return PostgresNodeRepository(get_db_manager())
    else:
        logger.warning(
            "NodeRepository not implemented for DB_TYPE '%s'. Using SQLite fallback if possible or failing.",
            cfg.DB_TYPE,
        )
        from ..core.db import get_db_manager

        return SQLiteNodeRepository(get_db_manager())


@lru_cache(maxsize=1)
def get_embodied_repository() -> EmbodiedRepository:
    """
    Factory function to get the embodied emissions repository.
    """
    from ..core.db import get_db_manager

    return EmbodiedRepository(get_db_manager())


@lru_cache(maxsize=1)
def get_recommendation_repository() -> RecommendationRepository:
    """
    Factory function to get the recommendation history repository.
    Uses lru_cache to act as a singleton.
    """
    cfg = get_config()
    db_type = cfg.DB_TYPE

    if db_type == "sqlite":
        from ..core.db import get_db_manager
        from ..storage.sqlite_recommendation_repository import SQLiteRecommendationRepository

        return SQLiteRecommendationRepository(get_db_manager())
    elif db_type == "postgres":
        from ..core.db import get_db_manager
        from ..storage.postgres_recommendation_repository import PostgresRecommendationRepository

        return PostgresRecommendationRepository(get_db_manager())
    else:
        logger.warning(
            "RecommendationRepository not implemented for DB_TYPE '%s'. Using SQLite fallback.",
            db_type,
        )
        from ..core.db import get_db_manager
        from ..storage.sqlite_recommendation_repository import SQLiteRecommendationRepository

        return SQLiteRecommendationRepository(get_db_manager())


@lru_cache(maxsize=1)
def get_processor() -> DataProcessor:
    """
    Factory function to instantiate and return a fully configured DataProcessor.
    Uses lru_cache to act as a singleton.
    """
    cfg = get_config()
    logger.info("Initializing data collectors and processor...")
    try:
        # 1. Get the repository
        repository = get_repository()
        combined_metrics_repository = get_combined_metrics_repository()
        node_repository = get_node_repository()
        embodied_repository = get_embodied_repository()

        # 2. Instantiate all collectors
        prometheus_collector = PrometheusCollector(cfg)
        opencost_collector = OpenCostCollector()
        node_collector = NodeCollector()
        pod_collector = PodCollector()
        electricity_maps_collector = ElectricityMapsCollector()
        boavizta_collector = BoaviztaCollector()

        # 3. Instantiate Calculator and Estimator
        calculator = CarbonCalculator(repository, config=cfg)
        estimator = BasicEstimator(cfg)

        # 4. Instantiate and return DataProcessor
        return DataProcessor(
            prometheus_collector=prometheus_collector,
            opencost_collector=opencost_collector,
            node_collector=node_collector,
            pod_collector=pod_collector,
            electricity_maps_collector=electricity_maps_collector,
            boavizta_collector=boavizta_collector,
            repository=repository,
            combined_metrics_repository=combined_metrics_repository,
            node_repository=node_repository,
            embodied_repository=embodied_repository,
            calculator=calculator,
            estimator=estimator,
            config=cfg,
        )
    except Exception as e:
        logger.error("An error occurred during processor initialization: %s", e)
        logger.error("Processor initialization failed: %s", traceback.format_exc())
        clear_caches()
        raise typer.Exit(code=1)


def clear_caches():
    """Clear all factory function caches.

    This is primarily useful in tests to ensure a fresh set of components
    after configuration changes. It is also called automatically by
    :func:`get_processor` when initialization fails, so that a subsequent
    call can retry cleanly.
    """
    get_repository.cache_clear()
    get_combined_metrics_repository.cache_clear()
    get_node_repository.cache_clear()
    get_embodied_repository.cache_clear()
    get_recommendation_repository.cache_clear()
    get_processor.cache_clear()
