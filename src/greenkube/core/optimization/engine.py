# src/greenkube/core/optimization/engine.py
"""The unified optimization engine.

Single orchestration path for recommendation generation and persistence. API,
startup scan and CLI all delegate here; only the CLI skips persistence.
"""

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional, Sequence

from greenkube.core.optimization.actions import build_action_plans
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.context_builder import ContextBuilder
from greenkube.core.optimization.dedup import finalize_recommendations
from greenkube.core.optimization.enrich import enrich_recommendations
from greenkube.core.optimization.evidence import build_patch
from greenkube.core.optimization.providers.base import RecommendationSource
from greenkube.core.optimization.registry import build_sources
from greenkube.models.metrics import Recommendation, RecommendationPatchV2, RecommendationRecord, RecommendationType

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
        run_repository=None,
    ):
        from greenkube.core.config import get_config

        self.config = config if config is not None else get_config()
        if sources is None:
            sources = build_sources(self.config)
        self.sources: List[RecommendationSource] = list(sources)
        self.run_repository = run_repository
        self.failed_sources: List[str] = []

    async def generate(self, context: OptimizationContext) -> List[Recommendation]:
        """Runs every available source and returns deduplicated recommendations."""
        recs: List[Recommendation] = []
        self.failed_sources = []
        for source in self.sources:
            source_name = getattr(source, "name", repr(source))
            try:
                if not await source.is_available():
                    logger.debug("Recommendation source '%s' is not available; skipping.", source_name)
                    continue
                recs.extend(await source.collect(context))
            except Exception:
                logger.exception("Recommendation source '%s' failed; continuing without it.", source_name)
                self.failed_sources.append(source_name)
                from greenkube.core.observability import record_analyzer_failure

                record_analyzer_failure(source_name)
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
        from greenkube.core.observability import (
            elapsed,
            record_recommendation_scan_resources,
            record_run,
            start_timer,
        )

        record_recommendation_scan_resources(
            context.analysis_window_seconds or 0.0,
            len(context.metrics) + len(context.container_metrics),
            _peak_resident_memory_bytes(),
        )

        timer = start_timer()
        run = None
        run_repository = self.run_repository
        if run_repository is not None:
            from greenkube.models.optimization_run import OptimizationRun

            run = await run_repository.create(
                OptimizationRun(
                    started_at=datetime.now(timezone.utc),
                    namespace=namespace,
                    analyzer_count=len(self.sources),
                )
            )
        try:
            recommendations = await self.generate(context)
            # A partial run must not mark previously valid recommendations stale.
            if not self.failed_sources:
                recommendations = await self.persist(recommendations, reco_repo, namespace=namespace)
            if run is not None and run_repository is not None:
                await run_repository.complete(
                    run.id,
                    status="succeeded" if not self.failed_sources else "failed",
                    completed_at=datetime.now(timezone.utc),
                    recommendation_count=len(recommendations),
                    analyzer_count=len(self.sources),
                    failed_analyzer_count=len(self.failed_sources),
                    error=", ".join(self.failed_sources) if self.failed_sources else None,
                )
            record_run(
                "succeeded" if not self.failed_sources else "failed",
                elapsed(timer),
                len(recommendations),
            )
        except Exception as exc:
            if run is not None and run_repository is not None:
                await run_repository.complete(
                    run.id,
                    status="failed",
                    completed_at=datetime.now(timezone.utc),
                    recommendation_count=0,
                    analyzer_count=len(self.sources),
                    failed_analyzer_count=len(self.sources),
                    error=str(exc),
                )
            record_run("failed", elapsed(timer))
            raise
        return recommendations

    async def persist(
        self,
        recommendations: List[Recommendation],
        reco_repo: "RecommendationRepository",
        namespace: Optional[str] = None,
    ) -> List[Recommendation]:
        """Upserts generated recommendations, expires overdue actives and reconciles stale ones."""
        try:
            # Expire actives whose TTL elapsed before reconciling so they are not
            # accidentally reported as stale.
            try:
                from greenkube.core.optimization.lifecycle import RecommendationLifecycle

                await RecommendationLifecycle(reco_repo).expire()
            except Exception as e:
                logger.warning("Failed to expire overdue recommendations: %s", e)

            records = [RecommendationRecord.from_recommendation(r) for r in recommendations]
            if records:
                await reco_repo.upsert_recommendations(records)
                await self._attach_persisted_actions(recommendations, reco_repo, namespace)
                action_records = [
                    RecommendationRecord.from_recommendation(recommendation)
                    for recommendation in recommendations
                    if recommendation.action is not None
                ]
                if action_records:
                    await reco_repo.upsert_recommendations(action_records)
            await reco_repo.reconcile_active_recommendations(records, namespace=namespace)
        except Exception as e:
            logger.error("Failed to upsert recommendation history: %s", e)
        return recommendations

    async def _attach_persisted_actions(
        self,
        recommendations: List[Recommendation],
        reco_repo: "RecommendationRepository",
        namespace: Optional[str],
    ) -> None:
        """Attach v3 actions only after recommendation IDs are assigned by persistence."""
        has_candidates = any(
            recommendation.type in (RecommendationType.RIGHTSIZING_CPU, RecommendationType.RIGHTSIZING_MEMORY)
            and recommendation.container_name
            and recommendation.evidence is not None
            and recommendation.evidence.proposed_patch is not None
            for recommendation in recommendations
        )
        if not has_candidates:
            return

        get_active = getattr(reco_repo, "get_active_recommendations", None)
        if get_active is None:
            return
        try:
            persisted = await get_active(namespace=namespace)
            ids_by_fingerprint = {
                record.fingerprint: record.id
                for record in persisted
                if record.fingerprint and record.id is not None and record.id > 0
            }
        except Exception as exc:
            logger.warning("Could not load persisted recommendation IDs for action plans: %s", exc)
            return

        for recommendation in recommendations:
            if recommendation.id is not None and recommendation.id > 0:
                continue
            fingerprint = RecommendationRecord.from_recommendation(recommendation).fingerprint
            recommendation_id = ids_by_fingerprint.get(fingerprint)
            if recommendation_id is not None:
                recommendation.id = recommendation_id

        plans = build_action_plans(recommendations)
        if not plans:
            return
        for recommendation in recommendations:
            if recommendation.id is None:
                continue
            plan = plans.get(recommendation.id)
            if plan is None:
                continue
            recommendation.action = plan
            patch = build_patch(recommendation, plan)
            recommendation.patch = RecommendationPatchV2.model_validate(patch) if patch is not None else None


def _peak_resident_memory_bytes() -> int:
    """Return process peak RSS in bytes across common Unix platforms."""
    try:
        import resource
        import sys

        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except Exception:
        return 0
    return value if sys.platform == "darwin" else value * 1024
