import pytest

from greenkube.api.metrics_endpoint import get_metrics_output
from greenkube.core.observability import (
    ANALYZER_FAILURES,
    AUTOMATION_BACKLOG,
    AUTOMATION_WORKER_HEARTBEAT_TIMESTAMP,
    HEALTH_GATE_FAILURES,
    OPTIMIZATION_RUNS,
    VERIFICATION_OUTCOMES,
    record_analyzer_failure,
    record_automation_backlog,
    record_automation_failure,
    record_automation_plan,
    record_automation_queue_age,
    record_automation_worker_heartbeat,
    record_gitops_discovery_refresh,
    record_plan_operation,
    record_plan_operation_duration,
    record_pr_poll_cycle,
    record_pr_poll_error,
    record_pull_request_merge_detection,
    record_pull_request_state,
    record_run,
    record_sync_outcome,
    record_verification,
    record_webhook_delivery,
    set_automation_worker_up,
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


def test_gitops_lifecycle_metrics_are_exported_with_bounded_labels():
    record_automation_plan("validated", "raw", 0.25)
    record_automation_queue_age(30)
    record_automation_worker_heartbeat(1_798_000_000)
    record_plan_operation("retrying", "rollback")
    record_plan_operation_duration("rollback", 2)
    set_automation_worker_up(True)
    record_pull_request_state("merged", "github")
    record_pr_poll_cycle("lease_contended", 0.1)
    record_pr_poll_error()
    record_pull_request_merge_detection(15)
    record_webhook_delivery("processed", 0.05)
    record_gitops_discovery_refresh(
        status="succeeded",
        duration_seconds=0.5,
        binding_counts={"resolved": 2, "ambiguous": 1},
        coverage_ratio=2 / 3,
        source_age_seconds=12,
        refreshed_at_timestamp=1_798_000_000,
    )
    record_sync_outcome("timeout")
    record_automation_failure("worker")

    # Untrusted and identifier-like values must never become Prometheus labels.
    record_automation_plan("plan-8e1c", "https://repo.example/acme", 0.1)
    record_plan_operation("private-plan-id", "https://repo.example/acme")
    record_pull_request_state("closed", "provider-main")
    record_webhook_delivery("delivery-secret", 0.1)

    output = get_metrics_output().decode()
    assert 'greenkube_automation_plans_total{renderer="raw",status="validated"}' in output
    assert 'greenkube_automation_operations_total{status="retrying",type="rollback"}' in output
    assert 'greenkube_automation_operation_duration_seconds_count{type="rollback"}' in output
    assert "greenkube_automation_queue_age_seconds_count" in output
    assert "greenkube_automation_worker_up 1.0" in output
    assert "greenkube_automation_worker_heartbeat_timestamp_seconds" in output
    assert AUTOMATION_WORKER_HEARTBEAT_TIMESTAMP._value.get() == 1_798_000_000
    assert 'greenkube_pull_requests_total{provider="github",state="merged"}' in output
    assert 'greenkube_gitops_pr_poll_cycles_total{outcome="lease_contended"}' in output
    assert "greenkube_gitops_pr_poll_errors_total" in output
    assert 'greenkube_webhook_deliveries_total{status="processed"}' in output
    assert "greenkube_webhook_delivery_lag_seconds_count" in output
    assert "greenkube_pull_request_merge_detection_seconds_count" in output
    assert 'greenkube_gitops_discovery_refreshes_total{status="succeeded"}' in output
    assert 'greenkube_gitops_bindings_total{status="resolved"}' in output
    assert "greenkube_gitops_coverage_ratio" in output
    assert "greenkube_gitops_source_age_seconds" in output
    assert 'greenkube_gitops_sync_outcomes_total{outcome="timeout"}' in output
    assert "greenkube_automation_failures_total" in output
    assert "private-plan-id" not in output
    assert "delivery-secret" not in output
    assert "provider-main" not in output
    assert "repo.example" not in output
    set_automation_worker_up(False)


def test_automation_backlog_metrics_export_bounded_state_counts():
    from time import time

    record_automation_backlog(
        {
            "queued_operations": 2,
            "running_operations": 1,
            "retrying_operations": 3,
            "failed_operations": 1,
            "stuck_operations": 1,
            "applying_plans": 1,
            "merged_plans": 2,
            "stuck_plans": 1,
            "sync_failed_plans": 1,
            "pending_webhooks": 4,
            "stuck_webhooks": 2,
            "worker_count": 2,
            "stale_workers": 1,
            "latest_worker_heartbeat_timestamp": time() - 30,
        }
    )

    output = get_metrics_output().decode()
    assert 'greenkube_automation_backlog{queue="operations",state="queued"} 2.0' in output
    assert 'greenkube_automation_backlog{queue="plans",state="sync_failed"} 1.0' in output
    assert 'greenkube_automation_backlog{queue="webhooks",state="stuck"} 2.0' in output
    assert 'greenkube_automation_backlog{queue="workers",state="stale"} 1.0' in output
    assert "greenkube_automation_worker_heartbeat_age_seconds" in output
    assert AUTOMATION_BACKLOG.labels(queue="operations", state="queued")._value.get() == 2
    assert AUTOMATION_WORKER_HEARTBEAT_TIMESTAMP._value.get() > 0

    with pytest.raises(ValueError, match="queued_operations"):
        record_automation_backlog(
            {
                "queued_operations": True,
            }
        )
