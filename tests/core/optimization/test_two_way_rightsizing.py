"""Two-way rightsizing, coverage gate and limits policy (roadmap E9)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from greenkube.core.config import Config
from greenkube.core.optimization.analyzers import CpuRightsizingAnalyzer, MemoryRightsizingAnalyzer, NodesAnalyzer
from greenkube.core.optimization.analyzers.rightsizing import (
    CONTAINER_USAGE_EXCEEDS_POD,
    POD_LEVEL_USAGE_ONLY,
    bounded_by_pod,
    cpu_proposal,
    memory_proposal,
    skipped_increase,
)
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.evidence import build_evidence
from greenkube.models.metrics import CombinedMetric, RecommendationType
from greenkube.models.node import NodeInfo
from greenkube.models.workload_usage import UsageSnapshot, UsageStats, WorkloadUsageSummary

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
        # Known-clean reliability history: unknown signals withhold decreases (REC-04).
        "oom_kill_count": 0,
        "restarts_in_window": 0,
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
                restart_count=0,
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
                    restart_count=0,
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
                restart_count=0,
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


class TestIncreaseSizing:
    """How increases are sized (USER BUG 2): robust statistic, explained cap, no pod-level guess."""

    def test_a_single_startup_spike_does_not_size_the_increase(self):
        # Steady p99 at 450Mi over a 400Mi request; one WAL-replay spike at 1.1Gi.
        summary = _summary(
            memory_request=400 * MIB,
            memory=_stats(avg=380 * MIB, p95=440 * MIB, p99=450 * MIB, peak=1100 * MIB),
        )
        proposal = memory_proposal(Config(), summary)
        assert proposal is not None and proposal.direction == "increase"
        assert proposal.basis == "p99"
        assert proposal.proposed == math.ceil(450 * 1.2) * MIB
        assert not proposal.capped

    def test_oom_kills_size_from_the_observed_maximum_without_a_cap(self):
        summary = _summary(
            memory_request=400 * MIB,
            oom_kill_count=3,
            memory=_stats(avg=380 * MIB, p95=440 * MIB, p99=450 * MIB, peak=1100 * MIB),
        )
        proposal = memory_proposal(Config(), summary)
        assert proposal is not None
        assert proposal.basis == "observed_max" and proposal.cap_factor is None
        assert proposal.proposed == math.ceil(1100 * 1.1) * MIB
        assert proposal.proposed > 2 * 400 * MIB

    def test_one_step_is_capped_at_twice_the_request_and_explained(self):
        summary = _summary(
            memory_request=400 * MIB,
            memory=_stats(avg=900 * MIB, p95=1000 * MIB, p99=1100 * MIB, peak=1200 * MIB),
        )
        proposal = memory_proposal(Config(), summary)
        assert proposal is not None and proposal.capped
        assert proposal.proposed == 800 * MIB
        assert proposal.raw_proposed == math.ceil(1100 * 1.2) * MIB

        context = OptimizationContext(config=Config(), usage=_snapshot(summary))
        (rec,) = MemoryRightsizingAnalyzer(context.config).analyze(context)
        assert rec.recommended_memory_request_bytes == 800 * MIB
        assert "Capped at 2x the current request" in rec.reason
        assert "1320MiB" in rec.reason

    def test_the_cap_is_configurable_and_applies_to_cpu(self):
        hot = _summary(cpu_request=100, cpu=_stats(avg=300, p95=380, p99=400, peak=450))
        capped = cpu_proposal(Config(), hot)
        assert capped is not None and capped.proposed == 200 and capped.capped
        wide = cpu_proposal(_config(RIGHTSIZING_MAX_INCREASE_FACTOR=10.0), hot)
        assert wide is not None and wide.proposed == 480 and not wide.capped
        with pytest.raises(ValueError):
            Config(RIGHTSIZING_MAX_INCREASE_FACTOR=1.0)

    def test_pod_level_usage_is_skipped_for_increases_and_flagged_for_decreases(self):
        hot = _summary(
            container_name="", memory_request=400 * MIB, memory=_stats(500 * MIB, 600 * MIB, 640 * MIB, 650 * MIB)
        )
        assert memory_proposal(Config(), hot) is None
        assert skipped_increase(Config(), hot, "memory") == POD_LEVEL_USAGE_ONLY
        assert skipped_increase(Config(), _summary(), "memory") is None

        context = OptimizationContext(config=Config(), usage=_snapshot(hot))
        analyzer = MemoryRightsizingAnalyzer(context.config)
        assert analyzer.analyze(context) == []
        assert analyzer.skipped == [(("shop", "Deployment", "api"), None, "memory", POD_LEVEL_USAGE_ONLY)]

        idle = _summary(container_name="")
        context = OptimizationContext(config=Config(), usage=_snapshot(idle))
        (rec,) = MemoryRightsizingAnalyzer(context.config).analyze(context)
        assert rec.recommended_memory_request_bytes is not None and rec.recommended_memory_request_bytes < 1024 * MIB
        assert "Pod-level usage only" in rec.reason

    def test_jvm_hint_is_explained_on_memory_decreases(self):
        summary = _summary(jvm_hint="JAVA_TOOL_OPTIONS")
        context = OptimizationContext(config=Config(), usage=_snapshot(summary))
        (rec,) = MemoryRightsizingAnalyzer(context.config).analyze(context)
        assert "JVM runtime suspected (JAVA_TOOL_OPTIONS)" in rec.reason


class TestContainerBoundedByItsPod:
    """USER BUG 2: a container's usage above its pod's (Metrics API page cache) never sizes an increase."""

    @staticmethod
    def _inflated() -> tuple[WorkloadUsageSummary, WorkloadUsageSummary]:
        # Minikube/cri-dockerd: the Metrics API reports ~1Gi for a container whose pod cgroup holds ~610Mi.
        container = _summary(
            memory_request=400 * MIB,
            memory=_stats(avg=900 * MIB, p95=1000 * MIB, p99=1050 * MIB, peak=1072 * MIB),
            cpu_request=100,
            cpu=_stats(avg=300, p95=380, p99=400, peak=450),
        )
        pod = _summary(
            container_name="",
            memory_request=450 * MIB,
            memory=_stats(avg=540 * MIB, p95=600 * MIB, p99=606 * MIB, peak=640 * MIB),
            cpu_request=110,
            cpu=_stats(avg=20, p95=24, p99=25, peak=30),
        )
        return container, pod

    def test_memory_increase_is_sized_from_the_pod_when_the_container_exceeds_it(self):
        container, pod = self._inflated()
        unbounded = memory_proposal(Config(), container)
        assert unbounded is not None and unbounded.capped  # the cap alone hid the bad basis

        proposal = memory_proposal(Config(), container, pod)
        assert proposal is not None and proposal.direction == "increase"
        assert proposal.evidence == [CONTAINER_USAGE_EXCEEDS_POD]
        assert proposal.proposed == math.ceil(606 * 1.2) * MIB
        assert not proposal.capped

        context = OptimizationContext(config=Config(), usage=_snapshot(container, pod))
        (rec,) = MemoryRightsizingAnalyzer(context.config).analyze(context)
        assert rec.recommended_memory_request_bytes == math.ceil(606 * 1.2) * MIB
        assert "pod's p99 (606MiB" in rec.reason and CONTAINER_USAGE_EXCEEDS_POD in rec.reason
        assert "Capped" not in rec.reason and "usage suggests" not in rec.reason

    def test_cpu_above_the_pod_triggers_nothing_once_bounded(self):
        container, pod = self._inflated()
        assert cpu_proposal(Config(), container) is not None
        # Bounded by the pod (p99 25m of a 100m request): no increase, and no decrease from contradicting data.
        assert cpu_proposal(Config(), container, pod) is None
        context = OptimizationContext(config=Config(), usage=_snapshot(container, pod))
        assert CpuRightsizingAnalyzer(context.config).analyze(context) == []

    def test_consistent_containers_are_sized_from_their_own_usage(self):
        container = _summary(memory_request=400 * MIB, memory=_stats(500 * MIB, 560 * MIB, 590 * MIB, 600 * MIB))
        pod = _summary(container_name="", memory=_stats(520 * MIB, 590 * MIB, 610 * MIB, 615 * MIB))
        proposal = memory_proposal(Config(), container, pod)
        assert proposal is not None and proposal.evidence == []
        assert proposal.proposed == math.ceil(590 * 1.2) * MIB

    def test_sub_mebibyte_noise_does_not_block_a_decrease(self):
        # Recorded on Minikube (e2e sample oversized-web, 384Mi requested): the Metrics API
        # container working set (p99 6.7MiB) sits 0.9MiB above the pod cgroup's (5.8MiB),
        # 15% in relative terms, so the decrease was dropped as contradicting data.
        container = _summary(
            memory_request=384 * MIB,
            memory=_stats(avg=6773418, p95=7041024, p99=7041024, peak=7041024),
            cpu_request=10,
            cpu=_stats(avg=0, p95=0, p99=0, peak=0),
        )
        pod = _summary(
            container_name="",
            memory_request=384 * MIB,
            memory=_stats(avg=6513384, p95=6127616, p99=6127616, peak=7335936),
        )
        proposal = memory_proposal(_config(RECOMMENDATION_MIN_DECREASE_COVERAGE_DAYS=1), container, pod)
        assert proposal is not None and proposal.direction == "decrease"
        assert proposal.evidence == []
        # A few millicores of difference are noise too.
        cpu_container = _summary(cpu_request=400, cpu=_stats(avg=3, p95=4, p99=4, peak=5))
        cpu_pod = _summary(container_name="", cpu=_stats(avg=1, p95=1, p99=1, peak=2))
        cpu = cpu_proposal(_config(RECOMMENDATION_MIN_DECREASE_COVERAGE_DAYS=1), cpu_container, cpu_pod)
        assert cpu is not None and cpu.direction == "decrease"

    def test_bounding_needs_pod_samples(self):
        stats = _stats(900 * MIB, 1000 * MIB, 1050 * MIB, 1072 * MIB)
        empty = UsageStats(sample_count=0, avg=0, p50=0, p90=0, p95=0, p99=0, max=0)
        assert bounded_by_pod(stats, empty) == (stats, False)
        assert bounded_by_pod(stats, None) == (stats, False)
        bounded, flagged = bounded_by_pod(stats, _stats(540 * MIB, 600 * MIB, 606 * MIB, 640 * MIB))
        assert flagged and bounded is not None and bounded.p99 == 606 * MIB and bounded.max == 640 * MIB


def _snapshot(*summaries: WorkloadUsageSummary) -> UsageSnapshot:
    snapshot = UsageSnapshot(source="rows")
    for summary in summaries:
        snapshot.summaries.setdefault(summary.target, {})[summary.container_name] = summary
    return snapshot
