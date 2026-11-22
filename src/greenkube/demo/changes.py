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
from typing import Any, Optional, Sequence

from greenkube.models.metrics import RecommendationRecord
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
)

_STATE_BY_OUTCOME = {
    ChangeOutcome.REALIZED: MeasurementState.REALIZED,
    ChangeOutcome.NOT_REALIZED: MeasurementState.NOT_REALIZED,
    ChangeOutcome.INCONCLUSIVE: MeasurementState.INCONCLUSIVE,
    ChangeOutcome.CONTAMINATED: MeasurementState.CONTAMINATED,
}


def _digest(*parts: object) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()


def _resources(cpu_m: Optional[int], memory_bytes: Optional[int]) -> dict[str, Any]:
    return {DEMO_CONTAINER: {"cpu_request_millicores": cpu_m, "memory_request_bytes": memory_bytes}}


def _buckets(start: datetime, hours: int, replicas: float) -> list[HourBucket]:
    return [HourBucket(hour=start + timedelta(hours=index), coverage=1.0, replicas=replicas) for index in range(hours)]


def build_demo_change(
    spec: DemoChange, member: RecommendationRecord
) -> tuple[ChangeEvent, ChangeMeasurement, MeasurementState]:
    """The change record, its decided measurement (with the measured capacity) and its final state."""
    if member.id is None or member.applied_at is None:
        raise ValueError(f"demo change {spec.pod_name}: its recommendation has no id or applied_at")
    completed = member.applied_at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    started = completed - timedelta(minutes=20)
    digest_before = _digest(spec.owner_name, spec.cpu_before_m, spec.memory_before_bytes)
    digest_after = _digest(spec.owner_name, spec.cpu_after_m, spec.memory_after_bytes)
    merge_sha = _digest("merge", spec.repository, spec.pr_number)[:40]
    change = ChangeEvent(
        change_id=f"demo-{_digest('change', spec.namespace, spec.owner_name)[:24]}",
        recommendation_ids=[member.id],
        plan_id=f"demo-plan-{spec.pr_number}",
        pr_url=f"https://git.greenoptic.example/{spec.repository}/pulls/{spec.pr_number}",
        merge_sha=merge_sha,
        gitops_revision=merge_sha,
        synced_at=started - timedelta(minutes=2),
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
    pre = TimeWindow(start=started - _WINDOW, end=started)
    post = TimeWindow(start=completed + _WARMUP, end=completed + _WARMUP + _WINDOW)
    hours = int(_WINDOW.total_seconds() // 3600)
    capacity = measure_capacity(
        change,
        observations,
        _buckets(pre.start, hours, 1.0),
        _buckets(post.start, hours, 1.0),
        [member],
        post_end=post.end,
    )
    measurement_id = uuid.uuid5(uuid.NAMESPACE_URL, change.change_id).hex
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
        reason=(
            "the 90% interval is above zero"
            if spec.outcome is ChangeOutcome.REALIZED
            else "the 90% interval includes zero"
        ),
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
        reasons=[estimate.reason],
        decided_at=post.end,
        capacity=capacity,
    ).with_evidence_digest()
    return change, measurement, _STATE_BY_OUTCOME[spec.outcome]


def _member(spec: DemoChange, applied: Sequence[RecommendationRecord]) -> Optional[RecommendationRecord]:
    for record in applied:
        if record.pod_name == spec.pod_name and record.namespace == spec.namespace:
            return record
    return None


async def populate_demo_changes(reco_repo, change_repo) -> int:
    """Record the demo changes with their decided measurements; returns how many were recorded."""
    applied = await reco_repo.get_applied_recommendations()
    recorded = 0
    for spec in DEMO_CHANGES:
        member = _member(spec, applied)
        if member is None:
            logger.warning("Demo change %s/%s skipped: no applied recommendation.", spec.namespace, spec.owner_name)
            continue
        change, measurement, final_state = build_demo_change(spec, member)
        stored, created = await change_repo.insert_change(change, actor="demo")
        if not created:
            continue
        status = await change_repo.get_status(stored.change_id)
        status = await change_repo.transition(
            status,
            MeasurementState.MEASURING,
            event_type="measurement_started",
            actor="demo",
            post_window_start=measurement.windows.post.start,
            post_window_end=measurement.windows.post.end,
            now=measurement.windows.post.start,
        )
        await change_repo.insert_measurement(measurement)
        await change_repo.transition(
            status,
            final_state,
            event_type="measurement_decided",
            actor="demo",
            reason="; ".join(measurement.reasons),
            latest_measurement_id=measurement.measurement_id,
            now=measurement.decided_at,
        )
        recorded += 1
    return recorded
