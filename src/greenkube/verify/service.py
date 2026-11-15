# src/greenkube/verify/service.py
"""Change measurement pipeline (roadmap C1-C5).

:class:`ChangeMeasurementService` is called from two places:

* the **applied detector** records every applied change as an immutable
  :class:`~greenkube.verify.model.ChangeEvent` with its
  :class:`~greenkube.verify.model.BaselineSnapshot` (C1), and the pod-template
  revisions of the workloads it reads (spec observations);
* the **lifecycle job** calls :meth:`ChangeMeasurementService.run_due`, which
  drives each change through ``applied → measuring → {realized |
  not_realized | inconclusive | contaminated}`` (C2), measures it once with
  the strategy of its action kind (C3, C4) and splits the result across its
  recommendations (C5).

Recommendations keep their existing API during the transition: an adapter
writes one ``savings_measurements`` row per recommendation (linked by
``change_id`` and ``group_id`` = the change measurement) and moves the
recommendation to ``verified``, ``applied``/``failed`` (not realized),
``applied``/``inconclusive`` or ``rollback_review`` exactly as the interim
verifier did. The interim verifier skips recommendations that belong to a
recorded change.
"""

from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from greenkube.automation.actions.delete_resource import deletion_target_for
from greenkube.core.savings_attributor import REPRESENTATIVE_KEY
from greenkube.impact.comparison import measurable_co2e_projection
from greenkube.impact.types import CO2E_COMPONENTS, MEASURED_CO2E_COMPONENTS
from greenkube.models.measurement import (
    MeasurementOutcome,
    QuantityEstimate,
    QuantityOutcome,
    SavingsMeasurement,
)
from greenkube.models.metrics import RecommendationEventType, RecommendationRecord, RecommendationStatus
from greenkube.models.verification import HealthGateResult, HealthGateStatus, MeasuredLedgerInput
from greenkube.storage.change_repository import ChangeConflictError, ChangeRepository, change_identity
from greenkube.utils.time import ensure_utc
from greenkube.verify.attribution import allocate, allocated_projection
from greenkube.verify.capacity import measure_capacity
from greenkube.verify.confounders import (
    SCOPE_WORKLOAD_NODES,
    ConfounderDetector,
    PoolContext,
    PoolNode,
    PoolSample,
    confidence_score,
    pool_unit_prices,
)
from greenkube.verify.health_baseline import (
    CONTAINERS_KEY,
    OOM_RESTARTS_KEY,
    compare_with_baseline,
    container_key,
    freeze_health,
)
from greenkube.verify.load_driver import CollectedLoad, LoadDriverResolver, PrometheusRangeReader
from greenkube.verify.model import (
    DECISION_GRACE,
    Allocation,
    BaselineSnapshot,
    ChangeApplicationMethod,
    ChangeEvent,
    ChangeMeasurement,
    ChangeOutcome,
    ChangeStatus,
    MeasurementState,
    SavingsEstimate,
    TimeWindow,
    WindowPlan,
    WorkloadRef,
    WorkloadSpecObservation,
)
from greenkube.verify.series import floor_hour, hourly_buckets, price_card, utc_hour
from greenkube.verify.strategy import (
    CO2E_WITH_EMBODIED,
    OBJECTIVE_RELIABILITY,
    OBJECTIVE_SAVINGS,
    MeasurementSettings,
    ReliabilityOutcome,
    SavingsMeasurementStrategy,
    change_objective,
    increase_reliability_detail,
    reliability_decision,
    strategy_for,
)

if TYPE_CHECKING:
    from greenkube.core.optimization.lifecycle import RecommendationLifecycle
    from greenkube.core.optimization.verifier import RecommendationVerifier
    from greenkube.storage.measurement_repository import SavingsMeasurementRepository

logger = logging.getLogger(__name__)

#: How long evidence waits for an unreachable Prometheus load driver before it is frozen without it.
LOAD_DRIVER_RETRY = timedelta(hours=24)
#: Key of the snapshot's ``collector_versions`` (and of the measurement's
#: ``counterfactual_params``) listing the load drivers that could not be read.
UNAVAILABLE_DRIVERS_KEY = "load_drivers_unavailable"
#: Node pools are sampled from the node snapshots this often over each window (pool confounders, C4).
POOL_SAMPLE_INTERVAL = timedelta(hours=6)
#: Each pool sample also reads this much of every pod's metrics for the pool's price card (C4).
POOL_PRICE_SLICE = timedelta(hours=1)
#: Pod rows read after another change's rollout to see which nodes its workload ran on.
POOL_NEIGHBOUR_LOOKUP = timedelta(hours=2)
#: At most this many other changes in the measurement are checked for pool membership.
POOL_MAX_OTHER_CHANGES = 50


class MeasurementReadError(RuntimeError):
    """A read needed for immutable evidence failed: nothing is frozen and the next pass retries."""


#: Recommendation statuses whose current measurement may still be decided.
_AWAITING_STATUSES = frozenset({RecommendationStatus.APPLIED.value, RecommendationStatus.VERIFYING.value})
_AWAITING_VERIFICATION = frozenset({None, "", "pending", "in_progress"})
#: Statuses of a recommendation whose change is still in effect (it shares the measured saving).
_IN_EFFECT = frozenset({"applied", "verifying", "verified", "rollback_review", "regressed"})
#: States from which an explicit re-measurement starts a new measurement (C0.5).
_REMEASURABLE = frozenset({MeasurementState.NOT_REALIZED, MeasurementState.INCONCLUSIVE, MeasurementState.CONTAMINATED})

_OUTCOME_TO_STATE = {
    ChangeOutcome.REALIZED: MeasurementState.REALIZED,
    ChangeOutcome.NOT_REALIZED: MeasurementState.NOT_REALIZED,
    ChangeOutcome.INCONCLUSIVE: MeasurementState.INCONCLUSIVE,
    ChangeOutcome.CONTAMINATED: MeasurementState.CONTAMINATED,
}
_OUTCOME_TO_RECOMMENDATION = {
    ChangeOutcome.REALIZED: MeasurementOutcome.REALIZED,
    ChangeOutcome.NOT_REALIZED: MeasurementOutcome.NOT_REALIZED,
    ChangeOutcome.INCONCLUSIVE: MeasurementOutcome.INCONCLUSIVE,
    # A contaminated measurement says nothing about the recommendation.
    ChangeOutcome.CONTAMINATED: MeasurementOutcome.INCONCLUSIVE,
}


def _value(raw: Any) -> str:
    return str(getattr(raw, "value", raw)) if raw is not None else ""


def _baseline(record: RecommendationRecord) -> dict:
    baseline = record.baseline
    if baseline is None:
        return {}
    return baseline.model_dump(mode="json") if hasattr(baseline, "model_dump") else dict(baseline)


def _sequence(record: RecommendationRecord) -> int:
    try:
        return int(_baseline(record).get("measurement_sequence") or 1)
    except (TypeError, ValueError):
        return 1


def _awaiting(record: RecommendationRecord) -> bool:
    return _value(record.status) in _AWAITING_STATUSES and record.verification_status in _AWAITING_VERIFICATION


def _parse(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        return ensure_utc(value)
    except (TypeError, ValueError):
        return None


def _application_method(record: RecommendationRecord, plan_id: Optional[str]) -> ChangeApplicationMethod:
    method = _value(record.application_method)
    if plan_id is not None or method in {"pr_merge", "webhook", "polling"}:
        return ChangeApplicationMethod.GITOPS_PR
    if method == "manual":
        return ChangeApplicationMethod.MANUAL
    return ChangeApplicationMethod.DETECTED


class ChangeMeasurementService:
    """Records applied changes and measures them (C1-C5)."""

    def __init__(
        self,
        lifecycle: "RecommendationLifecycle",
        combined_repo: Any,
        change_repo: ChangeRepository,
        *,
        verifier: Optional["RecommendationVerifier"] = None,
        measurement_repo: Optional["SavingsMeasurementRepository"] = None,
        config: Any = None,
        load_reader: Optional[PrometheusRangeReader] = None,
        pull_request_repo: Any = None,
        node_repo: Any = None,
        clock: Optional[Callable[[], datetime]] = None,
        workload_reader: Optional[Callable[[str, str, str], Awaitable[Any]]] = None,
    ):
        from greenkube.core.config import get_config

        #: Reads one live workload snapshot when a change is recorded without one (PATCH /apply,
        #: adoption): its pod-template, resources and image digests and UID back the drift checks.
        self.workload_reader = workload_reader

        self.lifecycle = lifecycle
        self.combined_repo = combined_repo
        self.changes = change_repo
        self.config = config if config is not None else get_config()
        if verifier is None:
            from greenkube.core.optimization.verifier import RecommendationVerifier

            verifier = RecommendationVerifier(
                lifecycle, combined_repo, config=self.config, measurement_repo=measurement_repo
            )
        self.verifier = verifier
        self.measurement_repo = (
            measurement_repo if measurement_repo is not None else getattr(verifier, "measurement_repo", None)
        )
        self.settings = MeasurementSettings.from_config(self.config)
        self.resolver = LoadDriverResolver(load_reader, min_coverage=self.settings.min_coverage)
        self.detector = ConfounderDetector()
        self.pull_request_repo = pull_request_repo
        #: Node snapshots (pool confounders, C4); without it the pool checks are skipped.
        self.node_repo = node_repo
        self._clock = clock

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "CHANGE_MEASUREMENT_ENABLED", True))

    def _now(self) -> datetime:
        return ensure_utc(self._clock()) if self._clock is not None else datetime.now(timezone.utc)

    def _step(self) -> int:
        from greenkube.core.optimization.verifier import _step_seconds

        return _step_seconds(self.config)

    def _health_window(self) -> timedelta:
        return timedelta(hours=float(getattr(self.config, "VERIFICATION_WINDOW_HOURS", 72)))

    @staticmethod
    def _strategy(change: ChangeEvent) -> SavingsMeasurementStrategy:
        strategy = strategy_for(change.action_kind)
        if strategy is None:
            strategy = strategy_for("container_resources")
        assert strategy is not None
        return strategy

    async def _read_workload(self, target: WorkloadRef, window: TimeWindow) -> List[Any]:
        """Pod-level rows of the workload in ``window`` (the change is measured on its workload, C5).

        A failed read raises :class:`MeasurementReadError`: these rows become
        insert-only evidence (a baseline snapshot or a decision), and an empty
        result must mean "no data", never "the database timed out".
        """
        from greenkube.core.optimization.targeting import target_key

        if window.end <= window.start:
            return []
        try:
            rows = await self.combined_repo.read_combined_metrics_smart(
                start_time=window.start, end_time=window.end, namespace=target.namespace
            )
        except Exception as exc:
            raise MeasurementReadError(f"metrics of {'/'.join(target.key)} could not be read: {exc}") from exc
        return [row for row in rows or [] if target_key(row) == target.key]

    async def _pool_samples(self, window: TimeWindow) -> List[Tuple[datetime, List[Any]]]:
        """Active nodes every :data:`POOL_SAMPLE_INTERVAL` over ``window`` (node snapshots, SCD2)."""
        samples: List[Tuple[datetime, List[Any]]] = []
        moment = ensure_utc(window.start)
        end = ensure_utc(window.end)
        while moment < end:
            try:
                nodes = await self.node_repo.get_latest_snapshots_before(moment)
            except Exception as exc:
                raise MeasurementReadError(f"node snapshots at {moment.isoformat()} could not be read: {exc}") from exc
            samples.append((moment, list(nodes or [])))
            moment += POOL_SAMPLE_INTERVAL
        return samples

    async def _pool_context(
        self, change: ChangeEvent, plan: WindowPlan, post_rows: Sequence[Any]
    ) -> Optional[PoolContext]:
        """The workload's node pools over both windows and the changes that landed in them (C4).

        The pools are those of the nodes the workload's pods ran on during the
        post-window. When none of those nodes carries a pool label, only the
        price control runs, over every other pod of those nodes (scope
        ``workload_nodes``, MEA-02). A failed read raises
        :class:`MeasurementReadError`, so the decision waits.
        """
        workload_nodes = {getattr(row, "node", None) for row in post_rows} - {None}
        if self.node_repo is None or not workload_nodes:
            return None
        pre = await self._pool_samples(plan.pre)
        post = await self._pool_samples(plan.post)
        pools = sorted(
            {
                node.node_pool
                for _, nodes in pre + post
                for node in nodes
                if node.name in workload_nodes and node.node_pool
            }
        )
        if not pools:
            nodes = {str(node) for node in workload_nodes}
            return PoolContext(
                pools=(),
                pre=(),
                post=(),
                prices_pre=pool_unit_prices(
                    await self._pool_rows(plan.pre), nodes, self._step(), exclude=change.target.key
                ),
                prices_post=pool_unit_prices(
                    await self._pool_rows(plan.post), nodes, self._step(), exclude=change.target.key
                ),
                scope=SCOPE_WORKLOAD_NODES,
                nodes=tuple(sorted(str(node) for node in nodes)),
            )

        def in_pools(samples: List[Tuple[datetime, List[Any]]]) -> Tuple[PoolSample, ...]:
            # A sample without any node of the pools predates the node snapshots: it is no data.
            built = (
                PoolSample(
                    at=moment,
                    nodes=tuple(
                        PoolNode(
                            name=node.name,
                            pool=node.node_pool,
                            instance_type=node.instance_type,
                            cpu_cores=float(node.cpu_capacity_cores or 0.0),
                        )
                        for node in nodes
                        if node.node_pool in pools
                    ),
                )
                for moment, nodes in samples
            )
            return tuple(sample for sample in built if sample.nodes)

        pre_samples, post_samples = in_pools(pre), in_pools(post)
        pool_nodes = {node.name for sample in pre_samples + post_samples for node in sample.nodes}
        step = self._step()
        own = change.target.key
        prices_pre = pool_unit_prices(await self._pool_rows(plan.pre), pool_nodes, step, exclude=own)
        prices_post = pool_unit_prices(await self._pool_rows(plan.post), pool_nodes, step, exclude=own)
        neighbours: List[ChangeEvent] = []
        try:
            landed = await self.changes.changes_landed_between(
                ensure_utc(change.baseline_window_end), ensure_utc(plan.post.end), limit=POOL_MAX_OTHER_CHANGES
            )
        except Exception as exc:
            raise MeasurementReadError(f"changes landed during the measurement could not be read: {exc}") from exc
        for other in landed:
            if other.change_id == change.change_id or other.target.key == change.target.key:
                continue
            completed = ensure_utc(other.rollout_completed_at)
            rows = await self._read_workload(
                other.target, TimeWindow(start=completed, end=completed + POOL_NEIGHBOUR_LOOKUP)
            )
            if {getattr(row, "node", None) for row in rows} & pool_nodes:
                neighbours.append(other)
        return PoolContext(
            pools=tuple(pools),
            pre=pre_samples,
            post=post_samples,
            other_changes=tuple(neighbours),
            # The read is capped: in a busier cluster later changes are not checked, and the measurement says so.
            truncated=len(landed) >= POOL_MAX_OTHER_CHANGES,
            prices_pre=prices_pre,
            prices_post=prices_post,
        )

    async def _pool_rows(self, window: TimeWindow) -> List[Any]:
        """Every pod's rows in a :data:`POOL_PRICE_SLICE` at each pool sample of ``window`` (price-card control)."""
        rows: List[Any] = []
        moment = ensure_utc(window.start)
        end = ensure_utc(window.end)
        while moment < end:
            try:
                rows.extend(
                    await self.combined_repo.read_combined_metrics_smart(
                        start_time=moment, end_time=min(end, moment + POOL_PRICE_SLICE)
                    )
                    or []
                )
            except Exception as exc:
                raise MeasurementReadError(f"pool metrics at {moment.isoformat()} could not be read: {exc}") from exc
            moment += POOL_SAMPLE_INTERVAL
        return rows

    async def _collect_load(
        self, change: ChangeEvent, window: TimeWindow, buckets: Sequence[Any], *, retry_until: datetime, now: datetime
    ) -> CollectedLoad:
        """Load-driver series of ``window``; raises while a failed Prometheus driver may still be retried."""
        collected = await self.resolver.collect(change, window, buckets)
        if collected.unavailable:
            if now < retry_until:
                raise MeasurementReadError(
                    f"load drivers {', '.join(collected.unavailable)} of change {change.change_id} could not be "
                    f"read; retried until {retry_until.isoformat()}"
                )
            logger.warning(
                "Load drivers %s of change %s stayed unreadable until %s; measuring without them.",
                ", ".join(collected.unavailable),
                change.change_id,
                retry_until.isoformat(),
            )
        return collected

    async def _records(self, ids: Iterable[int]) -> List[RecommendationRecord]:
        records = []
        for rec_id in sorted(set(ids)):
            record = await self.lifecycle.repo.get_recommendation_by_id(rec_id)
            if record is not None and record.id is not None:
                records.append(record)
        return records

    # ------------------------------------------------------------------
    # C1: recording changes (called by the applied detector)
    # ------------------------------------------------------------------

    async def needs_observations(self) -> bool:
        """Whether a detector pass should read workloads even without apply candidates."""
        if not self.enabled:
            return False
        try:
            open_states = [MeasurementState.APPLIED.value, MeasurementState.MEASURING.value]
            return bool(await self.changes.list_statuses(open_states))
        except Exception as exc:
            logger.warning("Could not read open changes: %s", exc)
            return False

    async def observe_workloads(
        self, snapshots: Mapping[Tuple[str, str, str], Any], now: Optional[datetime] = None
    ) -> int:
        """Record the workloads whose pod template (or HPA) differs from their last observation."""
        if not self.enabled or not snapshots:
            return 0
        moment = ensure_utc(now) if now is not None else self._now()
        try:
            latest = await self.changes.latest_spec_observations()
            fresh: List[WorkloadSpecObservation] = []
            for key, snapshot in snapshots.items():
                previous = latest.get(key)
                hpa = getattr(snapshot, "hpa_digest", None)
                if not getattr(snapshot, "hpa_observed", True):
                    # HPAs could not be listed: keep the last known digest rather than record a removal.
                    hpa = previous.hpa_digest if previous is not None else None
                observation = WorkloadSpecObservation(
                    namespace=key[0],
                    kind=key[1],
                    name=key[2],
                    workload_uid=getattr(snapshot, "uid", None),
                    generation=getattr(snapshot, "generation", None),
                    spec_digest=getattr(snapshot, "spec_digest", None),
                    hpa_digest=hpa,
                    resources_digest=getattr(snapshot, "resources_digest", None),
                    image_digest=getattr(snapshot, "image_digest", None),
                    container_resources=container_resources(snapshot),
                    observed_at=moment,
                )
                if previous is None or not previous.same_revision(observation):
                    fresh.append(observation)
            return await self.changes.insert_spec_observations(fresh)
        except Exception as exc:
            logger.warning("Could not record workload spec observations: %s", exc)
            return 0

    async def change_id_for(self, plan_id: Optional[str], snapshot: Any, completed_at: datetime) -> Optional[str]:
        """The change id of a detected rollout: the recorded one, or a new id (``None`` when disabled)."""
        if not self.enabled:
            return None
        key = change_identity(
            plan_id=plan_id,
            workload_uid=getattr(snapshot, "uid", None),
            target=(snapshot.namespace, snapshot.kind, snapshot.name),
            generation_after=getattr(snapshot, "generation", None),
            spec_digest_after=getattr(snapshot, "spec_digest", None),
            rollout_completed_at=completed_at,
        )
        try:
            existing = await self.changes.find_change_id(key)
        except Exception as exc:
            logger.warning("Could not look up change %s: %s", key, exc)
            existing = None
        return existing or uuid.uuid4().hex

    async def record_change(
        self,
        change_id: str,
        records: Sequence[RecommendationRecord],
        *,
        snapshot: Any = None,
        plan: Any = None,
        now: Optional[datetime] = None,
    ) -> Optional[ChangeEvent]:
        """Record the change that applied ``records`` (just transitioned to ``applied``) and freeze its baseline."""
        if not self.enabled or not records:
            return None
        moment = ensure_utc(now) if now is not None else self._now()
        change = await self._build_change(change_id, records, snapshot=snapshot, plan=plan, now=moment)
        stored, created = await self.changes.insert_change(change)
        if created:
            await self._close_superseded(stored, moment)
        try:
            await self._ensure_snapshot(stored, moment)
        except Exception as exc:
            logger.warning("Baseline snapshot of change %s failed; it is retried later: %s", stored.change_id, exc)
        return stored

    async def _build_change(
        self,
        change_id: str,
        records: Sequence[RecommendationRecord],
        *,
        snapshot: Any,
        plan: Any,
        now: datetime,
    ) -> ChangeEvent:
        lead = records[0]
        baseline = _baseline(lead)
        plan_id = getattr(plan, "plan_id", None) or baseline.get("plan_id")
        deleted = deletion_target_for(lead)
        if snapshot is None and deleted is None and self.workload_reader is not None and lead.owner_kind:
            try:
                snapshot = await self.workload_reader(lead.namespace or "", lead.owner_kind, lead.owner_name or "")
            except Exception as exc:
                logger.info("Live workload of change %s could not be read: %s", change_id, exc)
                snapshot = None
        if snapshot is not None:
            target = WorkloadRef(namespace=snapshot.namespace, kind=snapshot.kind, name=snapshot.name)
        elif deleted is not None:
            target = WorkloadRef(namespace=deleted.namespace, kind=deleted.kind, name=deleted.name)
        else:
            target = WorkloadRef(
                namespace=lead.namespace or "",
                kind=lead.owner_kind or "Pod",
                name=lead.owner_name or lead.pod_name or "",
            )
        completed = min(
            (ensure_utc(record.applied_at) for record in records if record.applied_at is not None), default=now
        )
        rollout = baseline.get("rollout") if isinstance(baseline.get("rollout"), dict) else {}
        window_ends = [_parse(_baseline(record).get("baseline_window_end")) for record in records]
        decision = min((value for value in window_ends if value is not None), default=completed)

        uid = getattr(snapshot, "uid", None)
        generation_after = getattr(snapshot, "generation", None)
        digest_after = getattr(snapshot, "spec_digest", None)
        generation_before: Optional[int] = None
        digest_before: Optional[str] = None
        before_source = "unobserved"
        first_seen: Optional[datetime] = None
        try:
            observations = await self.changes.spec_observations(target.namespace, target.kind, target.name)
        except Exception as exc:
            logger.warning("Could not read spec observations of %s: %s", "/".join(target.key), exc)
            observations = []
        for observation in observations:
            same_revision = observation.workload_uid == uid and (
                (digest_after is not None and observation.spec_digest == digest_after)
                or (digest_after is None and observation.generation == generation_after)
            )
            if same_revision:
                if first_seen is None:
                    first_seen = ensure_utc(observation.observed_at)
                continue
            if first_seen is None and (uid is None or observation.workload_uid == uid):
                generation_before = observation.generation
                digest_before = observation.spec_digest
                before_source = "observed"
        pre_apply = getattr(plan, "pre_apply_baseline", None)
        if generation_before is None and pre_apply is not None:
            generation_before = pre_apply.generation
            before_source = "plan_pre_apply_baseline"

        pr = None
        if plan_id is not None and self.pull_request_repo is not None:
            try:
                pr = await self.pull_request_repo.get_pull_request_by_plan_id(plan_id)
            except Exception as exc:
                logger.info("Could not read the pull request of plan %s: %s", plan_id, exc)
        synced_at = ensure_utc(pr.sync_observed_at) if pr is not None and pr.sync_observed_at is not None else None
        # The provider's merge time: GreenKube observes the merge one PR poll later, often after a
        # fast GitOps rollout completed, so the observed "merged" event alone would let the
        # baseline run past the merge into the rollout.
        merged_at = getattr(pr, "merged_at", None) if pr is not None else None
        if isinstance(merged_at, datetime):
            decision = min(decision, ensure_utc(merged_at))

        if first_seen is not None and first_seen <= completed:
            started, started_source = first_seen, "first_observed"
        elif synced_at is not None and synced_at <= completed:
            started, started_source = synced_at, "gitops_synced"
        else:
            started, started_source = completed, "rollout_completed"

        return ChangeEvent(
            change_id=change_id,
            recommendation_ids=sorted(record.id for record in records if record.id is not None),
            plan_id=plan_id,
            pr_url=getattr(pr, "pr_url", None),
            merge_sha=getattr(pr, "merge_commit_sha", None),
            gitops_revision=getattr(pr, "sync_revision", None),
            synced_at=synced_at,
            target=target,
            containers=sorted({record.container_name for record in records if record.container_name}),
            workload_uid=uid,
            generation_before=generation_before,
            generation_after=generation_after,
            spec_digest_before=digest_before,
            spec_digest_after=digest_after,
            resources_digest_after=getattr(snapshot, "resources_digest", None),
            image_digest_after=getattr(snapshot, "image_digest", None),
            rollout_started_at=min(started, completed),
            rollout_started_source=started_source,
            rollout_completed_at=completed,
            rollout_completed_source=str(rollout.get("completion_source") or "unknown"),
            baseline_window_end=min(decision, completed),
            action_kind=getattr(snapshot, "action_kind", None)
            or ("delete_resource" if deleted is not None else "container_resources"),
            application_method=_application_method(lead, plan_id),
            load_metric=getattr(snapshot, "load_metric", None),
            details={
                "spec_before_source": before_source,
                "rollout": rollout,
                **_container_requests(snapshot),
            },
            created_at=now,
        )

    async def _close_superseded(self, change: ChangeEvent, now: datetime) -> None:
        """A new change on a workload closes its earlier realized changes as ``superseded``.

        A superseded change keeps accruing its own measured step (the newer
        change is measured against a baseline that already includes it, so
        nothing is counted twice) and stays watched by the drift monitor: its
        accrual ends when a newer change on the workload ends (regressed,
        reverted, spec drift, workload deleted), or when the workload is
        deleted or drifts while no newer change is realized
        (:meth:`greenkube.verify.drift.DriftMonitor.run`).
        """
        try:
            earlier = await self.changes.changes_for_workload(*change.target.key)
        except Exception as exc:
            logger.warning("Could not read earlier changes of %s: %s", "/".join(change.target.key), exc)
            return
        completed = ensure_utc(change.rollout_completed_at)
        for other in earlier:
            # Only changes that landed before this one: a change recorded late (adopted, manual)
            # never closes a newer realized change.
            if other.change_id == change.change_id or ensure_utc(other.rollout_completed_at) >= completed:
                continue
            status = await self.changes.get_status(other.change_id)
            if status is None or status.state is not MeasurementState.REALIZED:
                continue
            try:
                await self.changes.transition(
                    status,
                    MeasurementState.CLOSED,
                    event_type="superseded",
                    reason=f"change {change.change_id} landed on the same workload",
                    payload={"superseded_by": change.change_id},
                    details={**(status.details or {}), "superseded_by": change.change_id},
                    now=now,
                )
            except ChangeConflictError as exc:
                logger.info("Change %s moved before it could be closed: %s", other.change_id, exc)

    async def _ensure_snapshot(self, change: ChangeEvent, now: datetime) -> BaselineSnapshot:
        existing = await self.changes.get_snapshot_for_change(change.change_id)
        if existing is not None:
            return existing
        return await self.changes.insert_snapshot(await self._build_snapshot(change, now))

    async def _build_snapshot(self, change: ChangeEvent, now: datetime) -> BaselineSnapshot:
        from greenkube import __version__
        from greenkube.impact import IMPACT_MODEL_VERSION

        plan = self._strategy(change).required_windows(change, self.settings)
        rows = await self._read_workload(change.target, plan.pre)
        buckets = hourly_buckets(rows, plan.pre, self._step(), slot_seconds=self.settings.slot_seconds)
        collected = await self._collect_load(
            change, plan.pre, buckets, retry_until=ensure_utc(change.created_at) + LOAD_DRIVER_RETRY, now=now
        )
        load = collected.series
        collectors: Dict[str, Any] = {"greenkube": __version__, "query_step_seconds": self._step()}
        if collected.unavailable:
            collectors[UNAVAILABLE_DRIVERS_KEY] = sorted(collected.unavailable)
        try:
            members = await self._members(change)
        except Exception as exc:
            logger.warning("Members of change %s could not be read for its baseline health: %s", change.change_id, exc)
            members = []
        health = await self._baseline_health(members, plan.pre, buckets)
        sources: Dict[str, int] = {}
        for row in rows:
            source = getattr(row, "intensity_source", None)
            if source:
                sources[str(source)] = sources.get(str(source), 0) + 1
        return BaselineSnapshot(
            snapshot_id=uuid.uuid4().hex,
            change_id=change.change_id,
            window=plan.pre,
            hours=buckets,
            load_series={
                key: {floor_hour(hour).isoformat(): value for hour, value in sorted(series.items())}
                for key, series in load.items()
            },
            price_card=price_card(buckets, getattr(self.config, "CURRENCY", None)),
            health=health,
            intensity_source=max(sources, key=lambda key: sources[key]) if sources else None,
            impact_model_version=IMPACT_MODEL_VERSION,
            collector_versions=collectors,
            created_at=now,
        ).with_digest()

    async def _baseline_health(
        self, members: Sequence[RecommendationRecord], window: TimeWindow, buckets: Sequence[Any]
    ) -> Dict[str, Any]:
        """Health signals of each container over the baseline window, frozen with the snapshot (MEA-07).

        A container whose gates cannot be read is left out (its decision gates
        are then judged on absolute levels, labelled ``baseline_missing``); an
        empty dict means no baseline health at all.
        """
        by_container: Dict[Optional[str], List[RecommendationRecord]] = {}
        for member in members:
            by_container.setdefault(member.container_name, []).append(member)
        gates: Dict[Optional[str], List[HealthGateResult]] = {}
        oom_restarts: Dict[Optional[str], Optional[int]] = {}
        for container, group in sorted(by_container.items(), key=lambda item: item[0] or ""):
            try:
                read, observation = await self.verifier.health_check_for(
                    group, ensure_utc(window.start), ensure_utc(window.end), strict=True
                )
            except Exception as exc:
                logger.warning("Baseline health of %s could not be read: %s", container or "workload", exc)
                continue
            gates[container] = list(read)
            oom_restarts[container] = observation.oom_restart_count if observation is not None else None
        if not gates:
            return {}
        replica_values = [bucket.replicas for bucket in buckets if getattr(bucket, "replicas", None) is not None]
        hours = (ensure_utc(window.end) - ensure_utc(window.start)).total_seconds() / 3600.0
        return freeze_health(
            gates,
            # A bucket is one hour, or one slot of accelerated demo windows (MEA-10).
            replica_hours=math.fsum(replica_values) * self.settings.slot_seconds / 3600.0 if replica_values else None,
            hours=hours,
            oom_restarts=oom_restarts,
        )

    # ------------------------------------------------------------------
    # C2: lifecycle
    # ------------------------------------------------------------------

    async def run_due(self, now: Optional[datetime] = None) -> List[RecommendationRecord]:
        """Advance every open change; returns the recommendations that changed."""
        if not self.enabled:
            return []
        moment = ensure_utc(now) if now is not None else self._now()
        updated: List[RecommendationRecord] = []
        # Applies the change pipeline never recorded (before it shipped, or by hand) join it (MEA-16).
        await self._adopt_unmanaged(moment)
        # Changes with a recommendation awaiting its measurement (decided changes need nothing else).
        awaiting = await self._repair_unrecorded(moment)
        for state, handler, only_awaiting in (
            (MeasurementState.APPLIED, self._start, False),
            (MeasurementState.MEASURING, self._advance, False),
            (MeasurementState.REALIZED, self._settle, True),
            (MeasurementState.NOT_REALIZED, self._settle, True),
            (MeasurementState.INCONCLUSIVE, self._settle, True),
            (MeasurementState.CONTAMINATED, self._settle, True),
        ):
            try:
                statuses = await self.changes.list_statuses([state.value])
            except Exception as exc:
                logger.warning("Could not list %s changes: %s", state.value, exc)
                continue
            for status in statuses:
                if only_awaiting and status.change_id not in awaiting:
                    continue
                try:
                    updated.extend(await handler(status, moment))
                except ChangeConflictError as exc:
                    logger.info("Change %s moved concurrently: %s", status.change_id, exc)
                except Exception as exc:
                    logger.warning("Measurement of change %s failed: %s", status.change_id, exc)
        return updated

    async def _adopt_unmanaged(self, now: datetime) -> int:
        """Give every applied, undecided recommendation without a change id a recorded change (MEA-16).

        Such records (applied before change measurement shipped, or by a client
        that bypassed ``PATCH /apply``) would otherwise be judged by the interim
        verifier. The records of one change (same container, same plan or
        landed together) share one change id, stored on the recommendation so
        the interim verifier leaves them alone; the change row itself is
        written by :meth:`_repair_unrecorded`. Returns the number adopted.
        """
        from greenkube.core.optimization.verifier import attribution_groups, change_id_of

        try:
            records = await self.lifecycle.repo.get_recommendations_by_statuses(sorted(_AWAITING_STATUSES))
        except Exception as exc:
            logger.warning("Could not read applied recommendations: %s", exc)
            return 0
        unmanaged = [
            record
            for record in records or []
            if record.id is not None
            and record.applied_at is not None
            and _awaiting(record)
            and change_id_of(record) is None
        ]
        adopted = 0
        for group in attribution_groups(unmanaged):
            change_id = uuid.uuid4().hex
            for record in group:
                assert record.id is not None
                baseline = {**_baseline(record), "change_id": change_id, "change_adopted_at": now.isoformat()}
                try:
                    await self.lifecycle.repo.update_recommendation_fields(record.id, {"baseline": baseline})
                    adopted += 1
                except Exception as exc:
                    logger.warning("Could not adopt recommendation %s into change %s: %s", record.id, change_id, exc)
        if adopted:
            logger.info("Adopted %d applied recommendation(s) into change measurement.", adopted)
        return adopted

    async def _repair_unrecorded(self, now: datetime) -> set[str]:
        """Record changes whose recommendations were applied but whose change row was never written.

        Returns the ids of the changes that have a recommendation awaiting its measurement.
        """
        try:
            records = await self.lifecycle.repo.get_recommendations_by_statuses(sorted(_AWAITING_STATUSES))
        except Exception as exc:
            logger.warning("Could not read applied recommendations: %s", exc)
            return set()
        linked: Dict[str, List[RecommendationRecord]] = {}
        for record in records or []:
            change_id = _baseline(record).get("change_id")
            if record.id is not None and change_id and _awaiting(record):
                linked.setdefault(str(change_id), []).append(record)
        if not linked:
            return set()
        known = await self.changes.change_ids_for_recommendations(
            [record.id for group in linked.values() for record in group if record.id is not None]
        )
        for change_id, group in linked.items():
            missing = [record for record in group if record.id not in known]
            if not missing:
                continue
            if await self.changes.get_change(change_id) is not None:
                await self.changes.add_members(change_id, [record.id for record in missing if record.id], now)
                continue
            try:
                await self.record_change(change_id, missing, now=now)
            except Exception as exc:
                logger.warning("Could not record change %s: %s", change_id, exc)
        return set(linked)

    async def _members(self, change: ChangeEvent) -> List[RecommendationRecord]:
        return await self._records(change.recommendation_ids)

    async def _co2e_shares(self, measurement: ChangeMeasurement) -> Dict[int, float]:
        """Split of the measured CO2e between the recommendations of a measurement.

        Cost is split by cost component (CPU and memory recommendations each
        take their whole component), but the workload's CO2e is one quantity:
        it is split by each recommendation's projected measurable CO2e, so it
        is never claimed twice.
        """
        ids = sorted({item.recommendation_id for item in measurement.allocations})
        if len(ids) <= 1:
            return {rec_id: 1.0 for rec_id in ids}
        return co2e_shares(await self._records(ids), ids, _co2e_components_of(measurement))

    def _plan(self, change: ChangeEvent, status: ChangeStatus) -> WindowPlan:
        plan = self._strategy(change).required_windows(change, self.settings)
        if status.post_window_start is not None and status.post_window_end is not None:
            plan = plan.model_copy(
                update={
                    "post": TimeWindow(
                        start=ensure_utc(status.post_window_start), end=ensure_utc(status.post_window_end)
                    )
                }
            )
        return plan

    async def _start(self, status: ChangeStatus, now: datetime) -> List[RecommendationRecord]:
        """``applied → measuring``: freeze the baseline (if not yet) and open the post-window."""
        change = await self.changes.get_change(status.change_id)
        if change is None:
            return []
        snapshot = await self._ensure_snapshot(change, now)
        plan = self._strategy(change).required_windows(change, self.settings)
        measuring = await self.changes.transition(
            status,
            MeasurementState.MEASURING,
            event_type="measurement_started",
            payload={
                "pre_window": plan.pre.model_dump(mode="json"),
                "post_window": plan.post.model_dump(mode="json"),
                "baseline_snapshot_id": snapshot.snapshot_id,
                "baseline_sha256": snapshot.content_sha256,
            },
            post_window_start=plan.post.start,
            post_window_end=plan.post.end,
            details={**status.details, "health_checked": False},
            now=now,
        )
        return await self._sync_members(change, measuring)

    async def _sync_members(self, change: ChangeEvent, status: ChangeStatus) -> List[RecommendationRecord]:
        """Show the change's post-window on every member still awaiting its measurement."""
        updated: List[RecommendationRecord] = []
        start, end = status.post_window_start, status.post_window_end
        for member in await self._members(change):
            if not _awaiting(member) or member.id is None:
                continue
            if (
                _value(member.status) == RecommendationStatus.VERIFYING.value
                and member.verification_window_start == start
                and member.verification_window_end == end
            ):
                continue
            try:
                updated.append(
                    await self.lifecycle.transition(
                        member.id,
                        RecommendationStatus.VERIFYING,
                        event_type=RecommendationEventType.VERIFICATION_STARTED,
                        updates={
                            "verification_status": "in_progress",
                            "verification_window_start": start,
                            "verification_window_end": end,
                        },
                        payload={"change_id": change.change_id, "measurement_sequence": status.sequence},
                        expected_statuses=(RecommendationStatus.APPLIED, RecommendationStatus.VERIFYING),
                    )
                )
            except Exception as exc:
                logger.info(
                    "Recommendation %s of change %s not moved to verifying: %s", member.id, change.change_id, exc
                )
        return updated

    async def _advance(self, status: ChangeStatus, now: datetime) -> List[RecommendationRecord]:
        change = await self.changes.get_change(status.change_id)
        if change is None:
            return []
        plan = self._plan(change, status)
        updated = await self._sync_members(change, status)
        health_end = ensure_utc(plan.post.start) + self._health_window()
        if (
            not status.details.get("health_checked")
            and health_end < ensure_utc(plan.post.end)
            and now >= health_end + DECISION_GRACE
        ):
            status, flagged = await self._interim_health(change, status, plan, health_end, now)
            updated.extend(flagged)
        if now >= ensure_utc(plan.post.end) + DECISION_GRACE:
            updated.extend(await self._decide(change, status, plan, now))
        return updated

    async def _health(
        self,
        members: Sequence[RecommendationRecord],
        start: datetime,
        end: datetime,
        *,
        baseline: Optional[Mapping[str, Any]] = None,
    ) -> List[HealthGateResult]:
        """Health gates per container of the change (one failure flags the whole change).

        Raises :class:`MeasurementReadError` when the gates cannot be read, so
        no health check or decision is recorded from missing metrics.
        """
        gates, _ = await self._health_and_reliability(members, start, end, baseline=baseline)
        return gates

    async def _health_and_reliability(
        self,
        members: Sequence[RecommendationRecord],
        start: datetime,
        end: datetime,
        *,
        baseline: Optional[Mapping[str, Any]] = None,
        post_replica_hours: Optional[float] = None,
    ) -> Tuple[List[HealthGateResult], Optional[ReliabilityOutcome]]:
        """Health gates per container and the reliability outcome of memory increases (H1).

        With ``baseline`` (``BaselineSnapshot.health``; empty for snapshots frozen
        before MEA-07) the restart, throttling and readiness gates are judged
        against the baseline window (:func:`compare_with_baseline`).
        """
        by_container: Dict[Optional[str], List[RecommendationRecord]] = {}
        for member in members:
            by_container.setdefault(member.container_name, []).append(member)
        raw: Dict[Optional[str], List[HealthGateResult]] = {}
        oom_restarts: Dict[Optional[str], Optional[int]] = {}
        for container, group in sorted(by_container.items(), key=lambda item: item[0] or ""):
            try:
                gates, health = await self.verifier.health_check_for(group, start, end, strict=True)
            except Exception as exc:
                raise MeasurementReadError(
                    f"health gates of {container or 'workload'} could not be evaluated: {exc}"
                ) from exc
            raw[container] = list(gates)
            oom_restarts[container] = health.oom_restart_count if health is not None else None
        # A memory-request increase made because of OOM kills is judged on that container's kill
        # rate (counted like its evidence), not on "any kill"; its OOM restarts leave the restart
        # gate. Every other container keeps the strict gates (H1).
        judged, reliability, relaxed = increase_reliability_detail(members, raw, start, end, oom_restarts)
        if baseline is not None:
            post_hours = (ensure_utc(end) - ensure_utc(start)).total_seconds() / 3600.0
            excused_baseline = _excused_baseline_restarts(baseline, relaxed)
            judged = {
                container: compare_with_baseline(
                    container_gates,
                    container,
                    baseline,
                    post_hours=post_hours,
                    post_replica_hours=post_replica_hours,
                    max_restart_delta=float(getattr(self.config, "VERIFICATION_MAX_RESTART_DELTA", 0)),
                    max_throttle_ratio=float(getattr(self.config, "VERIFICATION_MAX_THROTTLE_RATIO", 0.05)),
                    min_readiness=float(getattr(self.config, "VERIFICATION_MIN_READINESS", 0.99)),
                    excused_baseline_restarts=excused_baseline,
                )
                for container, container_gates in judged.items()
            }
        gates: List[HealthGateResult] = []
        for container in raw:
            for gate in judged[container]:
                if container and len(by_container) > 1:
                    gate = gate.model_copy(update={"detail": f"[{container}] {gate.detail}".strip()})
                gates.append(gate)
        return gates, reliability

    async def _interim_health(
        self, change: ChangeEvent, status: ChangeStatus, plan: WindowPlan, health_end: datetime, now: datetime
    ) -> Tuple[ChangeStatus, List[RecommendationRecord]]:
        """Evaluate health gates once the interim window elapsed, so a harmful change is flagged early."""
        members = [member for member in await self._members(change) if _awaiting(member)]
        try:
            snapshot = await self.changes.get_snapshot_for_change(change.change_id) if members else None
            gates = (
                await self._health(
                    members,
                    ensure_utc(plan.post.start),
                    health_end,
                    baseline=snapshot.health if snapshot is not None else {},
                )
                if members
                else []
            )
        except MeasurementReadError as exc:
            logger.warning("Interim health check of change %s is retried next pass: %s", change.change_id, exc)
            return status, []
        failed = [gate for gate in gates if gate.status is HealthGateStatus.FAILED]
        flagged: List[RecommendationRecord] = []
        if failed:
            reasons = [gate.detail for gate in failed]
            for member in members:
                assert member.id is not None
                try:
                    flagged.append(
                        await self.lifecycle.mark_rollback_review(
                            member.id,
                            reasons=reasons,
                            verification_window_start=ensure_utc(plan.post.start),
                            verification_window_end=health_end,
                            details={
                                "change_id": change.change_id,
                                "health": [gate.model_dump(mode="json") for gate in gates],
                                "interim": True,
                            },
                        )
                    )
                except Exception as exc:
                    logger.warning("Recommendation %s could not be sent to rollback review: %s", member.id, exc)
            from greenkube.core.observability import record_verification

            record_verification("rollback_review", [gate.gate for gate in failed])
        status = await self.changes.transition(
            status,
            MeasurementState.MEASURING,
            event_type="health_checked",
            reason="; ".join(gate.detail for gate in failed),
            payload={
                "window": {"start": ensure_utc(plan.post.start).isoformat(), "end": health_end.isoformat()},
                "health": [gate.model_dump(mode="json") for gate in gates],
                "failed": bool(failed),
            },
            details={**status.details, "health_checked": True, "health_failed": bool(failed)},
            now=now,
        )
        return status, flagged

    async def _decide(
        self, change: ChangeEvent, status: ChangeStatus, plan: WindowPlan, now: datetime
    ) -> List[RecommendationRecord]:
        """Measure the change once, persist the decision, then apply it to its recommendations."""
        stored = await self.changes.get_measurement_for_cycle(change.change_id, status.sequence)
        members = await self._members(change)
        if stored is None:
            stored = await self._measure(change, status, plan, members, now)
        decided = _OUTCOME_TO_STATE[stored.outcome]
        await self.changes.transition(
            status,
            decided,
            event_type="measurement_decided",
            reason="; ".join(stored.reasons),
            payload={
                "measurement_id": stored.measurement_id,
                "outcome": stored.outcome.value,
                "confidence": stored.confidence,
            },
            latest_measurement_id=stored.measurement_id,
            now=now,
        )
        updated: List[RecommendationRecord] = []
        for member in members:
            if _awaiting(member):
                record = await self._apply_to_member(change, stored, member, status.sequence)
                if record is not None:
                    updated.append(record)
        return updated

    async def _measure(
        self,
        change: ChangeEvent,
        status: ChangeStatus,
        plan: WindowPlan,
        members: Sequence[RecommendationRecord],
        now: datetime,
    ) -> ChangeMeasurement:
        strategy = self._strategy(change)
        snapshot = await self._ensure_snapshot(change, now)
        post_rows = await self._read_workload(change.target, plan.post)
        post_buckets = hourly_buckets(post_rows, plan.post, self._step(), slot_seconds=self.settings.slot_seconds)
        collected = await self._collect_load(
            change,
            plan.post,
            post_buckets,
            retry_until=ensure_utc(plan.post.end) + DECISION_GRACE + LOAD_DRIVER_RETRY,
            now=now,
        )
        post_load = collected.series
        pre_load = {
            key: {utc_hour(hour): float(value) for hour, value in series.items()}
            for key, series in snapshot.load_series.items()
        }
        load = self.resolver.resolve(change, plan.pre, plan.post, pre_load, post_load)
        others = [item for item in await self.changes.changes_for_workload(*change.target.key)]
        observations = await self.changes.spec_observations(*change.target.key, since=plan.pre.start)
        pool = await self._pool_context(change, plan, post_rows)
        confounders = self.detector.detect(
            change,
            plan,
            other_changes=others,
            observations=observations,
            pre=snapshot.hours,
            post=post_buckets,
            load=load,
            pool=pool,
            price_card=snapshot.price_card,
        )
        in_effect = [member for member in members if _value(member.status) in _IN_EFFECT]
        # Measured rows are allocated (projected != allocated != realized): compare with the allocated projection.
        projections = {
            "allocated_cost": _sum(allocated_projection(member) for member in in_effect),
            # Component-matched: only the projected components the measured rows carry.
            "co2e_g": _sum(_measurable_co2e(member) for member in in_effect),
            CO2E_WITH_EMBODIED: _sum(_measurable_co2e(member, CO2E_COMPONENTS) for member in in_effect),
        }
        result = strategy.estimate(
            change,
            plan,
            snapshot.hours,
            post_buckets,
            load=load,
            confounders=confounders,
            projections=projections,
            settings=self.settings,
            seed_parts=(change.change_id, status.sequence),
            rate_sources=[_rate_source(member) for member in in_effect],
            price_card=snapshot.price_card,
        )
        awaiting = [member for member in in_effect if _awaiting(member)]
        gates, reliability = (
            await self._health_and_reliability(
                awaiting,
                ensure_utc(plan.post.start),
                ensure_utc(plan.post.end),
                baseline=snapshot.health,
                post_replica_hours=_replica_hours(post_buckets, self.settings.slot_seconds),
            )
            if awaiting
            else ([], None)
        )
        if reliability is None and not awaiting and change_objective(in_effect) == OBJECTIVE_RELIABILITY:
            # MEA-14: members already flagged by the interim health check still need the fix's outcome.
            _, reliability = await self._health_and_reliability(
                in_effect,
                ensure_utc(plan.post.start),
                ensure_utc(plan.post.end),
                baseline=snapshot.health,
                post_replica_hours=_replica_hours(post_buckets, self.settings.slot_seconds),
            )
        counterfactual_params = dict(result.counterfactual_params)
        # Measured savings are allocated; the realization factor of the projections says how much of
        # them is expected on the bill (realized_billed is not measured yet, C10).
        counterfactual_params["projection_basis"] = PROJECTION_BASIS
        counterfactual_params["realization_factor"] = realization_factor(in_effect)
        reasons = list(result.reasons)
        outcome = result.outcome
        if reliability is not None:
            # The outcome of a reliability increase (H1); its cost stays in the estimates, as a cost.
            counterfactual_params["reliability"] = reliability.as_dict()
            reasons.append(f"reliability ({reliability.outcome.value}): {reliability.reason}")
        # MEA-14: a change whose members all raise a request is judged on its reliability outcome.
        decision = reliability_decision(in_effect, result.outcome, result.estimates, reliability, gates)
        if decision is not None:
            outcome = decision.outcome
            counterfactual_params.update(decision.params)
            reasons = decision.reasons + [reason for reason in reasons if not reason.startswith("reliability (")]
        else:
            counterfactual_params["objective"] = OBJECTIVE_SAVINGS
        estimates = {item.quantity: item for item in result.estimates}
        allocations = []
        for share in allocate(in_effect, result.estimates):
            estimate = estimates.get(share.quantity)
            assert share.record.id is not None
            allocations.append(
                Allocation(
                    recommendation_id=share.record.id,
                    basis=share.basis,
                    share=round(share.share, 6),
                    quantity=share.quantity,
                    annualized=estimate.annualized * share.share
                    if estimate is not None and estimate.annualized is not None
                    else None,
                )
            )
        previous = await self.changes.list_measurements(change.change_id)
        measurement = ChangeMeasurement(
            measurement_id=uuid.uuid4().hex,
            change_id=change.change_id,
            sequence=status.sequence,
            strategy_key=strategy.key,
            strategy_version=result.strategy_version or strategy.version,
            impact_model_version=snapshot.impact_model_version,
            baseline_snapshot_id=snapshot.snapshot_id,
            baseline_sha256=snapshot.content_sha256,
            windows=plan,
            load_driver=load.info if load is not None else None,
            counterfactual_params=_with_unavailable_drivers(
                counterfactual_params, snapshot.collector_versions.get(UNAVAILABLE_DRIVERS_KEY), collected
            ),
            estimates=result.estimates,
            confounders=confounders,
            confidence=confidence_score(confounders, result.coverage, load),
            health=gates,
            allocations=allocations,
            outcome=outcome,
            reasons=reasons,
            supersedes_measurement_id=previous[-1].measurement_id if previous else None,
            decided_at=now,
            # WP-08.1: released capacity and its verdict, from the specs alone (any cost outcome).
            capacity=measure_capacity(
                change,
                observations,
                snapshot.hours,
                post_buckets,
                in_effect,
                post_end=ensure_utc(plan.post.end),
                slot_seconds=self.settings.slot_seconds,
            ),
        ).with_evidence_digest()
        stored, _ = await self.changes.insert_measurement(measurement)
        return stored

    async def _settle(self, status: ChangeStatus, now: datetime) -> List[RecommendationRecord]:
        """Decided change: start a requested re-measurement, or apply the decision to late members."""
        change = await self.changes.get_change(status.change_id)
        if change is None:
            return []
        members = await self._members(change)
        awaiting = [member for member in members if _awaiting(member)]
        if not awaiting:
            return []
        requested = [
            member
            for member in awaiting
            if _value(member.status) == RecommendationStatus.VERIFYING.value and _sequence(member) > status.sequence
        ]
        if requested and status.state in _REMEASURABLE:
            post_start = floor_hour(now) + timedelta(hours=1)
            measuring = await self.changes.transition(
                status,
                MeasurementState.MEASURING,
                event_type="remeasure_started",
                reason="explicit re-measurement requested",
                payload={"requested_by": sorted(member.id for member in requested if member.id is not None)},
                sequence=max(_sequence(member) for member in requested),
                post_window_start=post_start,
                post_window_end=post_start + timedelta(days=self.settings.post_window_days),
                details={**status.details, "health_checked": False},
                now=now,
            )
            return await self._sync_members(change, measuring)
        if status.latest_measurement_id is None:
            return []
        stored = await self.changes.get_measurement_for_cycle(change.change_id, status.sequence)
        if stored is None:
            return []
        # A realized change is measured once (it ends regressed or closed): a re-measurement
        # requested by one of its recommendations is answered with the existing decision.
        declined = (
            f"re-measurement not started: change {change.change_id} is {status.state.value} and is measured once"
            if requested
            else None
        )
        requested_ids = {member.id for member in requested}
        updated: List[RecommendationRecord] = []
        for member in awaiting:
            record = await self._apply_to_member(
                change,
                stored,
                member,
                status.sequence,
                note=declined if member.id in requested_ids else None,
            )
            if record is not None:
                updated.append(record)
        return updated

    # ------------------------------------------------------------------
    # Adapter: recommendation lifecycle and per-recommendation rows (C5)
    # ------------------------------------------------------------------

    async def _apply_to_member(
        self,
        change: ChangeEvent,
        measurement: ChangeMeasurement,
        member: RecommendationRecord,
        change_sequence: int,
        *,
        note: Optional[str] = None,
    ) -> Optional[RecommendationRecord]:
        assert member.id is not None
        sequence = _sequence(member)
        baseline = _baseline(member)
        previous_ids = [str(item) for item in baseline.get("measurement_ids") or []]
        stored: Optional[SavingsMeasurement] = None
        if self.measurement_repo is not None:
            stored = await self.measurement_repo.get_for_cycle(member.id, sequence)
        if stored is None:
            row = self._member_measurement(
                change,
                measurement,
                member,
                sequence,
                gates_apply=sequence == change_sequence,
                previous=previous_ids[-1] if previous_ids else None,
                note=note,
                co2e_shares=await self._co2e_shares(measurement),
            )
            stored = await self.measurement_repo.insert(row) if self.measurement_repo is not None else row
        else:
            previous_ids = [item for item in previous_ids if item != stored.measurement_id]
        cost = next((item for item in stored.estimates if item.quantity == "cost"), None)
        co2e = next((item for item in stored.estimates if item.quantity == "co2e_g"), None)
        measured_cost: Optional[float]
        if stored.outcome is MeasurementOutcome.INCONCLUSIVE or cost is None:
            measured_cost = None
        elif stored.outcome is MeasurementOutcome.REALIZED:
            measured_cost = cost.claimed_annualized
        else:
            measured_cost = cost.annualized
        measured_co2e: Optional[float]
        if stored.outcome is MeasurementOutcome.INCONCLUSIVE or co2e is None:
            measured_co2e = None
        elif stored.outcome is MeasurementOutcome.REALIZED:
            measured_co2e = co2e.claimed_annualized
        else:
            measured_co2e = co2e.annualized
        if measurement.counterfactual_params.get("representative") is False:
            # Accelerated demo verdict (MEA-10): marked so its saving never accrues in the ledger.
            baseline = {**baseline, REPRESENTATIVE_KEY: False}
        try:
            return await self.verifier.apply_stored_measurement(
                member, stored, baseline, previous_ids, measured_cost=measured_cost, measured_co2e=measured_co2e
            )
        except Exception as exc:
            logger.warning(
                "Measurement %s could not be applied to recommendation %s: %s",
                measurement.measurement_id,
                member.id,
                exc,
            )
            return None

    def _member_measurement(
        self,
        change: ChangeEvent,
        measurement: ChangeMeasurement,
        member: RecommendationRecord,
        sequence: int,
        *,
        gates_apply: bool,
        previous: Optional[str],
        note: Optional[str] = None,
        co2e_shares: Optional[Mapping[int, float]] = None,
    ) -> SavingsMeasurement:
        assert member.id is not None
        allocation = next((item for item in measurement.allocations if item.recommendation_id == member.id), None)
        failed = [gate for gate in measurement.health if gate.status is HealthGateStatus.FAILED] if gates_apply else []
        reasons = list(measurement.reasons)
        # MEA-14: a reliability fix is verified on its reliability outcome; its cost is never claimed.
        reliability = measurement.counterfactual_params.get("objective") == OBJECTIVE_RELIABILITY
        if allocation is None:
            outcome = MeasurementOutcome.INCONCLUSIVE
            reasons = ["recommendation joined the change after it was measured"]
            estimate: Optional[SavingsEstimate] = None
            share = 0.0
            basis = "none"
        else:
            outcome = _OUTCOME_TO_RECOMMENDATION[measurement.outcome]
            estimate = measurement.estimate(allocation.quantity)
            share = allocation.share
            basis = allocation.basis
            if (
                not reliability
                and measurement.outcome is ChangeOutcome.REALIZED
                and estimate is not None
                and not estimate.claimed
                and estimate.outcome is not ChangeOutcome.REALIZED
            ):
                # The share comes from a cost component (e.g. ``cost_ram``): the recommendation
                # claims it only when that component's own interval shows a saving.
                outcome = _OUTCOME_TO_RECOMMENDATION.get(estimate.outcome, MeasurementOutcome.INCONCLUSIVE)
                reasons = [estimate.reason or f"{allocation.quantity} saving not realized", *reasons]
        if measurement.outcome is ChangeOutcome.CONTAMINATED:
            reasons = [f"contaminated: {reason}" for reason in reasons] or ["contaminated"]
        if note:
            reasons = [note, *reasons]
        if failed:
            outcome = MeasurementOutcome.ROLLBACK_REVIEW
            reasons = [gate.detail for gate in failed]
        cost = _quantity_estimate(
            "cost", "currency", basis, share, estimate, outcome, allocated_projection(member), claimed=not reliability
        )
        co2e_source = measurement.estimate("co2e_g")
        # The measured CO2e (operational, allocated by the Impact Model like the
        # projection) is claimed only when its own interval shows a saving, and
        # is compared with the projected components the measured rows carry.
        co2e_outcome = outcome
        if (
            not reliability
            and outcome is MeasurementOutcome.REALIZED
            and (co2e_source is None or co2e_source.outcome is not ChangeOutcome.REALIZED)
        ):
            co2e_outcome = (
                _OUTCOME_TO_RECOMMENDATION.get(co2e_source.outcome, MeasurementOutcome.INCONCLUSIVE)
                if co2e_source is not None
                else MeasurementOutcome.INCONCLUSIVE
            )
        co2e_share = share if co2e_shares is None else co2e_shares.get(member.id, 0.0)
        co2e_basis = basis if co2e_shares is None or allocation is None else _co2e_basis(co2e_shares)
        co2e = _quantity_estimate(
            "co2e_g",
            "gCO2e",
            co2e_basis,
            co2e_share,
            co2e_source,
            co2e_outcome,
            _measurable_co2e(member, _co2e_components_of(measurement)),
            claimed=not reliability,
        )
        windows = {
            "baseline": {
                "start": measurement.windows.pre.start.isoformat(),
                "end": measurement.windows.pre.end.isoformat(),
            },
            "after": {
                "start": measurement.windows.post.start.isoformat(),
                "end": measurement.windows.post.end.isoformat(),
            },
        }
        ledger = MeasuredLedgerInput(
            before_cost_per_hour=cost.before_per_hour,
            after_cost_per_hour=cost.after_per_hour,
            before_co2e_grams_per_hour=co2e.before_per_hour,
            after_co2e_grams_per_hour=co2e.after_per_hour,
            sample_count=estimate.aligned_hours if estimate is not None else 0,
            outcome=outcome.value,
            cost_outcome=cost.outcome.value,
            co2e_outcome=co2e.outcome.value,
            cost_basis=basis,
            measurement_scope="workload",
            baseline_window_start=measurement.windows.pre.start,
            baseline_window_end=measurement.windows.pre.end,
            after_window_start=measurement.windows.post.start,
            after_window_end=measurement.windows.post.end,
            group_id=measurement.measurement_id,
        )
        row = SavingsMeasurement(
            measurement_id=uuid.uuid4().hex,
            recommendation_id=member.id,
            group_id=measurement.measurement_id,
            group_recommendation_ids=sorted(item.recommendation_id for item in measurement.allocations),
            plan_id=change.plan_id,
            change_id=change.change_id,
            sequence=sequence,
            strategy_key=measurement.strategy_key,
            strategy_version=measurement.strategy_version,
            impact_model_version=measurement.impact_model_version,
            baseline_snapshot_id=measurement.baseline_snapshot_id,
            scope="workload",
            windows=windows,
            estimates=[cost, co2e],
            health=measurement.health,
            confounders=[item.model_dump(mode="json") for item in measurement.confounders],
            outcome=outcome,
            reasons=reasons,
            supersedes_measurement_id=previous,
            decided_at=measurement.decided_at,
            created_by="change-measurement",
        )
        ledger.measurement_id = row.measurement_id
        return row.model_copy(update={"ledger_input": ledger.model_dump(mode="json")}).with_evidence_digest()

    # ------------------------------------------------------------------
    # C8 hook
    # ------------------------------------------------------------------

    async def mark_regressed(self, change_id: str, *, reason: str, actor: str = "system") -> Optional[ChangeStatus]:
        """``realized → regressed`` (to be driven by the drift monitor, C8)."""
        status = await self.changes.get_status(change_id)
        if status is None or status.state is not MeasurementState.REALIZED:
            return None
        return await self.changes.transition(
            status, MeasurementState.REGRESSED, event_type="regressed", actor=actor, reason=reason, now=self._now()
        )


def _with_unavailable_drivers(params: Mapping[str, Any], pre: Any, post: CollectedLoad) -> Dict[str, Any]:
    """Counterfactual parameters, naming the load drivers that could not be read in either window."""
    unavailable = {
        "pre": sorted(str(item) for item in pre or []),
        "post": sorted(post.unavailable),
    }
    if not unavailable["pre"] and not unavailable["post"]:
        return dict(params)
    return {**params, UNAVAILABLE_DRIVERS_KEY: unavailable}


def co2e_shares(
    records: Sequence[RecommendationRecord],
    ids: Sequence[int],
    components: Sequence[str] = MEASURED_CO2E_COMPONENTS,
) -> Dict[int, float]:
    """Shares (summing to 1) of ``ids`` by projected measurable CO2e; equal when nothing was projected."""
    by_id = {record.id: record for record in records}
    weights = {}
    for rec_id in ids:
        record = by_id.get(rec_id)
        value = _measurable_co2e(record, components) if record is not None else None
        weights[rec_id] = max(value or 0.0, 0.0)
    total = math.fsum(weights.values())
    if total <= 0:
        return {rec_id: 1.0 / len(ids) for rec_id in ids} if ids else {}
    return {rec_id: weight / total for rec_id, weight in weights.items()}


def _co2e_basis(shares: Mapping[int, float]) -> str:
    return "projected_co2e_share" if len(shares) > 1 else "total"


def _measurable_co2e(
    member: RecommendationRecord, components: Sequence[str] = MEASURED_CO2E_COMPONENTS
) -> Optional[float]:
    """Projected allocated CO2e of the components a measurement sees (None: nothing comparable)."""
    return measurable_co2e_projection(getattr(member, "impact", None), components, allocated=True)


def _co2e_components_of(measurement: ChangeMeasurement) -> Sequence[str]:
    """Components the measured CO2e covers (operational only for measurements before embodied was measured)."""
    estimate = measurement.estimate("co2e_g")
    return tuple(estimate.components) if estimate is not None and estimate.components else MEASURED_CO2E_COMPONENTS


#: What a measurement is compared with (projected != allocated != realized).
PROJECTION_BASIS = "allocated"


def _factor(record: RecommendationRecord) -> Optional[float]:
    impact = record.impact if isinstance(record.impact, dict) else {}
    value = impact.get("realization_factor")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return min(max(float(value), 0.0), 1.0)


def realization_factor(records: Sequence[RecommendationRecord]) -> Optional[float]:
    """Realization factor of a group's projections, weighted by their allocated saving (None when one is unknown)."""
    pairs = []
    for record in records:
        factor = _factor(record)
        if factor is None:
            return None
        pairs.append((max(float(allocated_projection(record) or 0.0), 0.0), factor))
    if not pairs:
        return None
    total = math.fsum(weight for weight, _ in pairs)
    if total <= 0:
        return math.fsum(factor for _, factor in pairs) / len(pairs)
    return math.fsum(weight * factor for weight, factor in pairs) / total


def _rate_source(member: RecommendationRecord) -> str:
    """Provenance of the rate frozen in a recommendation's projection (``assumptions.cost_source``)."""
    impact = member.impact if isinstance(member.impact, dict) else {}
    assumptions = impact.get("assumptions") if isinstance(impact.get("assumptions"), dict) else {}
    return str(assumptions.get("cost_source") or "unknown")


def _sum(values: Iterable[Optional[float]]) -> Optional[float]:
    items = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return math.fsum(items) if items else None


def _excused_baseline_restarts(baseline: Mapping[str, Any], relaxed: Iterable[Optional[str]]) -> Optional[float]:
    """Baseline OOM restarts of the containers whose post-window OOM restarts left the restart gates.

    The post-window restart gates of a memory increase judged on its OOM-kill
    rate leave out the OOM restarts of the relaxed containers; the baseline count
    must leave out the same restarts (MEA-07 review). ``None`` when one of them
    was not frozen (the comparison then keeps the absolute rule, labelled).
    """
    containers = baseline.get(CONTAINERS_KEY) or {}
    total = 0.0
    for container in relaxed:
        value = (containers.get(container_key(container)) or {}).get(OOM_RESTARTS_KEY)
        if value is None:
            return None
        total += float(value)
    return total


def container_resources(snapshot: Any) -> Optional[Dict[str, Dict[str, Optional[int]]]]:
    """Per-container requests and limits of a workload snapshot (WP-08.1); ``None`` when unknown or invalid."""
    requests = getattr(snapshot, "container_request_values", None)
    if (
        not isinstance(requests, Mapping)
        or not requests
        or not getattr(snapshot, "container_request_values_valid", True)
    ):
        return None
    limits = getattr(snapshot, "container_limit_values", None)
    limits = limits if isinstance(limits, Mapping) else {}
    kinds = getattr(snapshot, "container_kinds", None)
    kinds = kinds if isinstance(kinds, Mapping) else {}
    resources: Dict[str, Dict[str, Optional[int]]] = {}
    for name, (cpu, memory) in sorted(requests.items()):
        cpu_limit, memory_limit = limits.get(name, (None, None))
        resources[str(name)] = {
            "cpu_request_millicores": cpu,
            "memory_request_bytes": memory,
            "cpu_limit_millicores": cpu_limit,
            "memory_limit_bytes": memory_limit,
        }
        if name in kinds:
            # Classic init containers are left out of the released capacity (greenkube.verify.capacity).
            resources[str(name)]["kind"] = kinds[name]
    return resources


def _container_requests(snapshot: Any) -> Dict[str, Any]:
    """``{"container_requests": {container: [cpu millicores, memory bytes]}}`` of a snapshot (empty when unknown)."""
    values = getattr(snapshot, "container_request_values", None)
    if not values or not getattr(snapshot, "container_request_values_valid", True):
        return {}
    return {"container_requests": {str(name): list(requests) for name, requests in sorted(values.items())}}


def _replica_hours(buckets: Sequence[Any], slot_seconds: int = 3600) -> Optional[float]:
    """Replica-hours of ``buckets``: their replica counts times the bucket length (``None`` without any)."""
    values = [bucket.replicas for bucket in buckets if getattr(bucket, "replicas", None) is not None]
    return math.fsum(values) * slot_seconds / 3600.0 if values else None


def _quantity_estimate(
    quantity: str,
    unit: str,
    basis: str,
    share: float,
    estimate: Optional[SavingsEstimate],
    outcome: MeasurementOutcome,
    projected: Optional[float],
    *,
    claimed: bool = True,
) -> QuantityEstimate:
    """A recommendation's share of a change estimate, in the per-recommendation measurement shape."""
    values: dict[str, Any] = {
        "quantity": quantity,
        "unit": unit,
        "basis": basis,
        "share": share,
        "projected_annual": projected,
    }
    if estimate is None or estimate.rate_per_hour is None:
        return QuantityEstimate(
            **values, outcome=QuantityOutcome.INCONCLUSIVE if claimed else QuantityOutcome.NOT_CLAIMED
        )
    annualized = (estimate.annualized or 0.0) * share
    if not claimed:
        quantity_outcome = QuantityOutcome.NOT_CLAIMED
    elif outcome is MeasurementOutcome.REALIZED:
        quantity_outcome = QuantityOutcome.REALIZED
    elif outcome is MeasurementOutcome.NOT_REALIZED:
        quantity_outcome = QuantityOutcome.NOT_REALIZED
    else:
        quantity_outcome = (
            QuantityOutcome.INCONCLUSIVE
            if outcome is MeasurementOutcome.INCONCLUSIVE
            else _quantity_outcome(estimate.outcome)
        )
    hours = float(estimate.aligned_hours)
    return QuantityEstimate(
        **values,
        before_per_hour=(estimate.counterfactual_per_hour or 0.0) * share,
        after_per_hour=(estimate.actual_per_hour or 0.0) * share,
        delta_per_hour=estimate.rate_per_hour * share,
        annualized=annualized,
        claimed_annualized=annualized if quantity_outcome is QuantityOutcome.REALIZED else 0.0,
        before_coverage_steps=hours,
        after_coverage_steps=hours,
        outcome=quantity_outcome,
        projection_accuracy=annualized / projected if projected else None,
    )


def _quantity_outcome(outcome: ChangeOutcome) -> QuantityOutcome:
    return {
        ChangeOutcome.REALIZED: QuantityOutcome.REALIZED,
        ChangeOutcome.NOT_REALIZED: QuantityOutcome.NOT_REALIZED,
    }.get(outcome, QuantityOutcome.INCONCLUSIVE)


__all__ = ["ChangeMeasurementService", "DECISION_GRACE", "PROJECTION_BASIS", "realization_factor"]
