# src/greenkube/demo/changes.py
"""Measured changes for the demo: applied rightsizings with their measurement and capacity verdict.

The demo has no cluster and no GitOps controller, so the change records are
synthetic. Their measured released capacity is not: it is computed by the
production code (:func:`greenkube.verify.capacity.measure_capacity`) from
synthetic workload spec observations, so the change page shows the same
verdicts (``released``, ``partially_released``) a real cluster would.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from greenkube.models.metrics import RecommendationRecord, RecommendationStatus
from greenkube.verify.capacity import measure_capacity
from greenkube.verify.model import (
    Allocation,
    ChangeApplicationMethod,
    ChangeEvent,
    ChangeMeasurement,
    ChangeOutcome,
    HourBucket,
    MeasurementState,
    SavingsEstimate,
    TimeWindow,
    WindowPlan,
    WorkloadRef,
    WorkloadSpecObservation,
)

logger = logging.getLogger(__name__)

MIB = 1024 * 1024
#: Container name of the demo workloads' main container.
DEMO_CONTAINER = "app"
#: Pre- and post-window length of the demo measurements (the production default is 7 days each).
_WINDOW = timedelta(days=7)
_WARMUP = timedelta(hours=24)


@dataclass(frozen=True)
class DemoChange:
    """One applied demo change: which recommendation, the requests before and after, and its cost result."""

    pod_name: str
    namespace: str
    owner_name: str
    cpu_before_m: Optional[int]
    cpu_after_m: Optional[int]
    memory_before_bytes: Optional[int]
    memory_after_bytes: Optional[int]
    #: Measured allocated saving per year and its 90% interval (USD).
    measured_annual: float
    ci_low: float
    ci_high: float
    projected_annual: float
    outcome: ChangeOutcome
    repository: str
    pr_number: int
    #: How far the measurement went: ``applied`` (warm-up), ``measuring`` (window open) or ``decided``.
    progress: str = "decided"
    #: Replicas before and after the change (an HPA may scale out after a smaller request).
    replicas_before: float = 1.0
    replicas_after: float = 1.0
    #: A realized saving the drift monitor later found regressed (C8): when, and why.
    regressed_days_ago: Optional[float] = None
    regressed_reason: Optional[str] = None


DEMO_CHANGES: tuple[DemoChange, ...] = (
    DemoChange(
        pod_name="pricing-service-0",
        namespace="commerce",
        owner_name="pricing-service",
        cpu_before_m=900,
        cpu_after_m=550,
        memory_before_bytes=512 * MIB,
        memory_after_bytes=512 * MIB,
        measured_annual=88.0,
        ci_low=71.0,
        ci_high=104.0,
        projected_annual=95.0,
        outcome=ChangeOutcome.REALIZED,
        repository="greenoptic/platform-gitops",
        pr_number=412,
    ),
    DemoChange(
        pod_name="api-gateway-0",
        namespace="platform",
        owner_name="api-gateway",
        # The pull request proposed 800m; the team merged 900m: a partial release.
        cpu_before_m=1200,
        cpu_after_m=900,
        memory_before_bytes=768 * MIB,
        memory_after_bytes=768 * MIB,
        measured_annual=41.0,
        ci_low=-12.0,
        ci_high=93.0,
        projected_annual=109.0,
        outcome=ChangeOutcome.INCONCLUSIVE,
        repository="greenoptic/platform-gitops",
        pr_number=398,
    ),
    DemoChange(
        pod_name="lens-configurator-0",
        namespace="manufacturing",
        owner_name="lens-configurator",
        cpu_before_m=None,
        cpu_after_m=None,
        memory_before_bytes=1024 * MIB,
        memory_after_bytes=768 * MIB,
        measured_annual=9.4,
        ci_low=7.8,
        ci_high=11.1,
        projected_annual=9.2,
        outcome=ChangeOutcome.REALIZED,
        repository="greenoptic/manufacturing-apps",
        pr_number=57,
    ),
    DemoChange(
        pod_name="stock-reservation-0",
        namespace="retail",
        owner_name="stock-reservation",
        cpu_before_m=350,
        cpu_after_m=200,
        memory_before_bytes=512 * MIB,
        memory_after_bytes=512 * MIB,
        measured_annual=0.0,
        ci_low=0.0,
        ci_high=0.0,
        projected_annual=0.0,
        outcome=ChangeOutcome.INCONCLUSIVE,
        repository="greenoptic/platform-gitops",
        pr_number=435,
        progress="applied",
        replicas_before=2.0,
        replicas_after=2.0,
    ),
    DemoChange(
        pod_name="notification-service-0",
        namespace="platform",
        owner_name="notification-service",
        cpu_before_m=300,
        cpu_after_m=150,
        memory_before_bytes=384 * MIB,
        memory_after_bytes=384 * MIB,
        measured_annual=0.0,
        ci_low=0.0,
        ci_high=0.0,
        projected_annual=0.0,
        outcome=ChangeOutcome.INCONCLUSIVE,
        repository="greenoptic/platform-gitops",
        pr_number=429,
        progress="measuring",
        replicas_before=2.0,
        replicas_after=2.0,
    ),
    DemoChange(
        pod_name="pos-sync-0",
        namespace="retail",
        owner_name="pos-sync",
        # 400m -> 250m per replica, but the HPA (60% of the request) went from 2 to 3 replicas.
        cpu_before_m=400,
        cpu_after_m=250,
        memory_before_bytes=512 * MIB,
        memory_after_bytes=512 * MIB,
        measured_annual=-7.4,
        ci_low=-14.0,
        ci_high=-0.8,
        projected_annual=29.0,
        outcome=ChangeOutcome.NOT_REALIZED,
        repository="greenoptic/platform-gitops",
        pr_number=419,
        replicas_before=2.0,
        replicas_after=3.0,
    ),
    DemoChange(
        pod_name="factory-scheduler-0",
        namespace="manufacturing",
        owner_name="factory-scheduler",
        cpu_before_m=None,
        cpu_after_m=None,
        memory_before_bytes=512 * MIB,
        memory_after_bytes=384 * MIB,
        measured_annual=4.6,
        ci_low=3.9,
        ci_high=5.3,
        projected_annual=4.6,
        outcome=ChangeOutcome.REALIZED,
        repository="greenoptic/manufacturing-apps",
        pr_number=61,
        regressed_days_ago=6,
        regressed_reason=(
            "working set above the new request since release 3.8 (P95 401Mi for a 384Mi request); 2 OOM kills in 24h"
        ),
    ),
)

_STATE_BY_OUTCOME = {
    ChangeOutcome.REALIZED: MeasurementState.REALIZED,
    ChangeOutcome.NOT_REALIZED: MeasurementState.NOT_REALIZED,
    ChangeOutcome.INCONCLUSIVE: MeasurementState.INCONCLUSIVE,
    ChangeOutcome.CONTAMINATED: MeasurementState.CONTAMINATED,
}

_REASON_BY_OUTCOME = {
    ChangeOutcome.REALIZED: "the 90% interval is above zero",
    ChangeOutcome.NOT_REALIZED: "the 90% interval is at or below zero: the allocated cost did not fall",
    ChangeOutcome.INCONCLUSIVE: "the 90% interval includes zero",
    ChangeOutcome.CONTAMINATED: "a confounding change landed in the measurement window",
}

#: A demo rollout lasts 20 minutes; the GitOps controller synced the merge 2 minutes before it started.
ROLLOUT_DURATION = timedelta(minutes=20)
SYNC_LEAD = timedelta(minutes=2)
#: The pull request was merged this long before its rollout completed.
MERGE_LEAD = timedelta(minutes=45)
#: Git host of the demo repositories (fictional).
DEMO_GIT_HOST = "git.greenoptic.example"


def _digest(*parts: object) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()


def merge_sha(repository: str, pr_number: int) -> str:
    """The merge commit of a demo pull request (stable for a repository and number)."""
    return _digest("merge", repository, pr_number)[:40]


def pull_request_url(repository: str, pr_number: int) -> str:
    """Web URL of a demo pull request on the fictional Gitea host."""
    return f"https://{DEMO_GIT_HOST}/{repository}/pulls/{pr_number}"


def rollout_completed_at(applied_at: datetime) -> datetime:
    """Rollout completion of a change applied at ``applied_at`` (whole hours, as the change records)."""
    return applied_at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _resources(cpu_m: Optional[int], memory_bytes: Optional[int]) -> dict[str, Any]:
    return {DEMO_CONTAINER: {"cpu_request_millicores": cpu_m, "memory_request_bytes": memory_bytes}}


def _buckets(start: datetime, hours: int, replicas: float) -> list[HourBucket]:
    return [HourBucket(hour=start + timedelta(hours=index), coverage=1.0, replicas=replicas) for index in range(hours)]


def build_demo_change(
    spec: DemoChange, member: RecommendationRecord, *, plan_id: Optional[str] = None
) -> tuple[ChangeEvent, Optional[ChangeMeasurement], MeasurementState]:
    """The change record, its decided measurement (with the measured capacity) and its current state.

    ``plan_id`` is the automation plan that delivered the change; a change still in
    its warm-up or measurement window has no decided measurement yet (``None``).
    """
    if member.id is None or member.applied_at is None:
        raise ValueError(f"demo change {spec.pod_name}: its recommendation has no id or applied_at")
    completed = rollout_completed_at(member.applied_at)
    started = completed - ROLLOUT_DURATION
    digest_before = _digest(spec.owner_name, spec.cpu_before_m, spec.memory_before_bytes)
    digest_after = _digest(spec.owner_name, spec.cpu_after_m, spec.memory_after_bytes)
    merge_commit = merge_sha(spec.repository, spec.pr_number)
    change = ChangeEvent(
        change_id=f"demo-{_digest('change', spec.namespace, spec.owner_name)[:24]}",
        recommendation_ids=[member.id],
        plan_id=plan_id or f"demo-plan-{spec.pr_number}",
        pr_url=pull_request_url(spec.repository, spec.pr_number),
        merge_sha=merge_commit,
        gitops_revision=merge_commit,
        synced_at=started - SYNC_LEAD,
        target=WorkloadRef(namespace=spec.namespace, kind="Deployment", name=spec.owner_name),
        containers=[DEMO_CONTAINER],
        generation_before=41,
        generation_after=42,
        spec_digest_before=digest_before,
        spec_digest_after=digest_after,
        rollout_started_at=started,
        rollout_started_source="replicaset",
        rollout_completed_at=completed,
        rollout_completed_source="deployment_status",
        baseline_window_end=started,
        application_method=ChangeApplicationMethod.GITOPS_PR,
        details={"container_requests": {DEMO_CONTAINER: [spec.cpu_after_m, spec.memory_after_bytes]}},
        created_at=completed,
        created_by="demo",
    )
    if spec.progress == "applied":
        return change, None, MeasurementState.APPLIED
    if spec.progress == "measuring":
        return change, None, MeasurementState.MEASURING
    observations = [
        WorkloadSpecObservation(
            namespace=spec.namespace,
            kind="Deployment",
            name=spec.owner_name,
            generation=41,
            spec_digest=digest_before,
            container_resources=_resources(spec.cpu_before_m, spec.memory_before_bytes),
            observed_at=started - timedelta(days=3),
        ),
        WorkloadSpecObservation(
            namespace=spec.namespace,
            kind="Deployment",
            name=spec.owner_name,
            generation=42,
            spec_digest=digest_after,
            container_resources=_resources(spec.cpu_after_m, spec.memory_after_bytes),
            observed_at=completed,
        ),
    ]
    pre, post = measurement_windows(completed)
    hours = int(_WINDOW.total_seconds() // 3600)
    capacity = measure_capacity(
        change,
        observations,
        _buckets(pre.start, hours, spec.replicas_before),
        _buckets(post.start, hours, spec.replicas_after),
        [member],
        post_end=post.end,
    )
    measurement_id = uuid.uuid5(uuid.NAMESPACE_URL, change.change_id).hex
    reason = _REASON_BY_OUTCOME[spec.outcome]
    estimate = SavingsEstimate(
        quantity="allocated_cost",
        unit="USD",
        claimed=spec.outcome is ChangeOutcome.REALIZED,
        rate_per_hour=spec.measured_annual / 8760.0,
        ci_low=spec.ci_low / 8760.0,
        ci_high=spec.ci_high / 8760.0,
        annualized=spec.measured_annual,
        annualized_ci_low=spec.ci_low,
        annualized_ci_high=spec.ci_high,
        coverage=0.97,
        aligned_hours=hours,
        method="ols_load_seasonal",
        outcome=spec.outcome,
        reason=reason,
        projected_annual=spec.projected_annual,
        projection_accuracy=round(spec.measured_annual / spec.projected_annual, 3),
    )
    measurement = ChangeMeasurement(
        measurement_id=measurement_id,
        change_id=change.change_id,
        strategy_key="request_rightsizing",
        strategy_version="2.5",
        windows=WindowPlan(pre=pre, post=post, warmup_hours=_WARMUP.total_seconds() / 3600, min_coverage=0.8),
        counterfactual_params={"projection_basis": "allocated", "realization_factor": 0.7, "demo": True},
        estimates=[estimate],
        confidence=0.86 if spec.outcome is ChangeOutcome.REALIZED else 0.52,
        allocations=[
            Allocation(
                recommendation_id=member.id,
                basis="sole_member",
                share=1.0,
                quantity="allocated_cost",
                annualized=spec.measured_annual,
                measurement_id=measurement_id,
            )
        ],
        outcome=spec.outcome,
        reasons=[reason],
        decided_at=post.end,
        capacity=capacity,
    ).with_evidence_digest()
    return change, measurement, _STATE_BY_OUTCOME[spec.outcome]


def measurement_windows(completed: datetime) -> tuple[TimeWindow, TimeWindow]:
    """The pre- and post-change windows of a demo change whose rollout completed at ``completed``."""
    started = completed - ROLLOUT_DURATION
    return (
        TimeWindow(start=started - _WINDOW, end=started),
        TimeWindow(start=completed + _WARMUP, end=completed + _WARMUP + _WINDOW),
    )


def _member(spec: DemoChange, records: Sequence[RecommendationRecord]) -> Optional[RecommendationRecord]:
    for record in records:
        if record.pod_name == spec.pod_name and record.namespace == spec.namespace and record.applied_at:
            return record
    return None


async def _landed_records(reco_repo) -> list[RecommendationRecord]:
    """Applied recommendations, plus the regressed ones the applied list leaves out."""
    records = list(await reco_repo.get_applied_recommendations())
    records.extend(await reco_repo.get_recommendations_by_statuses([RecommendationStatus.REGRESSED.value]))
    return records


async def populate_demo_changes(
    reco_repo, change_repo, *, plan_ids: Optional[Mapping[int, str]] = None, now: Optional[datetime] = None
) -> int:
    """Record the demo changes in their measurement state; returns how many were recorded.

    ``plan_ids`` maps a recommendation id to the automation plan that delivered it
    (see :mod:`greenkube.demo.automation`).
    """
    current = now or datetime.now(timezone.utc)
    landed = await _landed_records(reco_repo)
    recorded = 0
    for spec in DEMO_CHANGES:
        member = _member(spec, landed)
        if member is None or member.id is None:
            logger.warning("Demo change %s/%s skipped: no applied recommendation.", spec.namespace, spec.owner_name)
            continue
        change, measurement, final_state = build_demo_change(spec, member, plan_id=(plan_ids or {}).get(member.id))
        stored, created = await change_repo.insert_change(change, actor="demo")
        if not created:
            continue
        recorded += 1
        if final_state is MeasurementState.APPLIED:
            continue
        status = await change_repo.get_status(stored.change_id)
        _, post = measurement_windows(rollout_completed_at(member.applied_at or current))
        status = await change_repo.transition(
            status,
            MeasurementState.MEASURING,
            event_type="measurement_started",
            actor="demo",
            post_window_start=post.start,
            post_window_end=post.end,
            now=post.start,
        )
        if measurement is None:
            continue
        await change_repo.insert_measurement(measurement)
        status = await change_repo.transition(
            status,
            final_state,
            event_type="measurement_decided",
            actor="demo",
            reason="; ".join(measurement.reasons),
            latest_measurement_id=measurement.measurement_id,
            now=measurement.decided_at,
        )
        if spec.regressed_days_ago is not None:
            await change_repo.transition(
                status,
                MeasurementState.REGRESSED,
                event_type="regressed",
                actor="savings-drift",
                reason=spec.regressed_reason or "",
                now=current - timedelta(days=spec.regressed_days_ago),
            )
    return recorded
