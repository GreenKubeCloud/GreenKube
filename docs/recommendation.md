# Recommendation Lifecycle

This page describes how GreenKube recommendations are generated, stored, shown, and turned into measured impact. It reflects the current code paths in `src/greenkube/core/recommender.py`, `src/greenkube/models/metrics.py`, `src/greenkube/api/routers/recommendations.py`, the SQLite/PostgreSQL recommendation repositories, and the frontend recommendations page.

## What GreenKube Analyzes

Recommendations are derived from stored `CombinedMetric` records. These records already combine Kubernetes workload identity, resource requests, observed CPU and memory usage, energy, CO2e, cost, timestamps, grid intensity, and node metadata collected elsewhere in GreenKube.

The API and startup scan use `RECOMMENDATION_LOOKBACK_DAYS` to read the recent metrics window from the combined metrics repository. The default is 7 days. When the recommender receives the analysis window length, projected savings are annualized from the observed window.

The recommender can also use two optional inputs:

| Input | Purpose |
|---|---|
| Latest node snapshots | Enables node-level recommendations such as overprovisioned or underutilized nodes. |
| HPA targets | Prevents autoscaling recommendations for workloads that already have a HorizontalPodAutoscaler. |

During API and startup scans, metrics from Kubernetes namespaces that no longer exist are filtered out when the Kubernetes API is reachable. This lets reconciliation mark old active recommendations from deleted namespaces as stale instead of regenerating them forever.

## Lifecycle States

There are four implemented persisted states. A freshly generated in-memory `Recommendation` has no lifecycle state until it is converted into a `RecommendationRecord`.

| State | Meaning | How it is reached |
|---|---|---|
| `active` | The recommendation is currently valid and visible in active lists, top recommendations, Prometheus active gauges, Grafana cards, and the frontend Active tab. | Created by `RecommendationRecord.from_recommendation()` and inserted or refreshed by repository upsert. Ignored recommendations can also be restored to active. |
| `applied` | A user or automation marked the recommendation as implemented. Applied records are excluded from active recommendations and included in realized savings. | `PATCH /api/v1/recommendations/{id}/apply`. |
| `ignored` | A user intentionally hid the recommendation with an optional reason. Ignored records are preserved for review and can be restored. | `PATCH /api/v1/recommendations/{id}/ignore`. |
| `stale` | A previously active recommendation no longer appears in the latest generated set. It is kept in history but no longer shown as active. | `reconcile_active_recommendations()` after a refresh or startup scan. |

The current code does not implement `open`, `in_progress`, `resolved`, `dismissed`, or `snoozed` states.

## Generation Flow

1. Metrics are collected and written to storage by the normal GreenKube collection pipeline.
2. The recommendation scan reads a recent metrics window and, when available, node snapshots and HPA targets.
3. `Recommender.generate_recommendations()` groups metrics by stable target: Kubernetes owner kind/name when present, inferred Deployment from ReplicaSet-style pod names when possible, otherwise the pod name.
4. The recommender runs all recommendation analyzers and deduplicates by scope, namespace, target, type, and node.
5. Recommended CPU and memory requests are floored to configured minimums before being returned.
6. API and startup paths convert recommendations to `RecommendationRecord` objects, upsert active records, and reconcile missing active records as stale.

The CLI `greenkube recommend` uses the same `Recommender` engine, but it is a reporting command: it prints recommendations and can fail a CI/CD gate, but it does not persist lifecycle records or update recommendation statuses.

## Recommendation Types

GreenKube currently has nine recommendation types.

| Type | Scope | Current trigger |
|---|---|---|
| `ZOMBIE_POD` | pod or workload | Target has cost above `ZOMBIE_COST_THRESHOLD` and energy below `ZOMBIE_ENERGY_THRESHOLD`. Projected cost and CO2e savings are annualized from the observed window. |
| `RIGHTSIZING_CPU` | pod or workload | Average CPU usage divided by latest CPU request is below `RIGHTSIZING_CPU_THRESHOLD`. The target request is based on P95 usage, observed max, average usage, and `RIGHTSIZING_HEADROOM`, then floored by `RECOMMENDATION_MIN_CPU_MILLICORES`. Savings are proportional to the request reduction. |
| `RIGHTSIZING_MEMORY` | pod or workload | Average memory usage divided by latest memory request is below `RIGHTSIZING_MEMORY_THRESHOLD`. The target request uses the same balanced sizing formula and is floored by `RECOMMENDATION_MIN_MEMORY_BYTES`. Savings are proportional to the request reduction. |
| `AUTOSCALING_CANDIDATE` | pod or workload | CPU usage has enough samples, coefficient of variation is above `AUTOSCALING_CV_THRESHOLD`, max/mean spike ratio is above `AUTOSCALING_SPIKE_RATIO`, and no matching HPA was found for a non-pod owner target. |
| `OFF_PEAK_SCALING` | pod or workload | Timestamped CPU usage shows at least `OFF_PEAK_MIN_IDLE_HOURS` consecutive hours below `OFF_PEAK_IDLE_THRESHOLD` of the daily peak. The recommendation includes a suggested UTC scale-to-zero window. |
| `IDLE_NAMESPACE` | namespace | Namespace total energy is below `IDLE_NAMESPACE_ENERGY_THRESHOLD` while cost is positive. Common system namespaces are excluded unless `RECOMMEND_SYSTEM_NAMESPACES` is enabled. |
| `CARBON_AWARE_SCHEDULING` | pod or workload | Target average grid intensity is more than `CARBON_AWARE_THRESHOLD` times the average for its electricity zone. Projected CO2e savings are estimated from the high-carbon share. |
| `OVERPROVISIONED_NODE` | node | Node average CPU utilization, and memory utilization when capacity is available, are below `NODE_UTILIZATION_THRESHOLD`. |
| `UNDERUTILIZED_NODE` | node | Node has fewer than three pods and average CPU utilization below 15%. |

Not every recommendation type has projected savings today. The top recommendations API and Grafana actionable cards only rank active recommendations with a positive projected value for the selected metric.

## Persistence And Reconciliation

Recommendations are stored in the `recommendation_history` table. SQLite and PostgreSQL implement the same repository contract.

The active identity used by the repositories is:

```text
scope + namespace + pod_name + target_node + type
```

This identity allows pod, workload, namespace, and node recommendations to coexist without collapsing unrelated targets. Active records with the same identity are refreshed in place with the latest description, reason, priority, projected savings, current requests, recommended requests, schedule, and node target.

Ignored records are left untouched by normal upsert so a user decision is not overwritten by the next scan. If a generated recommendation matches a previously applied record, the applied record can be refreshed so its realized savings reflect the current observed state.

After each API or startup refresh, reconciliation compares the latest generated identities with currently active records. Any active record missing from the generated set becomes `stale`.

## API Usage

All recommendation API paths are under `/api/v1`.

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/recommendations?namespace=` | Runs the recommender, persists active records, reconciles stale records, and returns in-memory recommendations. |
| `GET` | `/recommendations/active?namespace=&refresh=false` | Returns persisted active records. With `refresh=true`, runs generation and reconciliation first. |
| `GET` | `/recommendations/top?limit=5&metric=co2&namespace=&refresh=false` | Returns ranked active recommendations with positive projected savings. `metric` is `co2` or `cost`; `limit` is 1 to 50. |
| `GET` | `/recommendations/ignored?namespace=` | Returns ignored records. |
| `GET` | `/recommendations/applied?namespace=` | Returns applied records ordered by most recent application. |
| `GET` | `/recommendations/history?start=&end=&type=&namespace=` | Returns records in a creation-time range, any status. |
| `GET` | `/recommendations/savings?namespace=&last=` | Returns realized savings. Without `last`, it uses applied recommendation records. With `last`, it prefers the savings ledger for exact window totals and falls back to records if the ledger is unavailable. |
| `PATCH` | `/recommendations/{id}/apply` | Marks a recommendation as `applied`, stores actual CPU or memory values when supplied, and records realized savings. |
| `PATCH` | `/recommendations/{id}/ignore` | Marks a recommendation as `ignored` and stores the reason. |
| `DELETE` | `/recommendations/{id}/ignore` | Restores an ignored recommendation to `active`. |

Example lifecycle calls:

```bash
# Refresh active records before reading them
curl "http://localhost:8000/api/v1/recommendations/active?refresh=true"

# Apply a CPU rightsizing recommendation with the value actually deployed
curl -X PATCH "http://localhost:8000/api/v1/recommendations/42/apply" \
  -H "Content-Type: application/json" \
  -d '{"actual_cpu_request_millicores": 300}'

# Ignore a recommendation with an audit reason
curl -X PATCH "http://localhost:8000/api/v1/recommendations/42/ignore" \
  -H "Content-Type: application/json" \
  -d '{"reason": "Workload is intentionally kept warm for latency."}'

# Restore an ignored recommendation
curl -X DELETE "http://localhost:8000/api/v1/recommendations/42/ignore"
```

## CLI Usage

```bash
greenkube recommend
greenkube recommend --namespace production
greenkube recommend --live
greenkube recommend --fail-on-recommendations
```

By default, the CLI reads stored metrics from the database over `RECOMMENDATION_LOOKBACK_DAYS`. With `--live`, it runs the full processor pipeline before generating recommendations. With `--fail-on-recommendations`, it exits with code 1 when at least one recommendation is found, which is useful for CI/CD policy gates.

The CLI does not expose lifecycle mutations. Use the API to apply, ignore, or restore recommendations.

## Frontend Usage

The web dashboard fetches active recommendations and realized savings in the background so the main dashboard can render even if recommendation refresh takes time.

The `/recommendations` page currently provides:

- Active, Ignored, and Realized Savings tabs.
- Type filtering for active and ignored records.
- Potential annual CO2e and cost savings summaries for active records.
- Ignore with a required reason from the Active tab.
- Restore from the Ignored tab.
- Applied recommendation details and realized savings in the Realized Savings tab.

The frontend API client contains an `applyRecommendation()` helper, but the recommendations page does not currently expose an Apply button. The page tells users to mark active recommendations as applied through the API.

## Prometheus And Grafana

`/prometheus/metrics` refreshes recommendation gauges from the database on scrape. The startup scan also performs a best-effort recommendation refresh after the API starts, so dashboards are not empty after pod restarts when metrics already exist.

Key recommendation metrics:

| Metric | Meaning |
|---|---|
| `greenkube_recommendations_total` | Active recommendation count by cluster, namespace, type, and priority. Also emits a cluster aggregate with `namespace="__all__"`. |
| `greenkube_recommendations_savings_co2e_grams` | Projected annual CO2e savings by recommendation type for active records. |
| `greenkube_recommendations_savings_cost_dollars` | Projected annual cost savings by recommendation type for active records. |
| `greenkube_namespace_recommendation_savings_co2e_grams_total` | Projected annual CO2e savings by target namespace. |
| `greenkube_namespace_recommendation_savings_cost_dollars_total` | Projected annual cost savings by target namespace. |
| `greenkube_top_recommendations` | Ranked active recommendations for Grafana actionable cards. It emits both CO2e and cost values for each rank and selected sort metric. |
| `greenkube_recommendations_implemented_total` | Applied recommendation count by namespace and type. |
| `greenkube_co2e_savings_attributed_grams_total` | Cumulative DB-backed attributed CO2e savings by recommendation type. |
| `greenkube_cost_savings_attributed_dollars_total` | Cumulative DB-backed attributed cost savings by recommendation type. |
| `greenkube_dashboard_savings_co2e_grams_total` | DB-backed CO2e savings for fixed dashboard windows. Prefer this for Grafana time-window panels. |
| `greenkube_dashboard_savings_cost_dollars_total` | DB-backed cost savings for fixed dashboard windows. Prefer this for Grafana time-window panels. |

The Grafana dashboard uses `greenkube_top_recommendations` in the Actionable Recommendations row. Dashboard variables let users choose the ranking metric (`co2` or `cost`) and displayed recommendation count.

## Realized Savings

Applying a recommendation records annual realized savings on the recommendation row. If the apply request includes explicit `carbon_saved_co2e_grams` or `cost_saved`, those values are used.

When explicit savings are omitted:

- CPU and memory rightsizing scale savings by the actual reduction compared with the original recommendation. For example, if the recommendation was 500m to 200m CPU but the user applied 350m, GreenKube records half of the projected savings.
- Other recommendation types fall back to the projected potential savings.

Applied recommendations can later be refreshed when the same issue is observed again:

- For CPU and memory rightsizing, the current observed request updates the actual value and recalculates realized savings.
- For non-resource recommendation types, seeing the same issue again sets realized savings back to zero, because the implementation no longer appears effective.

The `SavingsAttributor` converts annual realized savings into per-period ledger rows using the collection step duration. The ledger writes one row per applied recommendation per attribution cycle when annual CO2e savings are positive; cost savings are included on those rows. Raw rows can be compressed into hourly aggregates, and API/Grafana windowed savings read both raw and hourly data.

## Configuration

Recommendation behavior is configured through environment variables in `src/greenkube/core/config.py` and Helm values under `config.recommendations`.

| Environment variable | Helm value | Default |
|---|---|---|
| `RECOMMENDATION_LOOKBACK_DAYS` | `config.recommendations.lookbackDays` | `7` |
| `RIGHTSIZING_CPU_THRESHOLD` | `config.recommendations.rightsizingCpuThreshold` | `0.3` |
| `RIGHTSIZING_MEMORY_THRESHOLD` | `config.recommendations.rightsizingMemoryThreshold` | `0.3` |
| `RIGHTSIZING_HEADROOM` | `config.recommendations.rightsizingHeadroom` | `1.2` |
| `ZOMBIE_COST_THRESHOLD` | `config.recommendations.zombieCostThreshold` | `0.01` |
| `ZOMBIE_ENERGY_THRESHOLD` | `config.recommendations.zombieEnergyThreshold` | `1000` |
| `AUTOSCALING_CV_THRESHOLD` | `config.recommendations.autoscalingCvThreshold` | `0.7` |
| `AUTOSCALING_SPIKE_RATIO` | `config.recommendations.autoscalingSpikeRatio` | `3.0` |
| `OFF_PEAK_IDLE_THRESHOLD` | `config.recommendations.offPeakIdleThreshold` | `0.05` |
| `OFF_PEAK_MIN_IDLE_HOURS` | `config.recommendations.offPeakMinIdleHours` | `4` |
| `IDLE_NAMESPACE_ENERGY_THRESHOLD` | `config.recommendations.idleNamespaceEnergyThreshold` | `1000` |
| `CARBON_AWARE_THRESHOLD` | `config.recommendations.carbonAwareThreshold` | `1.5` |
| `NODE_UTILIZATION_THRESHOLD` | `config.recommendations.nodeUtilizationThreshold` | `0.2` |
| `RECOMMEND_SYSTEM_NAMESPACES` | `config.recommendations.recommendSystemNamespaces` | `false` |
| `RECOMMENDATION_MIN_CPU_MILLICORES` | `config.recommendations.minCpuMillicores` | `10` |
| `RECOMMENDATION_MIN_MEMORY_BYTES` | `config.recommendations.minMemoryBytes` | `16777216` |
| `RECOMMENDATION_APPLY_TOLERANCE` | `config.recommendations.applyTolerance` | `0.25` |

`RECOMMENDATION_APPLY_TOLERANCE` is present in configuration and Helm values, but the current apply endpoint marks a recommendation as applied only when the API is called. There is no automatic apply-detection path using this tolerance in the current code.

## Source Map

| Area | Main files |
|---|---|
| DTOs and lifecycle fields | `src/greenkube/models/metrics.py` |
| Recommendation generation | `src/greenkube/core/recommender.py` |
| Ranking | `src/greenkube/core/recommendation_ranking.py` |
| Realized savings estimation | `src/greenkube/core/recommendation_realization.py` |
| Savings ledger attribution | `src/greenkube/core/savings_attributor.py` |
| API routes | `src/greenkube/api/routers/recommendations.py` |
| Prometheus gauges | `src/greenkube/api/metrics_endpoint.py` |
| Startup scan | `src/greenkube/api/startup.py` |
| Storage adapters | `src/greenkube/storage/sqlite/recommendation_repository.py`, `src/greenkube/storage/postgres/recommendation_repository.py` |
| CLI | `src/greenkube/cli/recommend.py` |
| Frontend | `frontend/src/routes/recommendations/+page.svelte`, `frontend/src/lib/api.js` |
| End-to-end tests | `tests/integration/test_recommendation_lifecycle_e2e.py` |
