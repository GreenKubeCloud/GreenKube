"""Deterministic plans and recommendations for pull-request content tests (J1-J5).

Timestamps are fixed because rendering never reads the clock: nothing here is
compared with the current time.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from greenkube.gitops.contracts import (
    ActionPlanV3,
    AddHorizontalPodAutoscaler,
    BindingStatus,
    ContainerRequestBaseline,
    ContainerResources,
    ControllerKind,
    DeleteResource,
    FileEdit,
    FilePlan,
    Plan,
    PlanStatus,
    PreApplyWorkloadBaseline,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    ValidationResult,
    WorkloadSource,
)
from greenkube.models.metrics import RecommendationRecord, RecommendationType

RENDERED_AT = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
BASE_REVISION = "a" * 40
PLAN_DIGEST = "sha256:" + "d" * 64
GIB = 1024**3
MIB = 1024**2


def _blob(content: str) -> str:
    payload = content.encode()
    return hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest()


def source(**overrides: Any) -> WorkloadSource:
    values: dict[str, Any] = {
        "id": "source-1",
        "cluster": "prod",
        "namespace": "payments",
        "workload_kind": "Deployment",
        "workload_name": "api",
        "provider_id": "github-main",
        "repo_url": "https://github.com/acme/gitops.git",
        "ref": "main",
        "revision": BASE_REVISION,
        "path": "apps/payments",
        "controller_kind": ControllerKind.ARGOCD,
        "controller_name": "payments-prod",
        "controller_namespace": "argocd",
        "source_revision": BASE_REVISION,
        "renderer": RendererKind.RAW,
        "renderer_config": {},
        "confidence": 0.98,
        "status": BindingStatus.RESOLVED,
        "evidence": {},
    }
    values.update(overrides)
    return WorkloadSource(**values)


def plan(
    operations: list[Any] | None = None,
    *,
    plan_id: str = "pln-1",
    recommendation_ids: tuple[int, ...] = (41, 42),
    rollback_of_plan_id: str | None = None,
    diff: str = "--- a/apps/payments/deployment.yaml\n+++ b/apps/payments/deployment.yaml\n-cpu: 500m\n+cpu: 200m",
    rendered_diff: str = "-        cpu: 500m\n+        cpu: 200m",
    baseline: bool = True,
    status: PlanStatus = PlanStatus.APPROVED,
    policy_result: dict[str, Any] | None = None,
    plan_source: WorkloadSource | None = None,
) -> Plan:
    target_kind = "Deployment"
    operations = operations or [
        SetContainerResources(
            container="api",
            resources=ContainerResources(requests={"cpu": "200m", "memory": "512Mi"}),
        )
    ]
    action = ActionPlanV3(
        action_id=plan_id,
        recommendation_ids=list(recommendation_ids),
        target=ResourceTarget(api_version="apps/v1", kind=target_kind, namespace="payments", name="api"),
        operations=operations,
        evidence_ref=f"recommendation/{recommendation_ids[0]}",
    )
    content = "kind: Deployment\n"
    file_plan = FilePlan(
        plan_id=plan_id,
        files=[
            FileEdit(
                path="apps/payments/deployment.yaml",
                operation="update",
                original_sha=_blob(content),
                content=content + "#\n",
            )
        ],
        diff=diff,
        rendered_diff=rendered_diff,
        base_revision=BASE_REVISION,
        source_revision=BASE_REVISION,
        renderer=RendererKind.RAW,
        validation=ValidationResult(
            target_changed=True,
            unrelated_changes=False,
            requests_within_limits=True,
            renderer=RendererKind.RAW,
            output_digest="sha256:" + "e" * 64,
        ),
        digest=PLAN_DIGEST,
    )
    pre_apply = (
        PreApplyWorkloadBaseline(
            workload_uid="uid-api",
            generation=3,
            observed_generation=3,
            container_requests={"api": ContainerRequestBaseline(cpu_millicores=500, memory_bytes=GIB)},
        )
        if baseline
        else None
    )
    return Plan(
        plan_id=plan_id,
        recommendation_ids=list(recommendation_ids),
        source=plan_source or source(),
        action=action,
        policy_result=policy_result or {"allowed": True, "warnings": []},
        file_plan=file_plan,
        status=status,
        digest=PLAN_DIGEST,
        rollback_of_plan_id=rollback_of_plan_id,
        pre_apply_baseline=pre_apply,
        created_at=RENDERED_AT - timedelta(minutes=5),
    )


def impact(*, cost: float, co2e_g: float, realization: float = 0.8, currency: str = "USD") -> dict[str, Any]:
    return {
        "model_version": "1.1.0",
        "kind": "container_resources",
        "method": "projected",
        "currency": currency,
        "allocated_cost_annual": cost / realization,
        "expected_billable_cost_annual": cost,
        "realization_factor": realization,
        "co2e_expected_annual_g": co2e_g,
        "carbon_price_per_tonne": 100.0,
        "value_annual": cost,
        "confidence": 0.9,
        "assumptions": {
            "allocation_policy": "reservation_aware",
            "unit_prices": [
                {
                    "cpu_core_hour": 0.0316,
                    "ram_gib_hour": 0.0042,
                    "currency": currency,
                    "source": "pricing_catalog",
                    "detail": "aws/eu-west-3/m5.large",
                }
            ],
            "observed_cost_sources": {"opencost": 0.75},
            "grid_intensity_g_per_kwh": 56.0,
            "pue": 1.135,
            "power_sources": ["profile"],
            "realization_factor_source": "realization_context",
            "realization_reasons": ["karpenter consolidation is enabled on pool default"],
        },
    }


def recommendation(
    recommendation_id: int = 41,
    *,
    rec_type: RecommendationType = RecommendationType.RIGHTSIZING_CPU,
    risk_level: str = "low",
    cost: float = 300.0,
    co2e_g: float = 12_000.0,
    **overrides: Any,
) -> RecommendationRecord:
    cpu = rec_type is RecommendationType.RIGHTSIZING_CPU
    values: dict[str, Any] = {
        "id": recommendation_id,
        "pod_name": "api",
        "container_name": "api",
        "namespace": "payments",
        "type": rec_type,
        "description": "Requests exceed observed usage.",
        "reason": "Observed p95 is well below the request." if cpu else "Memory p99 is far below the request.",
        "scope": "workload",
        "owner_kind": "Deployment",
        "owner_name": "api",
        "potential_savings_cost": cost,
        "potential_savings_co2e_grams": co2e_g,
        "confidence": 0.92,
        "risk_level": risk_level,
        "risk_factors": ["single replica"] if risk_level == "high" else ["p95 usage remains below target"],
        "sources": ["greenkube", "vpa"] if cpu else ["greenkube"],
        "impact": impact(cost=cost, co2e_g=co2e_g),
        "evidence": {
            "observation_window_start": RENDERED_AT - timedelta(days=7),
            "observation_window_end": RENDERED_AT,
            "sample_count": 2016,
            "coverage_ratio": 0.98,
            "current": {"cpu_request_millicores": 500, "memory_request_bytes": GIB},
            "proposed": {"cpu_request_millicores": 200} if cpu else {"memory_request_bytes": 512 * MIB},
            "cpu_usage": {"p50": 80, "p90": 110, "p95": 120, "p99": 150, "max": 180, "avg": 90},
            "memory_usage": {
                "p50": 200 * MIB,
                "p90": 260 * MIB,
                "p95": 300 * MIB,
                "p99": 340 * MIB,
                "max": 380 * MIB,
                "avg": 220 * MIB,
            },
            "restart_count": 0,
            "oom_events": 0,
            "savings_method": "impact_model/1.1.0/container_resources",
            "rollback_conditions": [
                {
                    "metric": "cpu_p95_millicores",
                    "comparator": "gt",
                    "threshold": 220,
                    "description": "Review if CPU p95 exceeds 220m.",
                }
            ],
        },
    }
    values.update(overrides)
    return RecommendationRecord(**values)


def rightsize_plan() -> Plan:
    return plan()


def delete_plan() -> Plan:
    return plan([DeleteResource()], recommendation_ids=(51,), baseline=False)


def hpa_plan() -> Plan:
    return plan(
        [AddHorizontalPodAutoscaler(min_replicas=2, max_replicas=6, target_cpu_utilization=70)],
        recommendation_ids=(61,),
        baseline=False,
    )


def rollback_plan() -> Plan:
    return plan(plan_id="rbk-1", rollback_of_plan_id="pln-1")


def increase_plan() -> Plan:
    """H1: a memory increase (1Gi -> 2Gi) that raises the memory limit (1.5Gi -> 3Gi)."""
    return plan(
        [
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"memory": "2Gi"}, limits={"memory": "3Gi"}),
            )
        ],
        plan_id="pln-inc",
        recommendation_ids=(43,),
        diff="--- a/apps/payments/deployment.yaml\n+++ b/apps/payments/deployment.yaml\n-memory: 1Gi\n+memory: 2Gi",
        rendered_diff="-        memory: 1Gi\n+        memory: 2Gi",
    )


def cpu_limit_removal_plan() -> Plan:
    """H1: a CPU increase (500m -> 800m) under ``remove_cpu``: the CPU limit is removed."""
    return plan(
        [
            SetContainerResources(
                container="api", resources=ContainerResources(requests={"cpu": "800m"}), remove_limits=["cpu"]
            )
        ],
        plan_id="pln-lim",
        recommendation_ids=(44,),
        diff="--- a/apps/payments/deployment.yaml\n+++ b/apps/payments/deployment.yaml\n-cpu: 500m\n+cpu: 800m",
        rendered_diff="-        cpu: 500m\n+        cpu: 800m",
    )


def increase_recommendation(
    recommendation_id: int = 43, *, rec_type: RecommendationType = RecommendationType.RIGHTSIZING_MEMORY
) -> RecommendationRecord:
    """A reliability increase: negative projected savings (a cost), OOM history and limit changes."""
    memory = rec_type is RecommendationType.RIGHTSIZING_MEMORY
    base = recommendation(recommendation_id, rec_type=rec_type, cost=-150.0, co2e_g=-4_000.0)
    assert base.evidence is not None
    evidence = base.evidence.model_copy(
        update={
            "current": base.evidence.current.model_copy(
                update={"memory_limit_bytes": 1536 * MIB} if memory else {"cpu_limit_millicores": 1000}
            ),
            "proposed": base.evidence.proposed.model_copy(
                update={"memory_request_bytes": 2 * GIB, "memory_limit_bytes": 3 * GIB}
                if memory
                else {"cpu_request_millicores": 800}
            ),
            "oom_events": 3 if memory else 0,
            "cpu_throttling_ratio": None if memory else 0.12,
            "cpu_usage": base.evidence.cpu_usage.model_copy(update={"p95": 470, "p99": 520, "max": 610})
            if base.evidence.cpu_usage is not None and not memory
            else base.evidence.cpu_usage,
            "memory_usage": base.evidence.memory_usage.model_copy(
                update={"p95": 900 * MIB, "p99": 980 * MIB, "max": 1100 * MIB}
            )
            if base.evidence.memory_usage is not None and memory
            else base.evidence.memory_usage,
        }
    )
    request = (
        {"current_memory_request_bytes": GIB, "recommended_memory_request_bytes": 2 * GIB}
        if memory
        else {"current_cpu_request_millicores": 500, "recommended_cpu_request_millicores": 800}
    )
    return base.model_copy(
        update={
            "evidence": evidence,
            "reason": "Reliability: containers were OOM-killed."
            if memory
            else "Reliability: p99 CPU is above the request.",
            "risk_factors": ["oom_history"] if memory else ["cpu_throttling"],
            "value": {"lane": "reliability"},
            **request,
        }
    )
