"""Tests for safe, container-specific recommendation actions."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from greenkube.core.config import Config
from greenkube.core.optimization.actions import build_action_plans
from greenkube.core.optimization.analyzers.rightsizing_cpu import CpuRightsizingAnalyzer
from greenkube.core.optimization.analyzers.rightsizing_memory import MemoryRightsizingAnalyzer
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.engine import OptimizationEngine
from greenkube.core.optimization.enrich import enrich_recommendations
from greenkube.core.optimization.evidence import build_patch
from greenkube.gitops.contracts import ActionPlanV3, ContainerResources, SetContainerResources
from greenkube.models.evidence import PatchV2, ProposedChange, RecommendationEvidence, ResourceSnapshot
from greenkube.models.metrics import (
    CombinedMetric,
    Recommendation,
    RecommendationPatchV2,
    RecommendationRecord,
    RecommendationType,
)

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture
def multi_container_metrics() -> list[CombinedMetric]:
    """Container-level metrics for an app and its sidecar."""
    definitions = (
        ("api", 1000, 1_024**3, (150, 180, 200), (256, 270, 280)),
        ("sidecar", 500, 2 * 1_024**3, (80, 100, 120), (150, 160, 180)),
    )
    metrics = []
    for container, cpu_request, memory_request, cpu_usages, memory_usages_mib in definitions:
        for index, (cpu_usage, memory_usage_mib) in enumerate(zip(cpu_usages, memory_usages_mib, strict=True)):
            metrics.append(
                CombinedMetric(
                    pod_name="api-7d9f8b6c5-x2k4p",
                    namespace="prod",
                    owner_kind="Deployment",
                    owner_name="api",
                    container_name=container,
                    cpu_request=cpu_request,
                    memory_request=memory_request,
                    cpu_usage_millicores=cpu_usage,
                    cpu_usage_max_millicores=cpu_usage + 20,
                    memory_usage_bytes=memory_usage_mib * 1_024**2,
                    memory_usage_max_bytes=(memory_usage_mib + 20) * 1_024**2,
                    total_cost=0.01,
                    co2e_grams=1.0,
                    sample_count=1,
                    timestamp=NOW + timedelta(hours=index),
                )
            )
    return metrics


def _recommendation(
    *,
    recommendation_id: int,
    container_name: str | None,
    rec_type: RecommendationType = RecommendationType.RIGHTSIZING_CPU,
    cpu_request: int | None = None,
    cpu_target: int | None = None,
    memory_request: int | None = None,
    memory_target: int | None = None,
) -> Recommendation:
    current = ResourceSnapshot(
        cpu_request_millicores=cpu_request,
        memory_request_bytes=memory_request,
    )
    proposed = ResourceSnapshot(
        cpu_request_millicores=cpu_target,
        memory_request_bytes=memory_target,
    )
    changes = []
    if cpu_target is not None:
        changes.append(
            ProposedChange(
                resource="cpu",
                current=cpu_request,
                proposed=cpu_target,
                change_ratio=(cpu_request - cpu_target) / cpu_request if cpu_request else None,
            )
        )
    if memory_target is not None:
        changes.append(
            ProposedChange(
                resource="memory",
                current=memory_request,
                proposed=memory_target,
                change_ratio=(memory_request - memory_target) / memory_request if memory_request else None,
            )
        )
    evidence = RecommendationEvidence(
        current=current,
        proposed=proposed,
        changes=changes,
        proposed_patch=PatchV2(container_name=container_name),
    )
    return Recommendation(
        id=recommendation_id,
        pod_name="api",
        container_name=container_name,
        namespace="prod",
        type=rec_type,
        description="Reduce the request.",
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        current_cpu_request_millicores=cpu_request,
        recommended_cpu_request_millicores=cpu_target,
        current_memory_request_bytes=memory_request,
        recommended_memory_request_bytes=memory_target,
        evidence=evidence,
    )


def test_rightsizing_analyzers_preserve_explicit_container_identity(multi_container_metrics):
    config = Config()
    context = OptimizationContext(config=config, metrics=multi_container_metrics)

    cpu_recommendations = CpuRightsizingAnalyzer(config).analyze(context)
    memory_recommendations = MemoryRightsizingAnalyzer(config).analyze(context)

    assert {rec.container_name for rec in cpu_recommendations} == {"api", "sidecar"}
    assert {rec.container_name for rec in memory_recommendations} == {"api", "sidecar"}
    assert {rec.current_cpu_request_millicores for rec in cpu_recommendations} == {500, 1000}
    assert {rec.current_memory_request_bytes for rec in memory_recommendations} == {1_024**3, 2 * 1_024**3}


def test_rightsizing_savings_never_project_negative_cost(multi_container_metrics):
    config = Config()
    metrics = [metric.model_copy(update={"total_cost": -0.01}) for metric in multi_container_metrics]
    context = OptimizationContext(config=config, metrics=metrics, analysis_window_seconds=10_800)

    recommendations = [
        *CpuRightsizingAnalyzer(config).analyze(context),
        *MemoryRightsizingAnalyzer(config).analyze(context),
    ]

    assert recommendations
    assert all(recommendation.potential_savings_cost == 0.0 for recommendation in recommendations)


def test_evidence_is_scoped_to_the_recommendation_container(multi_container_metrics):
    config = Config()
    context = OptimizationContext(config=config, metrics=multi_container_metrics, analysis_window_seconds=10_800)
    recommendation = CpuRightsizingAnalyzer(config).analyze(context)[0]

    enriched = enrich_recommendations([recommendation], context, config)[0]

    assert enriched.container_name == recommendation.container_name
    assert enriched.evidence is not None
    assert enriched.evidence.current.cpu_request_millicores == recommendation.current_cpu_request_millicores
    assert enriched.evidence.cpu_usage is not None
    assert enriched.evidence.cpu_usage.avg == pytest.approx(
        sum(
            metric.cpu_usage_millicores
            for metric in multi_container_metrics
            if metric.container_name == recommendation.container_name
        )
        / 3
    )
    assert enriched.evidence.proposed_patch is not None
    assert enriched.evidence.proposed_patch.container_name == recommendation.container_name


def test_grouped_action_combines_container_updates_for_one_workload_change_set():
    cpu = _recommendation(recommendation_id=41, container_name="api", cpu_request=1000, cpu_target=300)
    memory = _recommendation(
        recommendation_id=42,
        container_name="api",
        rec_type=RecommendationType.RIGHTSIZING_MEMORY,
        memory_request=1_024**3,
        memory_target=256 * 1_024**2,
    )
    sidecar = _recommendation(recommendation_id=43, container_name="sidecar", cpu_request=500, cpu_target=200)

    plans = build_action_plans([cpu, memory, sidecar])

    assert plans[41] == plans[42] == plans[43]
    assert plans[41].recommendation_ids == [41, 42, 43]
    assert isinstance(plans[41], ActionPlanV3)
    assert len(plans[41].operations) == 2
    api_operation = next(
        operation
        for operation in plans[41].operations
        if isinstance(operation, SetContainerResources) and operation.container == "api"
    )
    assert isinstance(api_operation, SetContainerResources)
    assert api_operation.container == "api"
    assert api_operation.resources == ContainerResources(requests={"cpu": "300m", "memory": "256Mi"})
    sidecar_operation = next(
        operation
        for operation in plans[41].operations
        if isinstance(operation, SetContainerResources) and operation.container == "sidecar"
    )
    assert sidecar_operation.resources == ContainerResources(requests={"cpu": "200m"})


def test_aggregated_or_ambiguous_recommendations_have_no_action_or_patch():
    aggregated = _recommendation(recommendation_id=51, container_name=None, cpu_request=1000, cpu_target=300)
    ambiguous = _recommendation(recommendation_id=52, container_name="  ", cpu_request=1000, cpu_target=300)

    assert build_action_plans([aggregated, ambiguous]) == {}
    assert build_patch(aggregated) is None
    assert build_patch(ambiguous) is None


def test_legacy_patch_and_v3_action_round_trip_through_models():
    recommendation = _recommendation(
        recommendation_id=61,
        container_name="api",
        cpu_request=1000,
        cpu_target=300,
    )
    action = build_action_plans([recommendation])[61]
    recommendation.action = action
    recommendation.patch = RecommendationPatchV2.model_validate(build_patch(recommendation, action))

    assert recommendation.patch is not None
    assert recommendation.patch["kind"] == "Deployment"
    assert recommendation.patch["container_name"] == "api"
    assert recommendation.patch.operations[0].get("resource") == "cpu"
    assert recommendation.patch.operations[0].get("value") == "300m"

    recommendation_round_trip = Recommendation.model_validate_json(recommendation.model_dump_json())
    assert recommendation_round_trip.action == action
    assert recommendation_round_trip.patch is not None
    assert recommendation_round_trip.patch.action == action
    assert recommendation_round_trip.patch["container_name"] == "api"

    record = RecommendationRecord.from_recommendation(recommendation_round_trip)
    record_round_trip = RecommendationRecord.model_validate_json(record.model_dump_json())
    assert record_round_trip.action == action
    assert record_round_trip.patch is not None
    assert record_round_trip.patch.action == action


async def test_engine_persists_action_plans_with_database_recommendation_ids():
    cpu = _recommendation(recommendation_id=0, container_name="api", cpu_request=1000, cpu_target=300)
    memory = _recommendation(
        recommendation_id=0,
        container_name="api",
        rec_type=RecommendationType.RIGHTSIZING_MEMORY,
        memory_request=1_024**3,
        memory_target=256 * 1_024**2,
    )
    cpu.id = None
    memory.id = None
    cpu.patch = RecommendationPatchV2.model_validate(build_patch(cpu))
    memory.patch = RecommendationPatchV2.model_validate(build_patch(memory))
    persisted = [
        RecommendationRecord.from_recommendation(cpu).model_copy(update={"id": 71}),
        RecommendationRecord.from_recommendation(memory).model_copy(update={"id": 72}),
    ]
    repository = AsyncMock()
    repository.get_active_recommendations = AsyncMock(return_value=persisted)

    recommendations = await OptimizationEngine(sources=[]).persist([cpu, memory], repository)

    assert recommendations[0].action is not None
    assert recommendations[0].action == recommendations[1].action
    assert recommendations[0].action.recommendation_ids == [71, 72]
    assert recommendations[0].patch is not None
    assert recommendations[0].patch.action == recommendations[0].action
    assert repository.upsert_recommendations.await_count == 2
    repository.reconcile_active_recommendations.assert_awaited_once()
