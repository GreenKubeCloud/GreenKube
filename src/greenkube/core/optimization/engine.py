# src/greenkube/core/optimization/engine.py
"""The unified optimization engine.

Single orchestration path for recommendation generation and persistence. API,
startup scan and CLI all delegate here; only the CLI skips persistence.
"""

import logging
from typing import TYPE_CHECKING, List, Optional, Sequence

from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.context_builder import ContextBuilder
from greenkube.core.optimization.dedup import finalize_recommendations
from greenkube.core.optimization.enrich import enrich_recommendations
from greenkube.core.optimization.providers.base import RecommendationSource
from greenkube.core.optimization.registry import build_sources
from greenkube.models.metrics import Recommendation, RecommendationRecord

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.storage.base_repository import CombinedMetricsRepository, NodeRepository, RecommendationRepository

logger = logging.getLogger(__name__)


class OptimizationEngine:
    """Runs recommendation sources, finalizes their output and persists it."""

    def __init__(
        self,
        config: Optional["Config"] = None,
        sources: Optional[Sequence[RecommendationSource]] = None,
    ):
        from greenkube.core.config import get_config

        self.config = config if config is not None else get_config()
        if sources is None:
            sources = build_sources(self.config)
        self.sources: List[RecommendationSource] = list(sources)

    async def generate(self, context: OptimizationContext) -> List[Recommendation]:
        """Runs every available source and returns deduplicated recommendations."""
        recs: List[Recommendation] = []
        for source in self.sources:
            source_name = getattr(source, "name", repr(source))
            try:
                if not await source.is_available():
                    logger.debug("Recommendation source '%s' is not available; skipping.", source_name)
                    continue
                recs.extend(await source.collect(context))
            except Exception:
                logger.exception("Recommendation source '%s' failed; continuing without it.", source_name)
        finalized = finalize_recommendations(recs, self.config)
        return enrich_recommendations(finalized, context, self.config)

    async def build_context(
        self,
        combined_repo: "CombinedMetricsRepository",
        node_repo: "NodeRepository",
        namespace: Optional[str] = None,
    ) -> OptimizationContext:
        """Builds the context from the configured lookback window."""
        return await ContextBuilder(self.config).build(combined_repo, node_repo, namespace=namespace)

    async def refresh(
        self,
        combined_repo: "CombinedMetricsRepository",
        node_repo: "NodeRepository",
        reco_repo: "RecommendationRepository",
        namespace: Optional[str] = None,
    ) -> List[Recommendation]:
        """Generates recommendations and reconciles persisted active records."""
        context = await self.build_context(combined_repo, node_repo, namespace=namespace)
        recommendations = await self.generate(context)
        await self.persist(recommendations, reco_repo, namespace=namespace)
        return recommendations

    async def persist(
        self,
        recommendations: List[Recommendation],
        reco_repo: "RecommendationRepository",
        namespace: Optional[str] = None,
    ) -> None:
        """Upserts generated recommendations and marks absent actives as stale."""
        try:
            records = [RecommendationRecord.from_recommendation(r) for r in recommendations]
            if records:
                await reco_repo.upsert_recommendations(records)
            await reco_repo.reconcile_active_recommendations(records, namespace=namespace)
        except Exception as e:
            logger.error("Failed to upsert recommendation history: %s", e)
