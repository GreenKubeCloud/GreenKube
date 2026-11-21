# Observability and SLOs

GreenKube exposes dashboard metrics and control-loop telemetry at `/prometheus/metrics`. The endpoints require a bearer token and are scraped by the chart ServiceMonitors (API, controller and, with `worker.replicas` > 0, a separate ClusterIP Service and ServiceMonitor for the dedicated plan executors' own process-local control-loop registry), which send the `metrics:read` scrape token `GREENKUBE_METRICS_TOKEN`. Prometheus Operator reads that token's Secret in the ServiceMonitor's own namespace: the ServiceMonitors default to the release namespace, next to it, and with `monitoring.serviceMonitor.namespace` set elsewhere the chart keeps a copy, Secret `<release>-scrape-token`, in that namespace ([Monitoring](configuration.md#monitoring-monitoring)). A ServiceMonitor whose Secret is missing is rejected (`secret not found` in the operator logs) and Prometheus has no GreenKube target, so every alert below that reads `up` or a GreenKube metric stays silent or fires as absent. Every periodic job runs in the controller (see [Process topology](architecture.md#process-topology)), so the telemetry of the loops, of the automation loop (webhook outbox, PR polling, plan expiry) and of controller plan execution comes from the controller listener. Control-loop metrics use bounded labels only; workload names and namespaces remain in the existing dashboard gauges.

Recommendation scans and verification run in the controller, so `greenkube_optimization_*`, `greenkube_recommendations_generated_total`, `greenkube_last_successful_optimization_run_timestamp_seconds` and `greenkube_verification_*` come from the controller's own listener (port `9102`, Service `<release>-controller-metrics`, scraped by the controller ServiceMonitor), not from the API. Every controller replica is scraped and only the Lease holder scans, so the queries below aggregate with `sum()` (counters) and `max()` (timestamps).

The targets below are example operational objectives, not service guarantees or reported measurements. API availability requires ingress or service-level telemetry from the operator's environment.

## Service-level objectives

| SLO | Target | Prometheus signal |
| --- | --- | --- |
| Successful optimization runs | >= 99% over 30 days | `rate(greenkube_optimization_runs_total{status="succeeded"}[30d]) / rate(greenkube_optimization_runs_total[30d])` |
| Verification rollback reviews | < 5% of completed verifications | `rate(greenkube_verification_outcomes_total{outcome="rollback_review"}[6h]) / rate(greenkube_verification_outcomes_total[6h])` |
| Fresh collection data | < 15 minutes old | `time() - greenkube_last_collection_timestamp_seconds` |
| Collection watermark lag | < 2 collection steps | `greenkube_collection_watermark_lag_seconds{source="combined_metrics"}` |
| No abandoned collection windows | 0 per day | `increase(greenkube_collection_gap_windows_total[1d])` |
| Collection coverage of the backfill horizon | 1.0 | `greenkube_collection_coverage_ratio{source="combined_metrics"}` (steps collected or confirmed empty, from `collection_windows`; holes in `GET /api/v1/collection/coverage`) |
| Fresh GitOps discovery | < 15 minutes old | `time() - greenkube_gitops_discovery_last_success_timestamp_seconds` |
| Supported-layout binding coverage | >= 95% | `greenkube_gitops_coverage_ratio` |
| Plan creation p95 | < 30 seconds | `histogram_quantile(0.95, sum by (le, renderer) (rate(greenkube_automation_plan_duration_seconds_bucket[15m])))` |
| Queue age at claim p95 | < 5 minutes | `histogram_quantile(0.95, sum by (le) (rate(greenkube_automation_queue_age_seconds_bucket[15m])))` |
| Webhook ingress-to-response p95 | < 5 seconds | `histogram_quantile(0.95, sum by (le) (rate(greenkube_webhook_delivery_lag_seconds_bucket[15m])))` |
| API availability | >= 99.9% | ingress/service HTTP metrics |
| Controller loops healthy | every loop succeeded within 3 intervals | `time() - max by (loop) (greenkube_loop_last_success_timestamp_seconds)` against each loop's interval |
| Exactly one active controller | 1 | `sum(greenkube_controller_leader)` |

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
  - alert: GreenKubeRecommendationScansStale
    # Two missed scans at the default RECOMMENDATION_SCAN_INTERVAL (1h); adjust to
    # your interval. "> 0" ignores controllers that have not scanned since start.
    expr: time() - max(greenkube_last_successful_optimization_run_timestamp_seconds > 0) > 7200
    for: 15m
    labels: { severity: warning }
    annotations:
      summary: "No recommendation scan has succeeded for two scan intervals"
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
  - alert: GreenKubeCollectionWatermarkLagging
    expr: greenkube_collection_watermark_lag_seconds{source="combined_metrics"} > 900
    for: 15m
    labels: { severity: warning }
    annotations:
      summary: "GreenKube has not completed a collection window for 15 minutes (it will backfill on recovery)"
  - alert: GreenKubeCollectionGap
    expr: increase(greenkube_collection_gap_windows_total[1h]) > 0
    labels: { severity: warning }
    annotations:
      summary: "Collection windows older than COLLECTION_MAX_BACKFILL were skipped"
  - alert: GreenKubeGitOpsDiscoveryStale
    expr: time() - greenkube_gitops_discovery_last_success_timestamp_seconds > 900
    for: 10m
    labels: { severity: warning }
    annotations:
      summary: "GitOps binding discovery has not completed successfully"
  - alert: GreenKubeControllerLoopFailing
    # A loop failed or timed out on its last three ticks (any loop, any replica).
    expr: max by (loop) (greenkube_loop_consecutive_failures) >= 3
    for: 10m
    labels: { severity: warning }
    annotations:
      summary: "Controller loop {{ $labels.loop }} keeps failing; see /loops on the controller metrics port"
  - alert: GreenKubeAutomationLoopStale
    # The automation controller ticks every 5 s on the Lease holder.
    expr: time() - max(greenkube_loop_last_success_timestamp_seconds{loop="automation-controller"}) > 600
    for: 5m
    labels: { severity: warning }
    annotations:
      summary: "Plan expiry, webhook outbox and PR polling have not completed for 10 minutes"
  - alert: GreenKubeNoActiveController
    # "or vector(0)": also fires when no controller is running or scraped at all.
    expr: (max(greenkube_controller_leader) or vector(0)) < 1
    for: 5m
    labels: { severity: critical }
    annotations:
      summary: "No controller replica holds the Lease: collection, scans and automation are stopped"
  - alert: GreenKubeGitOpsSyncTimeout
    expr: increase(greenkube_gitops_sync_timeouts_total[15m]) > 0
    for: 5m
    labels: { severity: warning }
    annotations:
      summary: "A merged GreenKube plan exceeded its GitOps sync timeout"
```

## Controller loops

Every periodic job of the controller is a registered loop (`src/greenkube/core/loops.py`, roadmap M8). The registry records, per loop:

| Metric | Meaning |
| --- | --- |
| `greenkube_loop_runs_total{loop,outcome}` | Ticks by outcome: `success`, `failure` (the job raised or reported a failure), `timeout` (the tick exceeded the loop timeout; a `TimeoutError` raised by the job itself counts as `failure`), `skipped` (leader-only loop on a standby replica). |
| `greenkube_loop_duration_seconds{loop}` | Duration of executed ticks (histogram). |
| `greenkube_loop_last_success_timestamp_seconds{loop}` | Unix time of the last successful tick. |
| `greenkube_loop_consecutive_failures{loop}` | Failed or timed-out ticks since the last success. |
| `greenkube_controller_leader` | `1` on the replica that runs leader-only loops (the Lease holder, or any controller without leader election). |

`loop` takes one of these values: `collection`, `carbon-intensity`, `node-analysis`, `savings-attribution`, `recommendation-lifecycle`, `savings-drift`, `ledger-chain`, `recommendation-scan`, `metrics-compression`, `dashboard-summary`, `gitops-discovery`, `gitops-sync`, `automation-controller`, `plan-executor`, `session-purge` and `controller-heartbeat`.

The `ledger-chain` loop also exports the integrity of the savings ledger v2 ([methodology](methodology/savings-measurement.md#12-savings-ledger-v2-c6)):

| Metric | Meaning |
| --- | --- |
| `greenkube_ledger_chain_verification_failures_total{reason}` | Verifications that found a modified, missing or reordered entry (`hash_mismatch`, `broken_link`, `missing_entry`, `identity_mismatch`, `truncated`, `head_rewritten`). |
| `greenkube_ledger_chain_ok{cluster_uid}` | 1 when the last verification of the cluster's chain passed, 0 when it failed (failures are sticky until the anchor is reset). Alerted by `GreenKubeLedgerChainVerificationFailed` (critical, `== 0`) with the controller alerts. |
| `greenkube_ledger_chain_head_seq{cluster_uid}`, `greenkube_ledger_chain_head_info{cluster_uid,head_seq,head_hash}` | The last verified head of each savings ledger chain. Prometheus retention keeps its history outside the database: a later head below an earlier one means entries were removed. |
| `greenkube_ledger_chain_head_keyed{cluster_uid}` | 1 when the last verified head is chained with `LEDGER_CHAIN_KEY` (HMAC), 0 otherwise. A drop from 1 to 0 in Prometheus means the chain was re-written without the key (LED-05). |
| `greenkube_ledger_unreconciled_recommendations` | Recommendations in `rollback_review`, `reverted`, `failed` or `stale` whose accruals were still live at the last ledger pass, which then superseded them (LED-08). Non-zero means the lifecycle loop's one-shot supersession failed or raced with an accrual. |
| `greenkube_ledger_reconciliation_ok{cluster_uid}` | 1 when the accrued book of the savings ledger v2 equals the non-superseded v1 accrual rows per method and quantity (relative 1e-9), checked on every `ledger-chain` tick. Alerted by `GreenKubeLedgerReconciliationFailed` (warning, `== 0` for 3 hours). |
| `greenkube_ledger_unprojected_rows{source_table}` | v1 ledger rows with a non-zero saving that have no ledger v2 entry after the last full sweep. Alerted by `GreenKubeLedgerProjectionBacklog` (warning, `> 0` for 3 hours): the v2 totals do not reconcile with the savings report. |
| `greenkube_ledger_late_rows_projected_total{source_table}` | v1 rows found by a full sweep below the incremental window (committed out of id order) and projected late. |
| `greenkube_ledger_chain_verified_entries` | Entries verified by the last pass, all clusters. |
| `greenkube_ledger_write_errors_total{reason}` | Ledger passes that could not write (`projection_failed`, `supersession_failed`, `restatement_failed`, `dual_write_failed`, `invalid_v1_row`); the next pass catches up. |

The same listener serves three more endpoints:

- `/healthz`: liveness.
- `/readyz`: `503` when a loop task died or a loop heartbeat is older than its staleness bound (interval × maximum backoff + timeout + 60 s).
- `/loops`: a JSON snapshot with each loop's state (`idle`, `running`, `standby`, `failing` (the last tick failed), `crashed`), last success, last duration, consecutive failures and last error code. It requires the API key when the controller has one.

Standby replicas report their leader-only loops as `standby`, which counts as healthy.

**Persisted heartbeats.** Every controller replica (leader or standby) runs the `controller-heartbeat` loop: every 30 s it writes its `/loops` snapshot, whether it holds the Lease and whether all its loops are ready to the `controller_heartbeats` table (one row per replica, keyed by the pod name; rows of replicas silent for a day are pruned). `GET /api/v1/automation/status` reads them and returns `controller`: `healthy` (a leader with a heartbeat younger than `stale_after_seconds` (120 s) whose loops are all ready), `leader` (its instance id), `available` (`false` when the table could not be read) and `instances` (per replica: `leader`, `ready`, `stale`, `heartbeat_at`, `heartbeat_age_seconds`, `started_at` and the per-loop `loops` snapshot).

**Chart alerts.** With `monitoring.prometheusRule.enabled`, the chart's PrometheusRule also installs `GreenKubeControllerLoopFailing`, `GreenKubeAutomationLoopStale` and `GreenKubeNoActiveController` as above (group `greenkube.controller`; `monitoring.prometheusRule.controllerAlerts: false` leaves them out). They read the controller metrics endpoint, so the chart renders them only together with the controller ServiceMonitor (`monitoring.serviceMonitor.enabled` and `collector.metricsPort` > 0).

### Chart alerts

With the controller loop alerts, the PrometheusRule installs the loop health group `greenkube.loop` (roadmap N7). Every expression reads metrics the controller already exports; thresholds are Helm values under `monitoring.prometheusRule.thresholds`, and `.github/scripts/promtool-check.sh` unit-tests the rules with `promtool` (`helm-chart/tests/promtool/`).

| Alert | Expression (simplified) | Meaning |
| --- | --- | --- |
| `GreenKubeCollectionLagging` (warning, 15 min) | `greenkube_collection_watermark_lag_seconds{source}` × `greenkube_controller_leader` > `collectionLagSeconds` (1800) | The leader's collection watermark fell behind; a standby's frozen value is ignored. |
| `GreenKubeRecommendationScanFailing` (warning, 15 min) | failed `greenkube_optimization_runs_total` in the last hour, unless one succeeded | Every scan of the last hour failed; the inbox is not refreshed. |
| `GreenKubeRecommendationSourceFailing` (warning, 1 h) | `increase(greenkube_optimization_analyzer_failures_total[2h])` by `source` > 0 | One source keeps failing; its signals miss the consensus. |
| `GreenKubeRecommendationScanStale` (warning, 15 min) | `time() - greenkube_last_successful_optimization_run_timestamp_seconds` > `recommendationScanStaleSeconds` (10800) | No successful scan for 3 hours; silent before the first success. |
| `GreenKubeMeasurementStalled` (warning, 10 min) | `time() - greenkube_loop_last_success_timestamp_seconds{loop="recommendation-lifecycle"}` > `measurementStaleSeconds` (1800) | Apply detection, verification and change measurement are not advancing, so the measurement backlog grows. |
| `GreenKubeGitProviderRateLimited` (warning, 30 min) | `sum by (provider) (increase(greenkube_git_provider_rate_limited_total[30m]))` > 0 | A Git provider keeps rate-limiting GreenKube; plan operations wait for its reset time, so pull requests are delayed. |
| `GreenKubeLedgerChainVerificationFailed` (critical, group `greenkube.ledger`) | `min by (cluster_uid) (greenkube_ledger_chain_ok) == 0` | A ledger entry no longer matches the hash chain (edited, deleted or reordered rows). Treat the ledger as untrusted, export it and compare it with your last export before anything else writes to it. |

The code exports no gauge of pending measurements yet, so the measurement backlog is alerted through the loop that drains it.

The ledger alert sits in its own group, `greenkube.ledger`, rendered whenever `monitoring.serviceMonitor.enabled` is set, even with `controllerAlerts: false` or `controller.metricsPort: 0`, because the hash-chain verification may run in the API as well as in the controller.

## Operations alerts

`monitoring.prometheusRule.operations` (on by default with `monitoring.prometheusRule.enabled`) adds the group
`greenkube.operations`. Thresholds are Helm values under the same key; `helm-chart/tests/promtool/operations-alerts.yaml`
unit-tests every rule.

| Alert | Rendered when | Expression (simplified) | Meaning |
| --- | --- | --- | --- |
| `GreenKubeAPIDown` (critical, 5 min) | `serviceMonitor.enabled` | `sum(up{job="<release>-api"}) or vector(0)` < 1 | No API pod answers its scrape, including when none exists: UI, API and webhooks are down. |
| `GreenKubeDatabaseUnavailable` (critical, 5 min) | controller ServiceMonitor | `greenkube_loop_consecutive_failures{loop="controller-heartbeat"}` >= 3 | Every controller replica writes a heartbeat row every 30 s; repeated failures mean the database refuses writes. Works for the bundled and an external PostgreSQL. |
| `GreenKubeAPIUnavailable` (critical, 10 min) | `operations.kubeStateMetrics` | `kube_deployment_status_replicas_available{deployment="<release>-api"}` < 1 | API pods exist but none is ready: pending migrations, database unreachable at startup, or a wedged loop. |
| `GreenKubePostgresDown` (critical, 5 min) | bundled PostgreSQL and `kubeStateMetrics` | `kube_statefulset_status_replicas_ready{statefulset="<release>-postgres"}` < 1 | The bundled database is not ready. |
| `GreenKubeBackupMissing` (warning, 30 min) | `postgres.backup.enabled` and `kubeStateMetrics` | last successful run of `<release>-postgres-backup` older than `backupMaxAgeHours` (36), or none since the CronJob exists | Backups are failing. |
| `GreenKubeDataVolumeLow` (warning, 15 min) | a bundled or backup claim | `kubelet_volume_stats_available_bytes / capacity` < `volumeFreeRatio` (0.15) | Expand the claim or bound the retention ([Sizing](installation.md#sizing)). |
| `GreenKubeDataVolumeFillingUp` (warning, 1 h) | a bundled or backup claim | under 40% free and `predict_linear(available[6h], volumeFullWithinHours)` < 0 | Full within 4 days at the current growth. |

kube-state-metrics and the kubelet volume statistics ship with kube-prometheus-stack; set
`operations.kubeStateMetrics: false` when kube-state-metrics is not scraped, since its alerts would never fire.

## Collection telemetry

The metric collection job publishes, on the control-loop registry:

| Metric | Meaning |
| --- | --- |
| `greenkube_collection_watermark_lag_seconds{source}` | Seconds between now and the end of the last fully written window. |
| `greenkube_collection_windows_total{source}` | Collection chunks written and committed. |
| `greenkube_collection_window_errors_total{source,reason}` | Runs stopped before committing a window (`collection_failed`, `leadership_lost`); the window is retried. |
| `greenkube_collection_k8s_list_errors_total{resource}` | Kubernetes pod or node lists that failed during collection (`pods`, `nodes`). Without a pod snapshot newer than one step (pods) or any node snapshot (nodes) the window fails and is retried, up to `COLLECTION_K8S_LIST_MAX_FAILURES` attempts. |
| `greenkube_collection_degraded_windows_total{list}` | Windows stored in degraded mode after `COLLECTION_K8S_LIST_MAX_FAILURES` consecutive failed pod or node lists (`pods`, `nodes`): their rows without history carry the reason `k8s_list_failed`. Any increase means the controller cannot list pods or nodes (RBAC, API timeouts). Alert `GreenKubeCollectionDegraded` fires when one was stored in the last hour (for 10 minutes). |
| `greenkube_collection_window_duration_seconds{source}` | Time to collect and write one chunk. |
| `greenkube_collection_steps_total{source,status}` | Steps recorded in `collection_windows`: `collected`, `empty` (Prometheus scraped, no pod ran), `failed` (no scrape data, retried while inside `COLLECTION_MAX_BACKFILL`) and `gap` (abandoned). Any `failed` or `gap` increase is a hole in the stored history. |
| `greenkube_collection_identity_mismatch` | 1 while collection refuses the database because its `installation` row holds another cluster's kube-system UID (cluster rebuilt, backup restored into a new cluster); the controller `/readyz` is then not ready with `identity_error`. Alert `GreenKubeClusterIdentityMismatch` (critical, 5 minutes). See [Backup and restore](operations/backup-restore.md#restoring-into-a-new-or-rebuilt-cluster). |
| `greenkube_collection_rate_window_scrapes{source}` | Most scrapes any cAdvisor CPU series holds inside the rate window, measured hourly. Below 2, `rate()` returns nothing and every window is a hole: set `config.prometheus.scrapeInterval` to the real scrape interval (an ERROR is logged). |
| `greenkube_collection_cost_unattributed_ratio{source}` | Share of the OpenCost allocation total of the last collected window that no stored pod row carries (pods without any CPU, memory or Running sample); a warning is logged above 1%. |
| `greenkube_collection_coverage_ratio{source}` | Share of the steps of the backfill horizon that were collected or confirmed empty, read from `collection_windows` after each run (it survives restarts). |
| `greenkube_collection_gap_windows_total{source}` | Windows abandoned because they were older than `COLLECTION_MAX_BACKFILL`. |
| `greenkube_collection_recollect_skipped_total{source,reason}` | Re-collected windows left as stored because the new pass was worse: `missing_pods` (a step of the new pass misses a stored pod) or `cost_unavailable` (no OpenCost cost where the stored rows had one). |
| `greenkube_collection_rows_rejected_total{source,reason}` | Collected rows left out of their window because they cannot be stored (`no_timestamp`); the window's steps are recorded with reason `rows_rejected`. |
| `greenkube_collection_dropped_samples_total{reason}` | Prometheus samples dropped (`non_finite`, `malformed`, `out_of_window`) or clamped (`negative_clamped`). |
| `greenkube_unknown_power_profile_nodes` | Nodes that fell back to `DEFAULT_INSTANCE_PROFILE` in the latest collection. |

These metrics are produced in the controller process; they are scraped once the controller serves its own metrics endpoint (roadmap A12).

## Auditability

Every optimization run emits a structured `optimization_run_completed` audit event with status, duration, and recommendation count. Every verification emits `verification_completed` with its verdict and failed health gates. Persisted optimization runs, recommendation lifecycle events, and the measured savings ledger remain the source of truth for reconstruction; Prometheus is for alerting and trend analysis, not financial accounting.
