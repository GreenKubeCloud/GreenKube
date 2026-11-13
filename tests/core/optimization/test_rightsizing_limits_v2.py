"""Limit changes delivered with rightsizing (roadmap H1)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from greenkube.automation.actions.container_resources import build_container_resource_actions
from greenkube.core.config import Config
from greenkube.core.optimization.analyzers import CpuRightsizingAnalyzer
from greenkube.core.optimization.analyzers.rightsizing import CPU_LIMIT_P99_FACTOR, planned_limit_changes
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.evidence import build_evidence
from greenkube.gitops.contracts import SetContainerResources
from greenkube.models.metrics import CombinedMetric
from greenkube.models.workload_usage import UsageStats, WorkloadUsageSummary

MIB = 1024 * 1024


def _config(**overrides: Any) -> Config:
    return Config().model_copy(update=overrides)


def _stats(avg: float, p99: float, peak: float) -> UsageStats:
    return UsageStats(sample_count=4032, avg=avg, p50=avg, p90=p99, p95=p99, p99=p99, max=peak)


def _summary(**overrides: Any) -> WorkloadUsageSummary:
    last = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    values: dict[str, Any] = {
        "namespace": "shop",
        "target_kind": "Deployment",
        "target_name": "api",
        "container_name": "web",
        "first_hour": last - timedelta(hours=14 * 24 - 1),
        "last_hour": last,
        "hours_observed": 14 * 24,
        "cpu": _stats(avg=500, p99=900, peak=950),
        "memory": _stats(avg=400 * MIB, p99=480 * MIB, peak=600 * MIB),
        "cpu_request": 1000,
        "memory_request": 512 * MIB,
    }
    values.update(overrides)
    return WorkloadUsageSummary(**values)


def _changes(config: Config, summary: WorkloadUsageSummary, resource: str, proposed: int):
    return [
        (change.resource, change.action, change.current, change.proposed, change.reason)
        for change in planned_limit_changes(config, summary, None, resource, proposed)  # type: ignore[arg-type]
    ]


def test_cpu_increase_above_the_limit_raises_it_to_the_advised_floor() -> None:
    summary = _summary(cpu_limit=1000)
    floor = max(1200, math.ceil(900 * CPU_LIMIT_P99_FACTOR))
    assert _changes(Config(), summary, "cpu", 1200) == [("cpu", "set", 1000, floor, "below_request")]


def test_cpu_increase_under_remove_cpu_removes_the_limit() -> None:
    summary = _summary(cpu_limit=1000)
    assert _changes(_config(RECOMMENDATION_LIMITS_POLICY="remove_cpu"), summary, "cpu", 1200) == [
        ("cpu", "remove", 1000, None, "policy")
    ]


def test_throttled_cpu_increase_raises_the_limit() -> None:
    summary = _summary(cpu_limit=1100, throttling_ratio=0.2)
    floor = math.ceil(900 * CPU_LIMIT_P99_FACTOR)
    assert _changes(Config(), summary, "cpu", 1050) == [("cpu", "set", 1100, floor, "throttling")]
    # A limit already above the advised floor is never lowered.
    assert _changes(Config(), _summary(cpu_limit=1500, throttling_ratio=0.2), "cpu", 1200) == []


def test_memory_increase_raises_a_limit_below_the_observed_peak() -> None:
    summary = _summary(memory_limit=640 * MIB)
    floor = math.ceil(600 * 1.1)  # MiB, 110% of the observed maximum
    assert _changes(Config(), summary, "memory", 576 * MIB) == [
        ("memory", "set", 640 * MIB, floor * MIB, "below_floor")
    ]


def test_an_increase_never_lowers_a_limit() -> None:
    """``memory_equals_request`` on an increase only raises: the limit stays above the peak."""
    summary = _summary(memory_limit=2048 * MIB)
    config = _config(RECOMMENDATION_LIMITS_POLICY="memory_equals_request")
    assert _changes(config, summary, "memory", 576 * MIB) == []


def test_an_increase_under_memory_equals_request_sets_at_least_the_peak() -> None:
    summary = _summary(memory_limit=None)
    config = _config(RECOMMENDATION_LIMITS_POLICY="memory_equals_request")
    assert _changes(config, summary, "memory", 576 * MIB) == [("memory", "set", None, 660 * MIB, "policy")]


def test_a_decrease_keeps_limits_by_default() -> None:
    """Default ``keep``: a decrease PR stays request-only (golden decrease diffs unchanged)."""
    summary = _summary(memory_request=2048 * MIB, memory_limit=600 * MIB, cpu_limit=1000, throttling_ratio=0.5)
    assert _changes(Config(), summary, "memory", 1024 * MIB) == []
    assert _changes(Config(), _summary(cpu_request=4000, cpu_limit=1000, throttling_ratio=0.5), "cpu", 2000) == []


def test_a_decrease_carries_the_opt_in_policies() -> None:
    memory = _summary(memory_request=2048 * MIB, memory_limit=4096 * MIB)
    config = _config(RECOMMENDATION_LIMITS_POLICY="memory_equals_request")
    assert _changes(config, memory, "memory", 1024 * MIB) == [("memory", "set", 4096 * MIB, 1024 * MIB, "policy")]
    cpu = _summary(cpu_request=4000, cpu_limit=4000)
    assert _changes(_config(RECOMMENDATION_LIMITS_POLICY="remove_cpu"), cpu, "cpu", 2000) == [
        ("cpu", "remove", 4000, None, "policy")
    ]


def test_no_limit_means_no_change() -> None:
    assert _changes(Config(), _summary(), "cpu", 1200) == []
    assert _changes(Config(), _summary(), "memory", 576 * MIB) == []


def test_scan_evidence_carries_limits_and_the_action_raises_the_limit() -> None:
    """End to end: a hot container with a CPU limit gets an increase action that also raises the limit."""
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(days=2)
    rows = [
        CombinedMetric(
            pod_name="api-7d9f8b6c5d-abcde",
            namespace="shop",
            owner_kind="Deployment",
            owner_name="api",
            container_name="web",
            timestamp=start + timedelta(hours=hour),
            duration_seconds=3600,
            cpu_request=500,
            cpu_limit=500,
            memory_request=512 * MIB,
            cpu_usage_millicores=490,
            memory_usage_bytes=100 * MIB,
            total_cost=0.05,
            cost_cpu=0.03,
            cost_ram=0.02,
            cost_source="opencost",
            node="node-a",
        )
        for hour in range(48)
    ]
    context = OptimizationContext(config=Config(), metrics=rows, analysis_window_seconds=2 * 86400)
    (rec,) = CpuRightsizingAnalyzer(context.config).analyze(context)
    assert rec.recommended_cpu_request_millicores is not None and rec.recommended_cpu_request_millicores > 500
    evidence = build_evidence(rec, context)
    assert evidence is not None
    assert evidence.current.cpu_limit_millicores == 500
    (change,) = evidence.limit_changes
    assert change.action == "set" and change.proposed is not None
    assert change.proposed >= rec.recommended_cpu_request_millicores
    assert evidence.proposed.cpu_limit_millicores == change.proposed

    rec = rec.model_copy(update={"id": 11, "evidence": evidence})
    (operation,) = build_container_resource_actions([rec])[11].operations
    assert isinstance(operation, SetContainerResources)
    assert operation.resources.requests == {"cpu": f"{rec.recommended_cpu_request_millicores}m"}
    assert operation.resources.limits == {"cpu": f"{change.proposed}m"}
