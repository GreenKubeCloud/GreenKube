# Observability and SLOs

GreenKube exposes dashboard metrics and control-loop telemetry at `/prometheus/metrics`. The API endpoint is authenticated when `GREENKUBE_API_KEY` is configured and is scraped by the chart `ServiceMonitor`. In production worker mode, a separate ClusterIP Service and ServiceMonitor scrape the automation worker's own process-local control-loop registry; configure `secrets.apiKey` or `secrets.existingSecret` for bearer authentication. Control-loop metrics use bounded labels only; workload names and namespaces remain in the existing dashboard gauges.

The targets below are example operational objectives, not service guarantees or reported measurements. API availability requires ingress or service-level telemetry from the operator's environment.

## Service-level objectives

| SLO | Target | Prometheus signal |
| --- | --- | --- |
| Successful optimization runs | >= 99% over 30 days | `rate(greenkube_optimization_runs_total{status="succeeded"}[30d]) / rate(greenkube_optimization_runs_total[30d])` |
| Verification rollback reviews | < 5% of completed verifications | `rate(greenkube_verification_outcomes_total{outcome="rollback_review"}[6h]) / rate(greenkube_verification_outcomes_total[6h])` |
| Fresh collection data | < 15 minutes old | `time() - greenkube_last_collection_timestamp_seconds` |
| Fresh GitOps discovery | < 15 minutes old | `time() - greenkube_gitops_discovery_last_success_timestamp_seconds` |
| Supported-layout binding coverage | >= 95% | `greenkube_gitops_coverage_ratio` |
| Plan creation p95 | < 30 seconds | `histogram_quantile(0.95, sum by (le, renderer) (rate(greenkube_automation_plan_duration_seconds_bucket[15m])))` |
| Queue age at claim p95 | < 5 minutes | `histogram_quantile(0.95, sum by (le) (rate(greenkube_automation_queue_age_seconds_bucket[15m])))` |
| Webhook ingress-to-response p95 | < 5 seconds | `histogram_quantile(0.95, sum by (le) (rate(greenkube_webhook_delivery_lag_seconds_bucket[15m])))` |
| API availability | >= 99.9% | ingress/service HTTP metrics |

## Alerting examples

```yaml
groups:
- name: greenkube-control-loop
  rules:
  - alert: GreenKubeOptimizationRunsFailing
    expr: |
      sum(rate(greenkube_optimization_runs_total{status="failed"}[15m]))
      / clamp_min(sum(rate(greenkube_optimization_runs_total[15m])), 1) > 0.05
    for: 15m
    labels: { severity: warning }
    annotations:
      summary: "GreenKube optimization run failure rate is above 5%"
  - alert: GreenKubeVerificationRollbackReviews
    expr: |
      sum(rate(greenkube_verification_outcomes_total{outcome="rollback_review"}[6h]))
      / clamp_min(sum(rate(greenkube_verification_outcomes_total[6h])), 1) > 0.05
    for: 30m
    labels: { severity: warning }
    annotations:
      summary: "GreenKube verification rollback reviews exceed 5%"
  - alert: GreenKubeCollectionStale
    expr: time() - greenkube_last_collection_timestamp_seconds > 900
    for: 10m
    labels: { severity: critical }
    annotations:
      summary: "GreenKube metrics collection is stale"
  - alert: GreenKubeGitOpsDiscoveryStale
    expr: time() - greenkube_gitops_discovery_last_success_timestamp_seconds > 900
    for: 10m
    labels: { severity: warning }
    annotations:
      summary: "GitOps binding discovery has not completed successfully"
  - alert: GreenKubeGitOpsSyncTimeout
    expr: increase(greenkube_gitops_sync_timeouts_total[15m]) > 0
    for: 5m
    labels: { severity: warning }
    annotations:
      summary: "A merged GreenKube plan exceeded its GitOps sync timeout"
```

## Auditability

Every optimization run emits a structured `optimization_run_completed` audit event with status, duration, and recommendation count. Every verification emits `verification_completed` with its verdict and failed health gates. Persisted optimization runs, recommendation lifecycle events, and the measured savings ledger remain the source of truth for reconstruction; Prometheus is for alerting and trend analysis, not financial accounting.
