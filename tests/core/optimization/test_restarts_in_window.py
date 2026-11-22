"""restart_count is a lifetime counter: evidence counts the restarts inside the window (COL-20)."""

from datetime import datetime, timedelta, timezone

from greenkube.core.config import Config
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.evidence import build_evidence
from greenkube.core.optimization.restarts import restarts_in_window
from greenkube.models.metrics import CombinedMetric, Recommendation, RecommendationType

NOW = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)


def _rows(counters, pod="api-7d9f8b6c5-x2k4p"):
    return [
        CombinedMetric(
            pod_name=pod,
            namespace="prod",
            owner_kind="Deployment",
            owner_name="api",
            container_name="api",
            cpu_request=1000,
            memory_request=2 * 1024**3,
            cpu_usage_millicores=200,
            memory_usage_bytes=512 * 1024**2,
            restart_count=counter,
            total_cost=0.01,
            co2e_grams=1.0,
            timestamp=NOW + timedelta(hours=i),
        )
        for i, counter in enumerate(counters)
    ]


def _memory_rec():
    return Recommendation(
        pod_name="api",
        container_name="api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_MEMORY,
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        description="reduce memory",
        current_memory_request_bytes=2 * 1024**3,
        recommended_memory_request_bytes=1024**3,
    )


def _evidence(rows):
    context = OptimizationContext(
        config=Config(),
        metrics=rows,
        analysis_window_seconds=len(rows) * 3600,
        window_start=NOW,
        window_end=NOW + timedelta(hours=len(rows)),
    )
    return build_evidence(_memory_rec(), context)


def test_a_constant_counter_is_no_restart_in_the_window():
    evidence = _evidence(_rows([5] * 6))
    assert evidence is not None and evidence.restart_count == 0


def test_a_counter_going_from_5_to_7_is_two_restarts():
    evidence = _evidence(_rows([5, 5, 6, 6, 7, 7]))
    assert evidence is not None and evidence.restart_count == 2


def test_restarts_add_over_pods_and_a_recreated_pod_starts_from_zero():
    rows = _rows([5, 6]) + _rows([0, 0, 1], pod="api-7d9f8b6c5-new01")
    assert restarts_in_window(rows) == 2
    # A counter reset (container runtime restart) is never a negative restart count.
    assert restarts_in_window(_rows([3, 0, 1])) == 1
    assert restarts_in_window(_rows([None, None])) is None
