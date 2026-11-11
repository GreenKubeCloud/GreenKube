"""Two-way rightsizing, coverage gate and limits policy (roadmap E9)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from greenkube.core.config import Config
from greenkube.core.optimization.analyzers import CpuRightsizingAnalyzer, MemoryRightsizingAnalyzer, NodesAnalyzer
from greenkube.core.optimization.analyzers.rightsizing import cpu_proposal, memory_proposal
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.evidence import build_evidence
from greenkube.models.metrics import CombinedMetric, RecommendationType
from greenkube.models.node import NodeInfo
from greenkube.models.workload_usage import UsageStats, WorkloadUsageSummary

MIB = 1024 * 1024


def _config(**overrides: Any) -> Config:
    return Config().model_copy(update=overrides)


def _stats(avg: float, p95: float, p99: float, peak: float, p50: float | None = None) -> UsageStats:
    return UsageStats(sample_count=4032, avg=avg, p50=p50 or avg, p90=p95, p95=p95, p99=p99, max=peak)


def _summary(days: float = 14.0, **overrides: Any) -> WorkloadUsageSummary:
    last = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    values: dict[str, Any] = {
        "namespace": "shop",
        "target_kind": "Deployment",
        "target_name": "api",
        "container_name": "web",
        "first_hour": last - timedelta(hours=days * 24 - 1),
        "last_hour": last,
        "hours_observed": int(days * 24),
        "cpu": _stats(avg=50, p95=80, p99=90, peak=120),
        "memory": _stats(avg=100 * MIB, p95=120 * MIB, p99=130 * MIB, peak=140 * MIB),
        "cpu_request": 1000,
        "memory_request": 1024 * MIB,
    }
    values.update(overrides)
    return WorkloadUsageSummary(**values)


class TestCoverageGate:
    def test_decrease_requires_a_full_week_by_default(self):
        config = Config()
        assert config.RECOMMENDATION_MIN_DECREASE_COVERAGE_DAYS == 7
        assert config.RECOMMENDATION_LOOKBACK_DAYS == 14

        assert cpu_proposal(config, _summary(days=6)) is None
        assert memory_proposal(config, _summary(days=6)) is None

        cpu = cpu_proposal(config, _summary(days=7))
        memory = memory_proposal(config, _summary(days=7))
        assert cpu is not None and cpu.direction == "decrease" and cpu.proposed < 1000
        assert memory is not None and memory.direction == "decrease"

    def test_unknown_coverage_never_allows_a_decrease(self):
        assert cpu_proposal(Config(), _summary(first_hour=None, last_hour=None)) is None

    def test_increases_do_not_wait_for_coverage(self):
        hot = _summary(days=1, cpu=_stats(avg=850, p95=950, p99=990, peak=1200))
        proposal = cpu_proposal(Config(), hot)
        assert proposal is not None
        assert proposal.direction == "increase"
        assert proposal.proposed == math.ceil(990 * Config().RIGHTSIZING_HEADROOM)


class TestIncreases:
    def test_cpu_p99_above_ninety_percent_of_the_request(self):
        proposal = cpu_proposal(Config(), _summary(cpu=_stats(avg=500, p95=850, p99=910, peak=1000)))
        assert proposal is not None
        assert proposal.direction == "increase"
        assert proposal.triggers == ["p99_above_request"]
        assert proposal.proposed > 1000

    def test_cpu_p99_just_below_the_trigger_is_left_alone(self):
        assert cpu_proposal(Config(), _summary(cpu=_stats(avg=500, p95=850, p99=899, peak=1000))) is None

    @pytest.mark.parametrize(
        ("overrides", "trigger"),
        [
            ({"memory": _stats(avg=800 * MIB, p95=900 * MIB, p99=960 * MIB, peak=990 * MIB)}, "p99_above_request"),
            ({"oom_kill_count": 2, "memory_request": 150 * MIB}, "oom_kills"),
            (
                {
                    "memory_request": 128 * MIB,
                    "memory_limit": 150 * MIB,
                    "memory": _stats(avg=100 * MIB, p95=110 * MIB, p99=112 * MIB, peak=140 * MIB),
                },
                "working_set_near_limit",
            ),
        ],
    )
    def test_memory_triggers(self, overrides, trigger):
        proposal = memory_proposal(Config(), _summary(**overrides))
        assert proposal is not None
        assert proposal.direction == "increase"
        assert proposal.triggers == [trigger]
        assert proposal.proposed > proposal.current
        assert proposal.proposed % MIB == 0
        stats = _summary(**overrides).memory
        assert stats is not None and proposal.proposed >= stats.max * 1.1

    def test_increases_are_projected_as_negative_savings(self):
        context = _context_with_rows(cpu_usage=950, cpu_request=1000, days=1)
        (rec,) = CpuRightsizingAnalyzer(context.config).analyze(context)
        assert rec.recommended_cpu_request_millicores is not None
        assert rec.current_cpu_request_millicores is not None
        assert rec.recommended_cpu_request_millicores > rec.current_cpu_request_millicores
        assert rec.potential_savings_cost is not None and rec.potential_savings_cost < 0
        assert rec.impact is not None and rec.impact["expected_billable_cost_annual"] < 0
        assert rec.priority in {"high", "medium"}
        assert "Recommend increasing" in rec.description


class TestLimitsPolicy:
    def test_keep_flags_a_cpu_limit_below_the_new_request(self):
        summary = _summary(cpu_limit=1000, cpu=_stats(avg=900, p95=980, p99=990, peak=1000))
        proposal = cpu_proposal(Config(), summary)
        assert proposal is not None and proposal.proposed > 1000
        assert proposal.limit_advice and "below the proposed request" in proposal.limit_advice[0].text
        assert (proposal.limit_advice[0].action, proposal.limit_advice[0].reason) == ("raise", "below_request")

    def test_keep_flags_cpu_throttling(self):
        summary = _summary(days=14, cpu_limit=2000, throttling_ratio=0.12)
        proposal = cpu_proposal(Config(), summary)
        assert proposal is not None and proposal.direction == "decrease"
        assert proposal.limit_advice and "throttled in 12%" in proposal.limit_advice[0].text
        assert proposal.limit_advice[0].reason == "throttling"

    def test_keep_leaves_healthy_limits_alone(self):
        proposal = cpu_proposal(Config(), _summary(cpu_limit=2000))
        assert proposal is not None and proposal.limit_advice == []

    def test_remove_cpu_policy(self):
        proposal = cpu_proposal(_config(RECOMMENDATION_LIMITS_POLICY="remove_cpu"), _summary(cpu_limit=2000))
        assert proposal is not None and [advice.text for advice in proposal.limit_advice] == [
            "Remove the CPU limit (2000m) (limits policy remove_cpu)."
        ]
        assert (proposal.limit_advice[0].action, proposal.limit_advice[0].current) == ("remove", 2000)

    def test_memory_equals_request_keeps_the_observed_maximum_covered(self):
        summary = _summary(memory=_stats(avg=50 * MIB, p95=60 * MIB, p99=70 * MIB, peak=500 * MIB))
        keep = memory_proposal(Config(), summary)
        equal = memory_proposal(_config(RECOMMENDATION_LIMITS_POLICY="memory_equals_request"), summary)
        assert keep is not None and equal is not None
        # Under memory_equals_request the request becomes the limit: never below max x 1.1.
        assert equal.proposed >= 500 * MIB * 1.1 > keep.proposed
        assert "equal to the request" in equal.limit_advice[0].text
        assert equal.limit_advice[0].minimum == equal.proposed

    def test_keep_asks_to_raise_a_memory_limit_below_the_new_floor(self):
        summary = _summary(
            memory_request=1024 * MIB,
            memory_limit=1100 * MIB,
            memory=_stats(avg=700 * MIB, p95=800 * MIB, p99=850 * MIB, peak=1050 * MIB),
        )
        proposal = memory_proposal(Config(), summary)
        assert proposal is not None and proposal.direction == "increase"
        assert "working_set_near_limit" in proposal.triggers
        assert proposal.limit_advice and "Raise the memory limit (1100MiB)" in proposal.limit_advice[0].text
        advice = proposal.limit_advice[0]
        assert (advice.resource, advice.action, advice.current) == ("memory", "raise", 1100 * MIB)
        assert advice.minimum is not None and advice.minimum >= 1050 * MIB * 1.1

    def test_invalid_policy_is_rejected(self):
        with pytest.raises(ValueError):
            Config(RECOMMENDATION_LIMITS_POLICY="drop_everything")


def _context_with_rows(*, cpu_usage: int, cpu_request: int, days: float) -> OptimizationContext:
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(days=days)
    rows = []
    steps = int(days * 24)
    for hour in range(steps):
        rows.append(
            CombinedMetric(
                pod_name="api-7d9f8b6c5d-abcde",
                namespace="shop",
                owner_kind="Deployment",
                owner_name="api",
                container_name="web",
                timestamp=start + timedelta(hours=hour),
                duration_seconds=3600,
                cpu_request=cpu_request,
                memory_request=512 * MIB,
                cpu_usage_millicores=cpu_usage,
                memory_usage_bytes=100 * MIB,
                total_cost=0.05,
                cost_cpu=0.03,
                cost_ram=0.02,
                cost_source="opencost",
                node="node-a",
            )
        )
    return OptimizationContext(config=Config(), metrics=rows, analysis_window_seconds=days * 86400)


class TestPercentilesFromSteps:
    def test_decrease_targets_use_per_step_peaks_not_averages(self):
        """A one-hour burst is invisible in hourly averages but drives the per-step p99 (E9)."""
        start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(days=8)
        rows = []
        for step in range(8 * 24 * 12):
            ts = start + timedelta(minutes=5 * step)
            # Usage is 50m except a daily 30-minute burst at 600m (about 2% of steps).
            burst = ts.hour == 3 and ts.minute < 30
            rows.append(
                CombinedMetric(
                    pod_name="worker-0",
                    namespace="batch",
                    owner_kind="StatefulSet",
                    owner_name="worker",
                    timestamp=ts,
                    duration_seconds=300,
                    cpu_request=2000,
                    memory_request=512 * MIB,
                    cpu_usage_millicores=600 if burst else 50,
                    memory_usage_bytes=100 * MIB,
                    total_cost=0.001,
                )
            )
        context = OptimizationContext(config=Config(), metrics=rows, analysis_window_seconds=8 * 86400)
        summary = context.usage_snapshot().summaries[("batch", "StatefulSet", "worker")][""]
        assert summary.cpu is not None
        # The burst hour has p99 600m; its hourly average (300m) would hide it.
        assert summary.cpu.p99 == pytest.approx(600)
        assert summary.cpu.max == pytest.approx(600)

        (rec,) = CpuRightsizingAnalyzer(context.config).analyze(context)
        assert rec.recommended_cpu_request_millicores is not None
        assert rec.recommended_cpu_request_millicores >= 600 * 0.5 * 1.2  # (avg + max) / 2 x headroom

        # Evidence statistics come from the same distribution as the analyzer's.
        evidence = build_evidence(rec, context)
        assert evidence is not None and evidence.cpu_usage is not None
        assert evidence.cpu_usage.max == pytest.approx(600)


class TestNodes:
    def test_underutilized_node_type_is_never_emitted(self):
        rows = [
            CombinedMetric(
                pod_name="solo",
                namespace="default",
                node="lonely",
                cpu_usage_millicores=50,
                memory_usage_bytes=100 * MIB,
                timestamp=datetime.now(timezone.utc) - timedelta(hours=1),
                duration_seconds=300,
            )
        ]
        node = NodeInfo(name="lonely", cpu_capacity_cores=8.0, memory_capacity_bytes=32 * 1024 * MIB)
        context = OptimizationContext(config=Config(), metrics=rows, node_infos=[node])
        recs = NodesAnalyzer(context.config).analyze(context)
        assert [rec.type for rec in recs] == [RecommendationType.OVERPROVISIONED_NODE]
        assert RecommendationType.UNDERUTILIZED_NODE.value == "UNDERUTILIZED_NODE"


def test_memory_analyzer_reports_the_reliability_cause():
    context = _context_with_rows(cpu_usage=100, cpu_request=1000, days=1)
    for row in context.metrics:
        row.memory_usage_bytes = 500 * MIB
    context.usage = None
    (rec,) = MemoryRightsizingAnalyzer(context.config).analyze(context)
    assert rec.priority == "high"
    assert "Reliability" in rec.reason
    assert rec.potential_savings_cost is not None and rec.potential_savings_cost < 0
