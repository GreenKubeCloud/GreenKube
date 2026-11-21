# Prometheus & Grafana Integration

GreenKube has two distinct Prometheus integrations that serve different purposes.

## How they relate

| Direction | What it does | Required? |
|---|---|---|
| GreenKube → Prometheus | GreenKube queries your cluster's Prometheus for CPU and memory metrics | Strongly recommended |
| Prometheus → GreenKube | Prometheus scrapes GreenKube's `/prometheus/metrics` endpoints for GreenKube's own health (collection, loops, scans, measurement, ledger) and the savings ledger counters, for alerts and the operations dashboard | Optional |

Footprint data (cost, CO₂e and energy per pod, namespace and node) is not exported to Prometheus: per-pod and per-namespace labels multiply the series Prometheus stores. Read it in the Footprint pages, `GET /api/v1/report/summary` and `GET /api/v1/report/export` (CSV or JSON).

---

## 1. Scraping GreenKube metrics into Prometheus

### With the Prometheus Operator (kube-prometheus-stack)

Enable the `ServiceMonitor` in `values.yaml`:

```yaml
monitoring:
  serviceMonitor:
    enabled: true           # Creates a ServiceMonitor CRD resource
    namespace: ""           # Empty: the release namespace (Prometheus must select it)
    interval: 30s           # Scrape interval (30s recommended)
  networkPolicy:
    enabled: true           # Applies ingress and egress policies to GreenKube components
    prometheusNamespace: monitoring
```

Or via `--set`:

```bash
helm upgrade greenkube greenkube/greenkube \
  -n greenkube \
  --set monitoring.serviceMonitor.enabled=true \
  --set monitoring.networkPolicy.enabled=true
```

The `ServiceMonitor` scrapes the `http` port (8000) at `/prometheus/metrics` on the configured interval. It restores the exported `cluster`, `region`, `namespace`, and `pod` labels that Prometheus would otherwise rename because of scrape-target labels.

When NetworkPolicy is enabled, the chart covers the API, controller and (optional) worker pods. It permits DNS and the bundled PostgreSQL connection, and allows Prometheus to scrape the API, controller or worker metrics port. Other ingress is denied unless configured with `additionalIngress`; external database, Git provider, and Kubernetes API traffic require narrow `additionalEgress` rules. The chart does not add unrestricted internet egress.

The chart adds bearer-token authentication to the `ServiceMonitor`. Prometheus Operator reads that token's Secret in the ServiceMonitor's own namespace, so the ServiceMonitors default to the release namespace, next to the generated scrape token `GREENKUBE_METRICS_TOKEN` (`metrics:read` only; Secret `<release>`). With `monitoring.serviceMonitor.namespace` set to another namespace (for example `monitoring`), the chart keeps a copy of the scrape token there, Secret `<release>-scrape-token` (key `token`), and the ServiceMonitors use it; see [Monitoring](configuration.md#monitoring-monitoring). When no scrape token exists (`secrets.existingSecret` without `secrets.existingSecretHasMetricsToken`), the ServiceMonitors fall back to the deprecated bootstrap key `GREENKUBE_API_KEY`, which is never copied: outside the release namespace, create that Secret in the ServiceMonitor namespace yourself. You can also scrape the API with an API key that has only the `metrics:read` scope (**Settings > API keys**): store it in a Secret in the ServiceMonitor namespace and set `monitoring.serviceMonitor.bearerTokenSecret.name` (and `.key`, default `token`). The `prometheus-k8s` RoleBinding lets the Prometheus ServiceAccount (`monitoring.serviceMonitor.prometheusServiceAccount` in `monitoring.serviceMonitor.prometheusNamespace`, default `monitoring.networkPolicy.prometheusNamespace`) discover the GreenKube Services and Pods. See [Authentication](authentication.md#prometheus). If using static scrape configuration instead, configure the corresponding bearer token in Prometheus; unauthenticated scrapes will receive `401`.

### Without the Prometheus Operator

Add a static scrape job to `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: greenkube
    scrape_interval: 30s
    metrics_path: /prometheus/metrics
    static_configs:
      - targets:
          - greenkube-api.greenkube.svc.cluster.local:8000
```

If GreenKube API authentication is enabled, add the Prometheus `authorization`/bearer-token configuration for that scrape job as well.

---

## 2. Cluster labels

Set `CLUSTER_NAME` (Helm: `config.clusterName`) to use a stable cluster label. When it is empty, GreenKube derives a name in this order:

1. On a single-node cluster (minikube, kind, k3s), the name of that node.
2. A recognized EKS, GKE, AKS, Cluster API or minikube cluster-name label on a node.
3. `default` on any other multi-node cluster, so every GreenKube pod uses the same name whatever node it runs on.
4. When the nodes cannot be read (no RBAC, out of cluster, API not reachable yet at start): `default`, never the node hosting the pod.

The name also keys the collection watermark, the GitOps bindings and the savings ledger: set it before the first install of a multi-node cluster rather than after.

The API metric families (collection freshness and savings counters) use the resolved cluster value. Control-loop metrics have their own labels, documented below, and do not all include `cluster`. The chart's `ServiceMonitor` restores `cluster`, `region`, `namespace`, and `pod` labels after Prometheus target-label renaming, and replaces an empty `cluster` label with `default`. Static scrape configurations do not get those chart relabelings.

---

## 3. Available Prometheus metrics

The API registry holds seven families: collection freshness and the savings counters of the ledger. The control-loop families (collection, loops, scans, measurement, ledger health, GitOps and automation) follow; they are counters, histograms and gauges with bounded labels only. No family has a `pod` or workload label of its own (Prometheus still adds the target labels of the scraped GreenKube pod).

### API metrics

| Metric | Labels | Description |
|---|---|---|
| `greenkube_last_collection_timestamp_seconds` | `cluster` | Unix timestamp of the newest stored metric row. It keeps its last value while collection is stalled, so `time() - greenkube_last_collection_timestamp_seconds` grows |
| `greenkube_metrics_total` | `cluster` | Number of pods with a stored metric row in the last 20 minutes |
| `greenkube_co2e_savings_attributed_grams_total` | `cluster`, `recommendation_type`, `method` | **Counter.** CO₂e accrued in the savings ledger since installation (g). `method` is `projected`, `measured` or `user_declared`; only `measured` is realized savings |
| `greenkube_cost_savings_attributed_dollars_total` | `cluster`, `recommendation_type`, `method` | **Counter.** Cost accrued in the savings ledger since installation ($); the cost of reliability fixes is not a saving and is left out |
| `greenkube_co2e_savings_reversed_grams_total` | `cluster`, `recommendation_type`, `method` | **Counter.** CO₂e reversed (g): every superseded accrual (for example after a rollback review) plus negative accruals such as negative declared savings. Net savings = attributed − reversed |
| `greenkube_cost_savings_reversed_dollars_total` | `cluster`, `recommendation_type`, `method` | **Counter.** Cost reversed ($) |
| `greenkube_savings_measured_vs_projected_ratio` | `cluster` | Share of net accrued CO₂e savings that is measured rather than projected (0–1) |

The savings counters read the hash-chained ledger v2 once its accrued book reconciles with the v1 rows, and the v1 ledger before that. `increase()` and `rate()` work on them: the savings of a window are `increase(greenkube_cost_savings_attributed_dollars_total{method="measured"}[7d]) - increase(greenkube_cost_savings_reversed_dollars_total{method="measured"}[7d])`. Always filter on `method`; never add methods. Savings accrue on one-hour slots, so the counters are reloaded from the database at most once every 60 seconds per API process; scrapes in between reuse the previous values.

The savings attribution loop also exports, from the controller:

| Metric | Labels | Description |
|---|---|---|
| `greenkube_attribution_errors_total` | `reason` | **Counter.** Recommendations skipped by savings attribution (`invalid_record`), accrual batches that could not be stored (`persist_failed`), runs refused because the process clock disagrees with the database clock (`clock_skew`, logged at most every 15 minutes) or because the database clock could not be read (`clock_check_failed`) |
| `greenkube_accrual_watermark_ahead_seconds` | `cluster_name` | **Gauge.** Seconds by which the savings accrual watermark is ahead of the current hour (written by a wrong clock); 0 normally |

Collection telemetry, controller loops and ledger health metrics are listed in [Observability and SLOs](observability-slos.md).

### Optimization control-loop metrics

These metrics are process-local. The API's `/prometheus/metrics` endpoint
includes the registry of the API process serving the scrape; it does not
aggregate registries from separate controller or automation pods.

Recommendation scans and verification run in the **controller** (the
`greenkube start` process), so the optimization-run and verification metrics
below are recorded there and served by the controller's own listener:
`/prometheus/metrics` on port `9102` (see
[Controller metrics endpoint](#controller-metrics-endpoint)). The API endpoint
does not report them. The chart renders the internal ClusterIP Service
`<release>-controller-metrics`; with `monitoring.serviceMonitor.enabled=true`
it also renders a controller ServiceMonitor next to the API one, and the
optional NetworkPolicy admits Prometheus on that port. Without the Prometheus
Operator, add a scrape job for the controller Service, with an `authorization`
block carrying the scrape token `GREENKUBE_METRICS_TOKEN` when the controller
has one (the chart gives it one by default):

```yaml
- job_name: greenkube-controller
  metrics_path: /prometheus/metrics
  static_configs:
    - targets: ["greenkube-controller-metrics.greenkube.svc:9102"]
```

Every controller replica serves its registry, but only the Lease holder
scans: aggregate counters with `sum()` and the last-success timestamp with
`max()` (standbys report `0`). A run in which only some sources failed still
ends `succeeded`; the failed sources are counted in
`greenkube_optimization_analyzer_failures_total` and listed in the run's
`sources` (`GET /api/v1/optimization/runs/{run_id}`).

| Metric | Type and labels | Description |
|---|---|---|
| `greenkube_optimization_runs_total` | Counter; `status` | Completed optimization runs, with status `succeeded` or `failed` |
| `greenkube_optimization_run_duration_seconds` | Histogram; no labels | Optimization-run duration in seconds |
| `greenkube_optimization_analyzer_failures_total` | Counter; `source` | Failures by recommendation source |
| `greenkube_recommendations_generated_total` | Counter; no labels | Recommendations emitted by completed optimization runs |
| `greenkube_verification_outcomes_total` | Counter; `outcome` | Verification outcomes: `verified`, `not_realized`, `inconclusive`, `rollback_review` |
| `greenkube_verification_health_gate_failures_total` | Counter; `gate` | Verification health-gate failures: `cpu_p95`, `memory_p95`, `restarts`, `oom_kills`, `readiness`, `cpu_throttling` |
| `greenkube_last_successful_optimization_run_timestamp_seconds` | Gauge; no labels | Unix timestamp of the last fully successful optimization run |
| `greenkube_recommendation_scan_window_seconds` | Gauge; no labels | Lookback window used by the latest recommendation scan |
| `greenkube_recommendation_scan_metric_rows` | Gauge; no labels | Metric rows materialized by the latest recommendation scan |
| `greenkube_recommendation_scan_resident_memory_bytes` | Gauge; no labels | Controller peak resident memory when the latest scan was built |

### GitOps and PR-bot lifecycle metrics

These control-loop metrics reuse the process-local observability registry and
are included in `/prometheus/metrics`. They do not add repository, PR, plan,
workload, namespace, credential, or error-text labels. Provider, renderer,
status, outcome, operation type, and failure-reason labels are restricted to
the finite values described below.

| Metric | Type and labels | Description |
|---|---|---|
| `greenkube_gitops_pr_poll_cycles_total` | Counter; `outcome` (`completed`, `lease_contended`, `error`, `cancelled`) | Bounded PR-poll cycle outcomes |
| `greenkube_gitops_pr_poll_lease_contentions_total` | Counter; no labels | Cycles skipped because another worker owns the polling lease |
| `greenkube_gitops_pr_poll_errors_total` | Counter; no labels | Provider reconciliation and poll-queue errors |
| `greenkube_gitops_pr_poll_duration_seconds` | Histogram; no labels | Bounded polling cycle duration |
| `greenkube_automation_plans_total` | Counter; `status`, `renderer` (`raw`, `kustomize`, `helm_values`, `helm_release`, `unknown`) | Plan creation outcomes; `status` is a finite plan lifecycle status or `cancelled` |
| `greenkube_automation_plan_duration_seconds` | Histogram; `renderer` | Plan creation and render-validation duration |
| `greenkube_automation_operations_total` | Counter; `status`, `type` (`apply`, `rollback`, `unknown`) | Persisted plan-operation outcomes |
| `greenkube_automation_operation_duration_seconds` | Histogram; `type` | Time spent executing a claimed plan operation |
| `greenkube_automation_queue_age_seconds` | Histogram; no labels | Age of each plan operation when claimed |
| `greenkube_automation_worker_up` | Gauge; no labels | Whether the automation worker loop is active in this process |
| `greenkube_automation_worker_heartbeat_timestamp_seconds` | Gauge; no labels | Unix timestamp of the worker loop's most recent heartbeat; freshness is `time() - metric` |
| `greenkube_automation_failures_total` | Counter; `reason` (`plan`, `worker`, `polling`, `webhook`, `discovery`, `sync`, `other`) | Failures by fixed lifecycle subsystem category |
| `greenkube_pull_requests_total` | Counter; `state` (`open`, `merged`, `closed`, `unchanged`, `unknown`), `provider` (`github`, `gitlab`, `gitea`, `other`) | Lifecycle states observed through polling or webhooks |
| `greenkube_pull_request_merge_detection_seconds` | Histogram; no labels | Delay from provider-reported merge time to lifecycle detection |
| `greenkube_webhook_deliveries_total` | Counter; `status` (`received`, `processed`, `duplicate`, `ignored`, `retryable`, `reconcile_unavailable`, `rejected`, `unavailable`, `error`) | Delivery processing outcomes; `ignored` counts verified deliveries for pull requests GreenKube does not track (answered `202`, never retried, not a failure) |
| `greenkube_webhook_delivery_lag_seconds` | Histogram; no labels | Time from webhook ingress to the terminal HTTP response |
| `greenkube_gitops_discovery_refreshes_total` | Counter; `status` (`succeeded`, `failed`, `cancelled`) | Discovery refresh outcomes |
| `greenkube_gitops_bindings_total` | Gauge; `status` (`resolved`, `ambiguous`, `unresolved`, `blocked`, `stale`) | Current binding counts by resolution status |
| `greenkube_gitops_coverage_ratio` | Gauge; no labels | Resolved bindings divided by all discovered bindings |
| `greenkube_gitops_discovery_refresh_duration_seconds` | Histogram; no labels | Discovery refresh duration |
| `greenkube_gitops_source_age_seconds` | Gauge; no labels | Age of the oldest source observation at the latest successful refresh |
| `greenkube_gitops_discovery_last_success_timestamp_seconds` | Gauge; no labels | Unix timestamp for computing ongoing discovery freshness |
| `greenkube_gitops_sync_outcomes_total` | Counter; `outcome` (`synced`, `pending`, `failed`, `timeout`, `skipped`, `error`) | Bounded controller-sync observation outcomes |
| `greenkube_gitops_sync_timeouts_total` | Counter; no labels | Plans that reached the configured sync timeout |
| `greenkube_gitops_sync_lag_seconds` | Histogram; no labels | Delay from PR merge to terminal sync observation |
| `greenkube_git_provider_requests_total` | Counter; `provider` (`github`, `gitlab`, `gitea`, `other`), `operation` (`plan_apply`, `plan_rollback`, `plan_unknown`, `sync_ancestry`, `sync_planned_change`, `unscoped`) | HTTP requests sent to Git provider APIs, retries included |
| `greenkube_git_provider_operation_calls` | Histogram; `operation` | Provider requests made by one budgeted operation (a plan apply, a rollback, one sync ancestry or planned-change check) |
| `greenkube_git_provider_budget_exhausted_total` | Counter; `operation` | Operations stopped because they reached their provider call budget |
| `greenkube_git_provider_rate_limited_total` | Counter; `provider` | Git provider responses that signalled a rate limit (`429`, or a `403` with an exhausted quota or a secondary limit); the operation is rescheduled at the reset time |

These values are process-local. When the optional plan executors run
(`worker.replicas` > 0), each worker also serves `/prometheus/metrics` on
`worker.metricsPort` (default `8001`). Its ClusterIP Service and
worker-specific ServiceMonitor are rendered only when workers are deployed;
the ServiceMonitor additionally requires
`monitoring.serviceMonitor.enabled=true`. The worker endpoint exposes that
process's control-loop registry, not the API metrics. The Service is
internal-only (no Ingress or external Service type), and the endpoint requires
the scrape token `GREENKUBE_METRICS_TOKEN` (or, while `auth.legacyApiKey` is not
`disabled`, the deprecated `GREENKUBE_API_KEY`); a worker with neither
answers `503`. The worker ServiceMonitor sends the scrape token. The CLI enables its listener only when
`GREENKUBE_WORKER_METRICS_PORT` is set; the chart sets it on the worker Deployment.

### Controller metrics endpoint

The controller (`greenkube start`: collection, compression, recommendation
scans, verification, GitOps sync observation) serves its own process-local registry at
`/prometheus/metrics` (also at Prometheus' default `/metrics` path) on
`CONTROLLER_METRICS_PORT` (Helm
`controller.metricsPort`, default `9102`; `0` disables it). The chart renders a
`<release>-controller-metrics` ClusterIP Service and, with
`monitoring.serviceMonitor.enabled=true`, a dedicated controller
ServiceMonitor. Verification outcomes recorded by the controller are now
scrapeable (they used to live in a process without an endpoint).

The chart keeps the admin API key out of the controller (least privilege) and
gives it the `metrics:read` scrape token `GREENKUBE_METRICS_TOKEN` instead, which
the controller ServiceMonitor sends as a bearer token; `/loops` needs it too. The
endpoint is reachable on the pod network only and restricted to Prometheus when
`monitoring.networkPolicy` is enabled. Without any token (`secrets.existingSecret`
without `GREENKUBE_METRICS_TOKEN`) it serves its bounded, non-sensitive counters
openly, as before. Outside Kubernetes, `greenkube start` also listens on
`0.0.0.0:9102` by default; set `CONTROLLER_METRICS_PORT=0` to turn it off.

| Metric | Type and labels | Description |
|---|---|---|
| `greenkube_collection_source_requests_total` | Counter; `source` (`prometheus`, `opencost`, `electricity_maps`, `wattnet`, `kubernetes`, `other`), `outcome` (`success`, `empty`, `error`, `skipped`) | Requests to measurement sources |
| `greenkube_collection_source_duration_seconds` | Histogram; `source` | Measurement source request duration |
| `greenkube_collection_run_duration_seconds` | Histogram; no labels | Wall-clock duration of one collection run (every due window: collect, assemble, write); per-source chunk durations are `greenkube_collection_window_duration_seconds{source}` |
| `greenkube_metric_rows_written_total` | Counter; `scope` (`pod`, `container`), `dimension` (`cost`, `usage`, `power`, `intensity`), `source` (provenance value, `mixed` or `unset`) | Provenance mix of written metric rows |

---

## 4. Grafana dashboard

`dashboards/greenkube-operations.json` (**GreenKube - Operations**, uid `greenkube-operations`) is a small operations dashboard on the metrics above:

| Row | Panels |
|---|---|
| **Collection** | Collection data age, watermark lag and coverage per source, abandoned and degraded windows |
| **Loops and scans** | Active controller, consecutive failures and time since last success per loop, time since the last successful scan, scans by status, failures by recommendation source |
| **Measurement and ledger** | Measurement loop age, ledger chain and reconciliation status, projection backlog, ledger errors |
| **Savings (ledger v2)** | Net cost and CO₂e accrued by method (attributed − reversed), measured share |
| **Automation** | Automation backlog by queue and state |

It needs the API and controller ServiceMonitors (`monitoring.serviceMonitor.enabled=true`) and no Grafana plugin. The `cluster` variable filters the API families; control-loop families have no `cluster` label.

The previous, larger dashboard is removed together with the series it read ([Upgrading](upgrading.md)).

### Import

> Always use `/api/dashboards/import` (not `/api/dashboards/db`). Only the import endpoint resolves the `${DS_PROMETHEUS}` variable; using `db` leaves all panels with "datasource is not set".

**Via UI:** Dashboards → Import → Upload JSON file → select your Prometheus datasource.

**Via script:**

```python
import json, urllib.request, base64

with open("dashboards/greenkube-operations.json") as f:
    dashboard = json.load(f)

payload = json.dumps({
    "dashboard": dashboard,
    "folderId": 0,
    "overwrite": True,
    "inputs": [{
        "name": "DS_PROMETHEUS",
        "type": "datasource",
        "pluginId": "prometheus",
        "value": "<YOUR_PROMETHEUS_DATASOURCE_UID>",
    }]
}).encode()

req = urllib.request.Request(
    "http://<GRAFANA_HOST>/api/dashboards/import",
    data=payload,
    headers={
        "Content-Type": "application/json",
        "Authorization": "Basic " + base64.b64encode(b"admin:password").decode(),
    }
)
with urllib.request.urlopen(req) as r:
    print(json.load(r).get("importedUrl"))
```

Find your Prometheus datasource UID in Grafana → Connections → Data sources → select your Prometheus instance → copy the UID from the URL.

---

## 5. Troubleshooting

### "No data" in all panels

1. **ServiceMonitor not enabled**: in the Prometheus UI (`/targets`), verify the `greenkube-api` and `greenkube-controller-metrics` targets are `UP`.
2. **Unexpected `cluster` label**: set `config.clusterName` (or `CLUSTER_NAME`) to a stable cluster identifier. If unset, a multi-node cluster without a cluster-name node label uses `default` ([Cluster labels](#2-cluster-labels)).
3. **`DS_PROMETHEUS` not resolved**: open dashboard settings → Variables. `DS_PROMETHEUS` must show your Prometheus datasource name, not the literal string `${DS_PROMETHEUS}`. Re-import using `/api/dashboards/import`.
4. **GreenKube just started**: the collection panels fill after the first collection (5 minutes at most); the savings panels after the first ledger accrual.

### Datasource shows "not set"

The dashboard was imported via `/api/dashboards/db`. Re-import using the Python script above.
