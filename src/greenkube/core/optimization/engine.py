# src/greenkube/core/optimization/engine.py
"""The unified optimization engine.

Single orchestration path for recommendation generation and persistence. The
controller's scheduled ``recommendation_scan`` job (see
:mod:`greenkube.core.optimization.scan_service`) is the only production caller
that persists; API requests only enqueue runs.

Every source runs in isolation (E8): a source that raises, times out, is
unavailable or reports an incomplete result never blocks persistence of the
others and never marks its previous recommendations stale.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, FrozenSet, List, Optional, Sequence, Tuple

from greenkube.core.optimization.actions import build_action_plans
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.context_builder import ContextBuilder
from greenkube.core.optimization.dedup import finalize_recommendations
from greenkube.core.optimization.enrich import enrich_recommendations
from greenkube.core.optimization.evidence import build_patch
from greenkube.core.optimization.providers.base import RecommendationSource
from greenkube.core.optimization.registry import build_sources, parse_source_priority
from greenkube.core.recommendation_reconciliation import RecommendationDiff, compute_diff
from greenkube.models.metrics import Recommendation, RecommendationPatchV2, RecommendationRecord, RecommendationType
from greenkube.models.optimization_run import (
    OptimizationRun,
    OptimizationRunCompletion,
    OptimizationRunStatus,
    OptimizationRunTrigger,
    SourceRunOutcome,
    SourceRunStatus,
)

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.storage.base_optimization_run_repository import OptimizationRunRepository
    from greenkube.storage.base_repository import CombinedMetricsRepository, NodeRepository, RecommendationRepository

logger = logging.getLogger(__name__)

#: Bound on a source availability probe (Kubernetes discovery calls).
SOURCE_PROBE_TIMEOUT_SECONDS = 30.0
#: Bound on one source's collection; a hung connector must not stall the scan.
SOURCE_COLLECT_TIMEOUT_SECONDS = 300.0


@dataclass
class GenerationResult:
    """Finalized recommendations plus the health of every source that produced them."""

    recommendations: List[Recommendation]
    source_outcomes: List[SourceRunOutcome] = field(default_factory=list)

    @property
    def incomplete_sources(self) -> FrozenSet[str]:
        """Sources whose result is not authoritative for reconciliation."""
        return frozenset(outcome.source for outcome in self.source_outcomes if not outcome.complete)

    @property
    def failed_sources(self) -> List[str]:
        """Sources that raised, timed out or returned a partial result."""
        return [
            outcome.source
            for outcome in self.source_outcomes
            if outcome.status in (SourceRunStatus.FAILED, SourceRunStatus.INCOMPLETE)
        ]


@dataclass
class ScanResult:
    """Outcome of one persisted optimization scan."""

    status: OptimizationRunStatus
    recommendations: List[Recommendation] = field(default_factory=list)
    source_outcomes: List[SourceRunOutcome] = field(default_factory=list)
    diff: Optional[RecommendationDiff] = None
    run: Optional[OptimizationRun] = None
    error: Optional[str] = None


class OptimizationEngine:
    """Runs recommendation sources, finalizes their output and persists it."""

    def __init__(
        self,
        config: Optional["Config"] = None,
        sources: Optional[Sequence[RecommendationSource]] = None,
        run_repository: Optional["OptimizationRunRepository"] = None,
        *,
        probe_timeout_seconds: float = SOURCE_PROBE_TIMEOUT_SECONDS,
        collect_timeout_seconds: float = SOURCE_COLLECT_TIMEOUT_SECONDS,
    ):
        from greenkube.core.config import get_config

        self.config = config if config is not None else get_config()
        if sources is None:
            sources = build_sources(self.config)
        self.sources: List[RecommendationSource] = list(sources)
        self.run_repository = run_repository
        self.probe_timeout_seconds = probe_timeout_seconds
        self.collect_timeout_seconds = collect_timeout_seconds
        # Compatibility view of the last ``generate`` call; scans use the
        # returned ``GenerationResult`` instead of shared instance state.
        self.failed_sources: List[str] = []
        self.source_outcomes: List[SourceRunOutcome] = []

    async def generate(self, context: OptimizationContext) -> List[Recommendation]:
        """Runs every available source and returns deduplicated recommendations."""
        result = await self.collect(context)
        self.failed_sources = result.failed_sources
        self.source_outcomes = result.source_outcomes
        return result.recommendations

    async def collect(self, context: OptimizationContext) -> GenerationResult:
        """Runs every source in isolation and finalizes the combined output."""
        recs: List[Recommendation] = []
        outcomes: List[SourceRunOutcome] = []
        for source in self.sources:
            source_recs, outcome = await self._run_source(source, context)
            recs.extend(source_recs)
            outcomes.append(outcome)
        finalized = finalize_recommendations(recs, self.config)
        return GenerationResult(enrich_recommendations(finalized, context, self.config), outcomes)

    async def _run_source(
        self, source: RecommendationSource, context: OptimizationContext
    ) -> Tuple[List[Recommendation], SourceRunOutcome]:
        source_name = str(getattr(source, "name", None) or repr(source))
        started = time.monotonic()

        def outcome(status: SourceRunStatus, count: int = 0, error: Optional[str] = None) -> SourceRunOutcome:
            return SourceRunOutcome(
                source=source_name,
                status=status,
                complete=status == SourceRunStatus.SUCCEEDED,
                recommendation_count=count,
                duration_seconds=round(max(time.monotonic() - started, 0.0), 3),
                error=error,
            )

        try:
            available = await asyncio.wait_for(source.is_available(), timeout=self.probe_timeout_seconds)
            if not available:
                logger.debug("Recommendation source '%s' is not available; skipping.", source_name)
                return [], outcome(SourceRunStatus.UNAVAILABLE)
            _reset_completeness(source)
            collected = list(await asyncio.wait_for(source.collect(context), timeout=self.collect_timeout_seconds))
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.error("Recommendation source '%s' timed out; continuing without it.", source_name)
            self._record_source_failure(source_name)
            return [], outcome(SourceRunStatus.FAILED, error="timeout")
        except Exception as exc:
            logger.exception("Recommendation source '%s' failed; continuing without it.", source_name)
            self._record_source_failure(source_name)
            return [], outcome(SourceRunStatus.FAILED, error=type(exc).__name__)

        # Seam for connectors that degrade gracefully (return a partial list
        # instead of raising): they set ``last_collect_complete = False`` during
        # ``collect()``. The engine resets the flag before every call.
        if getattr(source, "last_collect_complete", True) is False:
            logger.warning("Recommendation source '%s' returned an incomplete result.", source_name)
            return collected, outcome(SourceRunStatus.INCOMPLETE, len(collected), "incomplete result")
        return collected, outcome(SourceRunStatus.SUCCEEDED, len(collected))

    @staticmethod
    def _record_source_failure(source_name: str) -> None:
        from greenkube.core.observability import record_analyzer_failure

        record_analyzer_failure(source_name)

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
        run: Optional[OptimizationRun] = None,
    ) -> List[Recommendation]:
        """Generates recommendations, persists them and reconciles active records.

        Raises when the scan fails (context, persistence); the run, when one is
        tracked, is marked failed first.
        """
        result = await self.run_scan(combined_repo, node_repo, reco_repo, namespace=namespace, run=run)
        return result.recommendations

    async def run_scan(
        self,
        combined_repo: "CombinedMetricsRepository",
        node_repo: "NodeRepository",
        reco_repo: "RecommendationRepository",
        namespace: Optional[str] = None,
        run: Optional[OptimizationRun] = None,
    ) -> ScanResult:
        """Executes one scan and records its outcome on ``run``.

        ``run`` is a claimed (``running``) queue entry. Without one, a direct run
        is recorded when the engine has a run repository.
        """
        from greenkube.core.observability import elapsed, record_run, start_timer

        timer = start_timer()
        run = await self._start_run(run, namespace)
        outcomes: List[SourceRunOutcome] = []
        try:
            context = await self.build_context(combined_repo, node_repo, namespace=namespace)
            from greenkube.core.observability import record_recommendation_scan_resources

            record_recommendation_scan_resources(
                context.analysis_window_seconds or 0.0,
                len(context.metrics) + len(context.container_metrics),
                _peak_resident_memory_bytes(),
            )
            generation = await self.collect(context)
            outcomes = generation.source_outcomes
            diff, persisted = await self._persist(
                generation.recommendations,
                reco_repo,
                namespace=namespace,
                incomplete_sources=generation.incomplete_sources,
            )
        except BaseException as exc:
            error = _error_text(exc)
            run = await self._finish_run(
                run,
                OptimizationRunStatus.FAILED,
                recommendations=[],
                outcomes=outcomes,
                diff=None,
                error=error,
            )
            record_run(OptimizationRunStatus.FAILED.value, elapsed(timer))
            raise

        status = _run_status(outcomes)
        failed = [o.source for o in outcomes if o.status in (SourceRunStatus.FAILED, SourceRunStatus.INCOMPLETE)]
        error = None
        if failed:
            error = "Sources failed or incomplete: " + ", ".join(sorted(failed))
        run = await self._finish_run(
            run,
            status,
            recommendations=persisted,
            outcomes=outcomes,
            diff=diff,
            error=error,
        )
        record_run(status.value, elapsed(timer), len(persisted))
        return ScanResult(
            status=status,
            recommendations=persisted,
            source_outcomes=outcomes,
            diff=diff,
            run=run,
            error=error,
        )

    async def _start_run(self, run: Optional[OptimizationRun], namespace: Optional[str]) -> Optional[OptimizationRun]:
        if run is not None or self.run_repository is None:
            return run
        now = datetime.now(timezone.utc)
        return await self.run_repository.create(
            OptimizationRun(
                status=OptimizationRunStatus.RUNNING,
                trigger=OptimizationRunTrigger.DIRECT,
                requested_at=now,
                started_at=now,
                namespace=namespace,
                analyzer_count=len(self.sources),
            )
        )

    async def _finish_run(
        self,
        run: Optional[OptimizationRun],
        status: OptimizationRunStatus,
        *,
        recommendations: List[Recommendation],
        outcomes: List[SourceRunOutcome],
        diff: Optional[RecommendationDiff],
        error: Optional[str],
    ) -> Optional[OptimizationRun]:
        if run is None or run.id is None or self.run_repository is None:
            return run
        failed_count = sum(1 for o in outcomes if o.status in (SourceRunStatus.FAILED, SourceRunStatus.INCOMPLETE))
        if status == OptimizationRunStatus.FAILED and not outcomes:
            failed_count = len(self.sources)
        completion = OptimizationRunCompletion(
            status=status,
            completed_at=datetime.now(timezone.utc),
            recommendation_count=len(recommendations),
            analyzer_count=len(self.sources),
            failed_analyzer_count=failed_count,
            new_count=len(diff.new) if diff is not None else None,
            changed_count=len(diff.changed) if diff is not None else None,
            resolved_count=len(diff.resolved) if diff is not None else None,
            unchanged_count=len(diff.unchanged) + len(diff.retained) if diff is not None else None,
            source_outcomes=outcomes,
            diff=diff.to_run_diff() if diff is not None else None,
            error=error,
        )
        try:
            return await self.run_repository.finish(run.id, completion)
        except Exception as exc:
            logger.error("Could not record the outcome of optimization run %s: %s", run.id, exc)
            return run

    async def persist(
        self,
        recommendations: List[Recommendation],
        reco_repo: "RecommendationRepository",
        namespace: Optional[str] = None,
        incomplete_sources: FrozenSet[str] = frozenset(),
    ) -> List[Recommendation]:
        """Best-effort persistence for callers outside a tracked scan; errors are logged."""
        try:
            await self._persist(recommendations, reco_repo, namespace=namespace, incomplete_sources=incomplete_sources)
        except Exception as e:
            logger.error("Failed to upsert recommendation history: %s", e)
        return recommendations

    async def _persist(
        self,
        recommendations: List[Recommendation],
        reco_repo: "RecommendationRepository",
        namespace: Optional[str] = None,
        incomplete_sources: FrozenSet[str] = frozenset(),
    ) -> Tuple[RecommendationDiff, List[Recommendation]]:
        """Upserts generated recommendations, expires overdue actives and reconciles stale ones.

        Returns the diff against the active set of the scanned scope and the
        recommendations actually written (an incomplete winner's last known
        advice is kept instead of a lower-priority replacement). Raises on
        repository errors so the run can be recorded as failed.
        """
        # Expire actives whose TTL elapsed before reconciling so they are not
        # accidentally reported as stale.
        try:
            from greenkube.core.optimization.lifecycle import RecommendationLifecycle

            await RecommendationLifecycle(reco_repo).expire()
        except Exception as e:
            logger.warning("Failed to expire overdue recommendations: %s", e)

        previous = await self._active_snapshot(reco_repo, namespace)
        kept = self._preserve_last_known(recommendations, previous, incomplete_sources)
        records = [RecommendationRecord.from_recommendation(r) for r in kept]
        if records:
            await reco_repo.upsert_recommendations(records)
            await self._attach_persisted_actions(kept, reco_repo, namespace)
            action_records = [
                RecommendationRecord.from_recommendation(recommendation)
                for recommendation in kept
                if recommendation.action is not None
            ]
            if action_records:
                await reco_repo.upsert_recommendations(action_records)
        await reco_repo.reconcile_active_recommendations(
            records,
            namespace=namespace,
            incomplete_sources=sorted(incomplete_sources),
        )
        # A namespaced scan snapshots and reconciles that namespace only; any
        # cluster-scoped record it emits is outside the baseline, not "new".
        in_scope = records if namespace is None else [record for record in records if record.namespace == namespace]
        return compute_diff(previous, in_scope, incomplete_sources), kept

    @staticmethod
    async def _active_snapshot(
        reco_repo: "RecommendationRepository", namespace: Optional[str]
    ) -> List[RecommendationRecord]:
        """Active records before this run's writes (diff baseline and last-known lookup)."""
        try:
            snapshot = await reco_repo.get_active_recommendations(namespace=namespace)
        except Exception as exc:
            logger.warning("Could not snapshot active recommendations before persisting: %s", exc)
            return []
        if not isinstance(snapshot, list):
            return []
        return [record for record in snapshot if isinstance(record, RecommendationRecord)]

    def _preserve_last_known(
        self,
        recommendations: List[Recommendation],
        previous: List[RecommendationRecord],
        incomplete_sources: FrozenSet[str],
    ) -> List[Recommendation]:
        """Keep an incomplete higher-priority source's last known advice.

        When the arbitration winner of an active recommendation did not
        complete this run, a lower-priority source re-emitting the same target
        must not overwrite it; otherwise the advice would flap between
        connectors on every transient outage.
        """
        if not incomplete_sources or not previous:
            return recommendations
        priorities: Dict[str, int] = parse_source_priority(getattr(self.config, "RECOMMENDATION_SOURCE_PRIORITY", None))
        previous_by_fp = {record.fingerprint: record for record in previous if record.fingerprint}
        kept: List[Recommendation] = []
        for recommendation in recommendations:
            fingerprint = RecommendationRecord.from_recommendation(recommendation).fingerprint
            before = previous_by_fp.get(fingerprint) if fingerprint else None
            if before is not None:
                old_source = _enum_text(before.source)
                new_source = _enum_text(recommendation.source)
                if (
                    old_source != new_source
                    and old_source in incomplete_sources
                    and priorities.get(old_source, 0) > priorities.get(new_source, 0)
                ):
                    logger.info(
                        "Keeping last known '%s' recommendation %s while that source is incomplete.",
                        old_source,
                        fingerprint,
                    )
                    continue
            kept.append(recommendation)
        return kept

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


def _reset_completeness(source: RecommendationSource) -> None:
    """Clear a connector's partial-result flag before each ``collect()`` call.

    The flag describes one call; without the reset a single partial result
    would keep the source "incomplete" (and its recommendations un-reconciled)
    forever. Sources whose flag is read-only are left as they are.
    """
    try:
        setattr(source, "last_collect_complete", True)
    except (AttributeError, TypeError):
        pass


def _run_status(outcomes: List[SourceRunOutcome]) -> OptimizationRunStatus:
    """A run fails only when every source that was attempted failed or was incomplete."""
    attempted = [o for o in outcomes if o.status != SourceRunStatus.UNAVAILABLE]
    if attempted and not any(o.status == SourceRunStatus.SUCCEEDED for o in attempted):
        return OptimizationRunStatus.FAILED
    return OptimizationRunStatus.SUCCEEDED


def _enum_text(value) -> str:
    return str(value.value if hasattr(value, "value") else value)


def _error_text(exc: BaseException) -> str:
    """API-safe run error: the exception type, plus the message only for GreenKube's own scan bound."""
    from greenkube.core.optimization.context_builder import RecommendationScanLimitError

    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    if isinstance(exc, RecommendationScanLimitError):
        return f"{type(exc).__name__}: {exc}"[:500]
    return type(exc).__name__


def _peak_resident_memory_bytes() -> int:
    """Return process peak RSS in bytes across common Unix platforms."""
    try:
        import resource
        import sys

        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except Exception:
        return 0
    return value if sys.platform == "darwin" else value * 1024
