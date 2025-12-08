# src/greenkube/core/factory.py
"""
Factory functions to instantiate core components like the DataProcessor
and the correct CarbonIntensityRepository.
"""

import logging
import os
import traceback
from functools import lru_cache

import typer

# --- GreenKube Collector Imports ---
from ..collectors.electricity_maps_collector import ElectricityMapsCollector
from ..collectors.node_collector import NodeCollector
from ..collectors.opencost_collector import OpenCostCollector
from ..collectors.pod_collector import PodCollector
from ..collectors.prometheus_collector import PrometheusCollector
from ..core.calculator import CarbonCalculator
from ..core.config import config
from ..core.processor import DataProcessor

# --- GreenKube Core Imports ---
from ..energy.estimator import BasicEstimator

# --- GreenKube Storage Imports ---
from ..storage.base_repository import CarbonIntensityRepository, NodeRepository
from ..storage.elasticsearch_repository import (
    ElasticsearchCarbonIntensityRepository,
)
from ..storage.postgres_node_repository import PostgresNodeRepository
from ..storage.postgres_repository import PostgresCarbonIntensityRepository
from ..storage.sqlite_node_repository import SQLiteNodeRepository
from ..storage.sqlite_repository import SQLiteCarbonIntensityRepository

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_repository() -> CarbonIntensityRepository:
    """
    Factory function to get the appropriate repository based on config.
    Uses lru_cache to act as a singleton.
    """
    # Allow tests to override DB_TYPE via environment variables (monkeypatch).
    db_type = os.getenv("DB_TYPE", config.DB_TYPE)

    if db_type == "elasticsearch":
        logger.info("Using Elasticsearch repository.")
        try:
            from ..storage import elasticsearch_repository as es_mod

            es_mod.setup_connection()
        except Exception as e:
            logger.error(f"Failed to setup Elasticsearch connection: {e}")
        return ElasticsearchCarbonIntensityRepository()
    elif db_type == "sqlite":
        logger.info("Using SQLite repository.")
        from ..core.db import db_manager

        return SQLiteCarbonIntensityRepository(db_manager.get_connection())
    elif db_type == "postgres":
        logger.info("Using PostgreSQL repository.")
        from ..core.db import db_manager

        return PostgresCarbonIntensityRepository(db_manager.get_connection())
    else:
        raise NotImplementedError(f"Repository for DB_TYPE '{config.DB_TYPE}' not implemented.")


@lru_cache(maxsize=1)
def get_node_repository() -> NodeRepository:
    """
    Factory function to get the node repository.
    """
    if config.DB_TYPE == "sqlite":
        from ..core.db import db_manager

        return SQLiteNodeRepository(db_manager.get_connection())
    elif config.DB_TYPE == "elasticsearch":
        from ..storage.elasticsearch_node_repository import ElasticsearchNodeRepository

        return ElasticsearchNodeRepository()
    elif config.DB_TYPE == "postgres":
        from ..core.db import db_manager

        return PostgresNodeRepository(db_manager.get_connection())
    else:
        # For now, only SQLite and Elasticsearch are supported for nodes
        logger.warning(
            f"NodeRepository not implemented for DB_TYPE '{config.DB_TYPE}'. "
            "Using SQLite fallback if possible or failing."
        )
        from ..core.db import db_manager

        return SQLiteNodeRepository(db_manager.get_connection())


@lru_cache(maxsize=1)
def get_processor() -> DataProcessor:
    """
    Factory function to instantiate and return a fully configured DataProcessor.
    Uses lru_cache to act as a singleton.
    """
    logger.info("Initializing data collectors and processor...")
    try:
        # 1. Get the repository
        repository = get_repository()
        node_repository = get_node_repository()

        # 2. Instantiate all collectors
        prometheus_collector = PrometheusCollector()
        opencost_collector = OpenCostCollector()
        node_collector = NodeCollector()
        pod_collector = PodCollector()
        electricity_maps_collector = ElectricityMapsCollector()

        # 3. Instantiate Calculator and Estimator
        calculator = CarbonCalculator()
        estimator = BasicEstimator(config)

        # 4. Instantiate and return DataProcessor
        return DataProcessor(
            prometheus_collector=prometheus_collector,
            opencost_collector=opencost_collector,
            node_collector=node_collector,
            pod_collector=pod_collector,
            electricity_maps_collector=electricity_maps_collector,
            repository=repository,
            node_repository=node_repository,
            calculator=calculator,
            estimator=estimator,
        )
    except Exception as e:
        logger.error(f"An error occurred during processor initialization: {e}")
        logger.error("Processor initialization failed: %s", traceback.format_exc())
        raise typer.Exit(code=1)
