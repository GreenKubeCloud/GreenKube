from greenkube.api.metrics_endpoint import get_metrics_output
from greenkube.core.observability import (
    ANALYZER_FAILURES,
    HEALTH_GATE_FAILURES,
    OPTIMIZATION_RUNS,
    VERIFICATION_OUTCOMES,
    record_analyzer_failure,
    record_run,
    record_verification,
)


def test_control_loop_metrics_are_exposed_with_stable_labels():
    record_analyzer_failure("cpu_rightsizing")
    record_run("succeeded", 0.25, recommendation_count=2)
    record_verification("rollback_review", ["readiness"])

    output = get_metrics_output().decode()
    assert "greenkube_optimization_runs_total" in output
    assert 'status="succeeded"' in output
    assert "greenkube_optimization_analyzer_failures_total" in output
    assert 'source="cpu_rightsizing"' in output
    assert "greenkube_verification_outcomes_total" in output
    assert 'outcome="rollback_review"' in output
    assert HEALTH_GATE_FAILURES.labels(gate="readiness")._value.get() >= 1
    assert OPTIMIZATION_RUNS.labels(status="succeeded")._value.get() >= 1
    assert VERIFICATION_OUTCOMES.labels(outcome="rollback_review")._value.get() >= 1
    assert ANALYZER_FAILURES.labels(source="cpu_rightsizing")._value.get() >= 1
