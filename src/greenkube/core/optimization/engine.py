# src/greenkube/core/optimization/engine.py
"""The unified optimization engine.

Single orchestration path for recommendation generation and persistence. The
controller's scheduled ``recommendation_scan`` job (see
:mod:`greenkube.core.optimization.scan_service`) is the only production caller
that persists; API requests only enqueue runs.

Sources are Source SDK sources (:mod:`greenkube.signals`, roadmap E5): one per
native analyzer, VPA, the NodePool heuristic and third-party plugins. v1
``RecommendationSource`` connectors passed by callers are wrapped by the SDK
adapter. Every source runs in isolation (E8) through the
:class:`~greenkube.signals.runtime.SourceRunner`: a source that raises, times
out, is unavailable, rate limited, circuit-broken or reports an incomplete
result never blocks persistence of the others and never marks its previous
recommendations stale. Each source's run and signals are recorded in
``source_runs``/``source_signals`` when a signal repository is configured.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Dict, FrozenSet, List, Optional, Sequence, Tuple, Union

from greenkube.core.optimization.actions import build_action_plans
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.context_builder import ContextBuilder
from greenkube.core.optimization.dedup import (
    RESOURCE_FIELDS,
    apply_consensus_assessment,
    consensus_policy,
    consensus_refiner,
    entries_for_reports,
    finalize_entries,
    track_record_weights,
)
from greenkube.core.optimization.enrich import enrich_recommendations, rescore_with_track_record
from greenkube.core.optimization.evidence import build_patch
from greenkube.core.optimization.providers.base import RecommendationSource
from greenkube.core.optimization.registry import build_signal_sources, cluster_uid
from greenkube.core.optimization.track_record import TrackRecordBook, load_track_record
from greenkube.core.recommendation_reconciliation import RecommendationDiff, compute_diff, supporting_sources
from greenkube.models.metrics import Recommendation, RecommendationPatchV2, RecommendationRecord, RecommendationType
from greenkube.models.optimization_run import (
    OptimizationRun,
    OptimizationRunCompletion,
    OptimizationRunStatus,
    OptimizationRunTrigger,
    SourceRunOutcome,
    SourceRunStatus,
)
from greenkube.models.recommendation_v2 import with_v2_fields
from greenkube.signals.adapters import adapt_recommendation_source, observed_window, workload_inventory
from greenkube.signals.consensus import KIND_REQUEST_RESOURCES, ConsensusPolicy
from greenkube.signals.runtime import SourceReport, SourceRunner, SourceRunRecord, legacy_outcomes
from greenkube.signals.sdk import CollectContext, Source, SourceDeps

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.storage.base_optimization_run_repository import OptimizationRunRepository
    from greenkube.storage.base_repository import CombinedMetricsRepository, NodeRepository, RecommendationRepository
    from greenkube.storage.base_signal_repository import SignalRepository

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
    #: Per-source run records and signals (Source SDK granularity).
    source_reports: List[SourceReport] = field(default_factory=list)

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
    source_reports: List[SourceReport] = field(default_factory=list)


class OptimizationEngine:
    """Runs recommendation sources, finalizes their output and persists it."""

    def __init__(
        self,
        config: Optional["Config"] = None,
        sources: Optional[Sequence[Union[Source, RecommendationSource]]] = None,
        run_repository: Optional["OptimizationRunRepository"] = None,
        *,
        probe_timeout_seconds: float = SOURCE_PROBE_TIMEOUT_SECONDS,
        collect_timeout_seconds: float = SOURCE_COLLECT_TIMEOUT_SECONDS,
        signal_repository: Optional["SignalRepository"] = None,
    ):
        from greenkube.core.config import get_config

        self.config = config if config is not None else get_config()
        self.cluster_uid = cluster_uid(self.config)
        deps = SourceDeps(config=self.config, cluster_uid=self.cluster_uid)
        if sources is None:
            sources = build_signal_sources(self.config)
        # v1 connectors keep working: the SDK adapter wraps them.
        self.sources: List[Source] = [
            source if isinstance(source, Source) else adapt_recommendation_source(source, deps) for source in sources
        ]
        self.run_repository = run_repository
        self.signal_repository = signal_repository
        self.probe_timeout_seconds = probe_timeout_seconds
        self.collect_timeout_seconds = collect_timeout_seconds
        self.runner = SourceRunner(
            probe_timeout_cap=probe_timeout_seconds,
            collect_timeout_cap=collect_timeout_seconds,
            on_failure=self._record_source_failure,
        )
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

    @property
    def source_group_count(self) -> int:
        """Number of v1 sources (native analyzers count as one: ``greenkube``)."""
        return len({type(source).legacy_source or source.source_id for source in self.sources})

    def collect_context(self, context: OptimizationContext) -> CollectContext:
        """What every source receives for this scan."""
        # The inventory walks the usage snapshot: build it only for sources
        # that do not already read the optimization context (plugins).
        needs_inventory = any(type(source).legacy_source is None for source in self.sources)
        return CollectContext(
            cluster_uid=self.cluster_uid,
            namespace=context.namespace,
            window=observed_window(context),
            deadline=datetime.now(timezone.utc) + timedelta(seconds=self.collect_timeout_seconds),
            inventory=workload_inventory(context, self.cluster_uid)
            if needs_inventory
            else workload_inventory(None, ""),
            optimization=context,
        )

    async def collect(
        self, context: OptimizationContext, track_record: Optional["TrackRecordBook"] = None
    ) -> GenerationResult:
        """Runs every source in isolation and builds one recommendation per target by consensus (E6).

        ``track_record`` weighs the sources under the ``weighted`` policy (I3);
        without it every source weighs the same.
        """
        previous = await self._previous_source_runs()
        reports = await self.runner.run_all(self.sources, self.collect_context(context), previous)
        finalized = finalize_entries(
            entries_for_reports(reports, self.cluster_uid),
            self.config,
            refine=consensus_refiner(context),
            weight_for=track_record_weights(track_record),
        )
        enriched = apply_consensus_assessment(enrich_recommendations(finalized, context, self.config), self.config)
        return GenerationResult(
            enriched,
            legacy_outcomes(reports),
            reports,
        )

    async def _previous_source_runs(self) -> Dict[str, SourceRunRecord]:
        """Each source's last persisted run (circuit breaker and rate limit across scans and restarts)."""
        if self.signal_repository is None:
            return {}
        try:
            summaries = await self.signal_repository.latest_runs()
        except Exception as exc:
            logger.warning("Could not read previous source runs (%s); using in-process state.", type(exc).__name__)
            return {}
        return {source_id: summary.latest for source_id, summary in summaries.items()}

    async def record_source_runs(self, reports: List[SourceReport], run: Optional[OptimizationRun]) -> None:
        """Persist each source's run and signals; a storage error never fails the scan."""
        if self.signal_repository is None:
            return
        run_id = run.id if run is not None else None
        for report in reports:
            record = report.run.model_copy(update={"optimization_run_id": run_id})
            try:
                report.run = await self.signal_repository.record_run(record, report.signals)
            except Exception as exc:
                logger.warning("Could not record the run of source '%s' (%s).", report.source_id, type(exc).__name__)

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
        reports: List[SourceReport] = []
        try:
            context = await self.build_context(combined_repo, node_repo, namespace=namespace)
            from greenkube.core.observability import record_recommendation_scan_resources

            record_recommendation_scan_resources(
                context.analysis_window_seconds or 0.0,
                len(context.metrics) + len(context.container_metrics),
                _peak_resident_memory_bytes(),
            )
            track_record = None
            if consensus_policy(self.config) is ConsensusPolicy.WEIGHTED:
                track_record = await load_track_record(reco_repo)
            generation = await self.collect(context, track_record)
            outcomes = generation.source_outcomes
            reports = generation.source_reports
            await self.record_source_runs(reports, run)
            # Scans score with priors; fold in each source's measured track record (I3).
            scored = await rescore_with_track_record(generation.recommendations, reco_repo, self.config)
            diff, persisted = await self._persist(
                scored,
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
            source_reports=reports,
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
                analyzer_count=self.source_group_count,
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
            failed_count = self.source_group_count
        completion = OptimizationRunCompletion(
            status=status,
            completed_at=datetime.now(timezone.utc),
            recommendation_count=len(recommendations),
            analyzer_count=self.source_group_count,
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

        await self._backfill_v2_fields(reco_repo)
        previous = await self._active_snapshot(reco_repo, namespace)
        kept = self._preserve_last_known(recommendations, previous, incomplete_sources)
        records = [self._record(r) for r in kept]
        if records:
            await reco_repo.upsert_recommendations(records)
            await self._attach_persisted_actions(kept, reco_repo, namespace)
            action_records = [
                self._record(recommendation) for recommendation in kept if recommendation.action is not None
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

    def _record(self, recommendation: Recommendation) -> RecommendationRecord:
        """The persisted form of a recommendation, with its v2 identity, target and proposal (E4)."""
        return with_v2_fields(RecommendationRecord.from_recommendation(recommendation), self.cluster_uid)

    async def _backfill_v2_fields(self, reco_repo: "RecommendationRepository") -> None:
        """Give rows written before Recommendation v2 their v2 columns (once, then a no-op)."""
        try:
            filled = await reco_repo.backfill_v2_fields(self.cluster_uid)
        except Exception as exc:
            logger.warning("Could not backfill the v2 recommendation columns (%s).", type(exc).__name__)
            return
        if isinstance(filled, int) and filled:
            logger.info("Backfilled the v2 columns of %d recommendation(s).", filled)

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
        """Account for the last known vote of a supporting source that is incomplete (E6, E8).

        When a source that supported an active recommendation did not complete
        this run, it is missing from the new consensus. Its last known
        proposal (stored in the record's consensus votes) still counts, so the
        advice does not flap with every transient outage of one connector, but
        it never holds back a safer value:

        * the new proposal is at least the previous one (a higher request, or
          an increase replacing a decrease): the new recommendation is written;
        * under ``safest``, every incomplete supporter's last known value is at
          most the new proposal: ``safest`` of the new and last known votes is
          the new proposal, which is written;
        * otherwise (the new proposal is more aggressive than what the last
          known votes support, or a non-numeric proposal) the last known
          record stays until that source completes again (or expires).

        A written recommendation keeps the incomplete sources among its
        supporting sources, with their last known values in ``consensus.last_known``.
        """
        if not incomplete_sources or not previous:
            return recommendations
        previous_by_fp = {record.fingerprint: record for record in previous if record.fingerprint}
        policy = consensus_policy(self.config)
        kept: List[Recommendation] = []
        for recommendation in recommendations:
            fingerprint = RecommendationRecord.from_recommendation(recommendation).fingerprint
            before = previous_by_fp.get(fingerprint) if fingerprint else None
            if before is not None:
                previous_support = supporting_sources(before.source, before.sources)
                current_support = supporting_sources(recommendation.source, recommendation.sources)
                missing = sorted((previous_support - current_support) & set(incomplete_sources))
                if missing:
                    merged = _with_last_known_votes(recommendation, before, missing, policy)
                    if merged is None:
                        logger.info(
                            "Keeping last known recommendation %s while %s is incomplete.",
                            fingerprint,
                            ", ".join(missing),
                        )
                        continue
                    recommendation = merged
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


def _last_known_values(before: RecommendationRecord, missing: Sequence[str], resource: str) -> Dict[str, int]:
    """``{source_id: value}`` of the incomplete sources' votes in ``before``'s consensus."""
    consensus = before.consensus if isinstance(before.consensus, dict) else {}
    if consensus.get("resource") != resource:
        return {}
    values: Dict[str, int] = {}
    for vote in consensus.get("votes") or []:
        if not isinstance(vote, dict) or vote.get("source") not in missing:
            continue
        value = vote.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            source_id = str(vote.get("source_id") or vote.get("source"))
            values[source_id] = max(int(value), values.get(source_id, int(value)))
    return values


def _with_last_known_votes(
    recommendation: Recommendation,
    before: RecommendationRecord,
    missing: Sequence[str],
    policy: ConsensusPolicy,
) -> Optional[Recommendation]:
    """``recommendation`` supported by the incomplete sources' last known votes, or None to keep ``before``.

    A higher request is always the safer one (a smaller decrease, or an
    increase that protects reliability), so the new proposal is written when
    it is not more aggressive than what the last known votes support.
    """
    resource = KIND_REQUEST_RESOURCES.get(str(getattr(recommendation.type, "value", recommendation.type)))
    if resource is None:
        return None
    _current_field, proposed_field = RESOURCE_FIELDS[resource]
    new_value = getattr(recommendation, proposed_field)
    old_value = getattr(before, proposed_field)
    if new_value is None or old_value is None:
        return None
    last_known = _last_known_values(before, missing, resource)
    known_units = {
        vote.get("source")
        for vote in (before.consensus or {}).get("votes") or []
        if isinstance(vote, dict) and vote.get("source_id") in last_known
    }
    every_vote_known = all(unit in known_units for unit in missing)
    safer_or_equal = int(new_value) >= int(old_value)
    safest_holds = (
        policy is ConsensusPolicy.SAFEST
        and every_vote_known
        and all(value <= int(new_value) for value in last_known.values())
    )
    if not (safer_or_equal or safest_holds):
        return None
    sources = list(recommendation.sources or [])
    for unit in missing:
        if unit not in sources:
            sources.append(unit)
    update: Dict[str, object] = {"sources": sources}
    if isinstance(recommendation.consensus, dict):
        consensus = {**recommendation.consensus, "last_known": last_known}
        update["consensus"] = consensus
        if recommendation.evidence is not None and recommendation.evidence.consensus is not None:
            update["evidence"] = recommendation.evidence.model_copy(update={"consensus": consensus})
    logger.info(
        "Recommendation %s follows the new proposal (%s) while %s is incomplete; last known votes: %s.",
        before.fingerprint,
        new_value,
        ", ".join(missing),
        last_known or "unknown",
    )
    return recommendation.model_copy(update=update)


def _run_status(outcomes: List[SourceRunOutcome]) -> OptimizationRunStatus:
    """A run fails only when every source that was attempted failed or was incomplete."""
    attempted = [o for o in outcomes if o.status != SourceRunStatus.UNAVAILABLE]
    if attempted and not any(o.status == SourceRunStatus.SUCCEEDED for o in attempted):
        return OptimizationRunStatus.FAILED
    return OptimizationRunStatus.SUCCEEDED


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
