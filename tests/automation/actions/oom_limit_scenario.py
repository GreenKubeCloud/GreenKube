"""The live Minikube e2e OOM scenario: memory request = limit = 24Mi, OOM-killed (shared by planner tests).

The increase raises the request to 48Mi. It must also raise the limit with headroom
above the new request, or the OOM kills continue (the limit triggers the kill).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from greenkube.automation.actions.container_resources import build_container_resource_actions
from greenkube.core.config import Config
from greenkube.core.optimization.analyzers.rightsizing import planned_limit_changes
from greenkube.gitops.contracts import ActionPlanV3, SetContainerResources
from greenkube.models.evidence import PatchV2, ProposedChange, RecommendationEvidence, ResourceSnapshot
from greenkube.models.metrics import Recommendation, RecommendationType
from greenkube.models.workload_usage import UsageStats, WorkloadUsageSummary

MIB = 1024 * 1024
#: Request and limit of the OOM-killed container.
OOM_REQUEST = 24 * MIB
#: Request of the bounded increase GreenKube opened.
OOM_PROPOSED = 48 * MIB


def oom_summary(*, limit: int | None = OOM_REQUEST, oom_kills: int = 3) -> WorkloadUsageSummary:
    """A container pinned at its 24Mi limit and OOM-killed ``oom_kills`` times over two days."""
    last = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
    stats = UsageStats(
        sample_count=576,
        avg=20 * MIB,
        p50=20 * MIB,
        p90=23 * MIB,
        p95=23 * MIB,
        p99=23.5 * MIB,
        max=23.9 * MIB,
    )
    return WorkloadUsageSummary(
        namespace="payments",
        target_kind="Deployment",
        target_name="api",
        container_name="api",
        first_hour=last - timedelta(hours=47),
        last_hour=last,
        hours_observed=48,
        memory=stats,
        memory_request=OOM_REQUEST,
        memory_limit=limit,
        oom_kill_count=oom_kills,
        restarts_in_window=oom_kills,
    )


def oom_recommendation(config: Config | None = None, *, recommendation_id: int = 7) -> Recommendation:
    """The 24Mi -> 48Mi increase with the limit changes the rightsizing rules plan for it."""
    config = config or Config()
    summary = oom_summary()
    limit_changes = planned_limit_changes(config, summary, None, "memory", OOM_PROPOSED)
    proposed = ResourceSnapshot(memory_request_bytes=OOM_PROPOSED)
    for change in limit_changes:
        proposed = proposed.model_copy(update={"memory_limit_bytes": change.proposed})
    evidence = RecommendationEvidence(
        current=ResourceSnapshot(memory_request_bytes=OOM_REQUEST, memory_limit_bytes=OOM_REQUEST),
        proposed=proposed,
        changes=[ProposedChange(resource="memory", current=OOM_REQUEST, proposed=OOM_PROPOSED)],
        proposed_patch=PatchV2(container_name="api"),
        limit_changes=limit_changes,
        sample_count=576,
        oom_events=summary.oom_kill_count,
        observation_window_seconds=48 * 3600,
    )
    return Recommendation(
        id=recommendation_id,
        pod_name="api",
        container_name="api",
        namespace="payments",
        type=RecommendationType.RIGHTSIZING_MEMORY,
        description="Raise the memory request.",
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        evidence=evidence,
        current_memory_request_bytes=OOM_REQUEST,
        recommended_memory_request_bytes=OOM_PROPOSED,
        source="greenkube",
    )


def oom_action(config: Config | None = None) -> ActionPlanV3 | None:
    """The action GreenKube builds for the scenario, None when it stays advisory."""
    recommendation = oom_recommendation(config)
    return build_container_resource_actions([recommendation]).get(recommendation.id or 0)


def oom_operation() -> SetContainerResources:
    action = oom_action()
    assert action is not None
    (operation,) = action.operations
    assert isinstance(operation, SetContainerResources)
    return operation
