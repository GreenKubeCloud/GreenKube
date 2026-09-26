# Recommendation Lifecycle

> **Note:** The recommendation component is being overhauled into a multi-source
> optimization engine. The target architecture, evidence model, ranking and
> verification lifecycle are specified in
> [Optimization engine specification](specs/optimization-engine.md). This page
> documents the behavior currently implemented.

This page describes how GreenKube recommendations are generated, stored, shown, and turned into measured impact. It reflects the current code paths in `src/greenkube/core/optimization/`, `src/greenkube/models/metrics.py`, `src/greenkube/api/routers/recommendations.py`, the SQLite/PostgreSQL recommendation repositories, and the frontend recommendations page.

## What GreenKube Analyzes

Recommendations are derived from stored `CombinedMetric` records. These records already combine Kubernetes workload identity, resource requests, observed CPU and memory usage, energy, CO2e, cost, timestamps, grid intensity, and node metadata collected elsewhere in GreenKube.

The API and startup scan use `RECOMMENDATION_LOOKBACK_DAYS` to read the recent metrics window from the combined metrics repository. The default is 7 days. When the recommender receives the analysis window length, projected savings are annualized from the observed window.

The recommender can also use four optional inputs:

| Input | Purpose |
|---|---|
| Latest node snapshots | Enables node-level recommendations such as overprovisioned or underutilized nodes. |
| HPA targets | Prevents autoscaling recommendations for workloads that already have a HorizontalPodAutoscaler. |
| Orphaned PersistentVolumes | Enables delete-orphaned-PV recommendations. The `PVCollector` lists all PVs and PVCs from the Kubernetes API and reports volumes whose claim is gone or released. Requires read access to `persistentvolumes` and `persistentvolumeclaims` at cluster scope; the Helm chart's `ClusterRole` includes these resources by default. When OpenCost is reachable, real per-volume storage costs are fetched via `OpenCostCollector.collect_pv_costs()` and used for the projected savings; otherwise the capacity-based estimate applies. |
| Orphaned LoadBalancers | Enables delete-orphaned-LoadBalancer recommendations. The `LoadBalancerCollector` lists all Services and Endpoints from the Kubernetes API and reports Services of type `LoadBalancer` that have no ready backing endpoints. Requires read access to `services` and `endpoints` at cluster scope; the Helm chart's `ClusterRole` includes these resources by default. When OpenCost is reachable, real per-service LoadBalancer costs are fetched via `OpenCostCollector.collect_lb_costs()` (allocations aggregated by `service`, using the `loadBalancerCosts` field) and used for the projected savings; otherwise the flat `LOAD_BALANCER_COST_PER_MONTH` estimate applies. |

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
2. `OptimizationEngine.refresh()` builds an `OptimizationContext`: a recent metrics window plus, when available, node snapshots, HPA targets, orphaned PersistentVolumes and orphaned LoadBalancers. This loading logic is shared by the API, the startup scan and the CLI.
3. Enabled **sources** run over the context. The native source runs the analyzers (grouping metrics by stable target: Kubernetes owner kind/name when present, inferred Deployment from ReplicaSet-style pod names when possible, otherwise the pod name). The optional VPA source reads recommendation-mode VPAs.
4. Generated recommendations are **normalized, arbitrated and deduplicated**: when several sources own the same target, capability and type, the highest-priority source wins (VPA replaces native rightsizing for the same workload) and provenance is recorded in `sources`.
5. Each recommendation is **enriched** with a review-grade evidence block, a risk/confidence/effort assessment, a machine-readable `patch` plan and an expiry date, then scored with the multi-criteria ranking profile.
6. Recommended CPU and memory requests are floored to configured minimums.
7. API and startup paths convert recommendations to `RecommendationRecord` objects, upsert active records, and reconcile missing active records as stale.

The CLI `greenkube recommend` uses the same engine, but it is a reporting command: it prints recommendations and can fail a CI/CD gate, but it does not persist lifecycle records or update recommendation statuses.

## Sources, Evidence And Ranking

**Sources.** Each recommendation carries `source` (`greenkube`, `vpa`, `karpenter`), an optional `source_ref` (e.g. `namespace/vpa-name`), a `sources` provenance list and a `capability` (used for arbitration). Enable the VPA connector with `RECOMMENDATION_VPA_ENABLED=true`; source precedence is controlled by `RECOMMENDATION_SOURCE_PRIORITY` (default `vpa,karpenter,greenkube`).

**Evidence.** Every recommendation embeds a `RecommendationEvidence` block: observation window and coverage, current vs proposed requests, CPU/memory distribution (avg, p50, p90, p95, p99, max), restart count, expected savings and method, confidence with factors, risk level with factors, rollback conditions, a `patch` action plan and `expires_at`. Retrieve the full block from `GET /api/v1/recommendations/{id}`; it is also rendered in the dashboard's evidence panel.

**Risk and confidence.** `risk_level` (low/medium/high) is derived from the observed headroom, restart/OOM history and blast radius; `confidence` (0–1) aggregates sample count, coverage and source authority. Both are explainable via `risk_factors` and the confidence factors inside the evidence block.

**Ranking.** `ranking_score` combines carbon impact, cost impact, confidence, risk, effort, actionability and source authority. Profiles (`balanced`, `carbon_first`, `cost_first`, `quick_wins`, `low_risk`) can be selected per request; `ranking_factors` exposes the contribution of each criterion.


## Recommendation Types

GreenKube currently has eleven recommendation types.

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
| `ORPHANED_PERSISTENT_VOLUME` | cluster | A PersistentVolume is in `Released` phase (its PVC was deleted but the volume was not reclaimed) or its `claimRef` references a PVC that no longer exists. The PV name is stored in the `pod_name` field because PVs are cluster-scoped. Deleting the volume releases the provisioned storage. Projected cost savings prefer the real storage cost reported by OpenCost for the volume (annualized from the observation window); when OpenCost has no cost data (e.g. on-premises or local storage), the estimate falls back to the provisioned capacity and `STORAGE_COST_PER_GIB_MONTH` (default `$0.10`/GiB-month). CO2e savings are not projected because energy estimation currently only covers CPU usage, not disk usage. |
| `ORPHANED_LOAD_BALANCER` | cluster | A Service of type `LoadBalancer` has no ready backing endpoints, so its selector matches no pods and the provisioned cloud LoadBalancer routes traffic to nothing while continuing to bill hourly. The Service name is stored in the `pod_name` field and its namespace in the `namespace` field. Deleting the Service removes the cloud LoadBalancer. Projected cost savings prefer the real LoadBalancer cost reported by OpenCost for the Service (`OpenCostCollector.collect_lb_costs()` aggregates allocations by `service` and annualizes the window cost); when OpenCost has no cost data, the estimate falls back to `LOAD_BALANCER_COST_PER_MONTH` (default `$18.00`/month). CO2e savings are not projected because energy estimation currently only covers CPU usage. |

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
| `GET` | `/recommendations?namespace=` | Runs the optimization engine, persists active records, reconciles stale records, and returns in-memory recommendations. |
| `GET` | `/recommendations/active?namespace=&refresh=false&source=&risk_level=&capability=` | Returns persisted active records. With `refresh=true`, runs generation and reconciliation first. Optional filters by source, risk level and capability. |
| `GET` | `/recommendations/top?limit=5&metric=co2&profile=&namespace=&refresh=false` | Returns ranked active recommendations with positive projected savings. `metric` is `co2` or `cost`; `limit` is 1 to 50. With `profile` (`balanced`, `carbon_first`, `cost_first`, `quick_wins`, `low_risk`), the multi-criteria `ranking_score` drives the order. |
| `GET` | `/recommendations/{id}` | Returns a single record including its full evidence block, risk/confidence assessment, patch plan and expiry. |
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
- Source badges (GreenKube, VPA, Karpenter), risk badges and confidence indicators.
- An expandable evidence panel per active recommendation (window, coverage, utilization distribution, proposed change, rollback conditions, expiry).
- A ranking profile selector (projected savings, balanced, carbon first, cost first, quick wins, low risk).
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
| `STORAGE_COST_PER_GIB_MONTH` | `config.recommendations.storageCostPerGibMonth` | `0.1` |
| `LOAD_BALANCER_COST_PER_MONTH` | `config.recommendations.loadBalancerCostPerMonth` | `18.0` |
| `RECOMMENDATION_VPA_ENABLED` | `config.recommendations.vpaEnabled` | `false` |
| `RECOMMENDATION_KARPENTER_ENABLED` | `config.recommendations.karpenterEnabled` | `false` |
| `RECOMMENDATION_SOURCE_PRIORITY` | `config.recommendations.sourcePriority` | `vpa,karpenter,greenkube` |
| `RECOMMENDATION_TTL_DAYS` | `config.recommendations.ttlDays` | `14` |
| `RECOMMENDATION_RANKING_PROFILE` | `config.recommendations.rankingProfile` | `balanced` |
| `RECOMMENDATION_RANKING_WEIGHTS` | `config.recommendations.rankingWeights` | `""` (profile defaults) |
| `RECOMMENDATION_MIN_SAMPLES` | `config.recommendations.minSamples` | `36` |

`RECOMMENDATION_APPLY_TOLERANCE` is present in configuration and Helm values, but the current apply endpoint marks a recommendation as applied only when the API is called. There is no automatic apply-detection path using this tolerance in the current code.

## Source Map

| Area | Main files |
|---|---|
| DTOs and lifecycle fields | `src/greenkube/models/metrics.py`, `src/greenkube/models/evidence.py` |
| Optimization engine | `src/greenkube/core/optimization/engine.py`, `context_builder.py`, `context.py` |
| Analyzers | `src/greenkube/core/optimization/analyzers/` |
| Sources (native, VPA, Karpenter) | `src/greenkube/core/optimization/providers/`, `registry.py` |
| Dedup / arbitration / provenance | `src/greenkube/core/optimization/dedup.py` |
| Evidence, risk, ranking | `src/greenkube/core/optimization/evidence.py`, `risks.py`, `scoring.py`, `enrich.py` |
| VPA discovery | `src/greenkube/collectors/vpa_collector.py`, `src/greenkube/utils/k8s_quantities.py` |
| Orphaned PV discovery | `src/greenkube/collectors/pv_collector.py` |
| Orphaned LoadBalancer discovery | `src/greenkube/collectors/lb_collector.py` |
| Ranking (API DTOs) | `src/greenkube/core/recommendation_ranking.py` |
| Realized savings estimation | `src/greenkube/core/recommendation_realization.py` |
| Savings ledger attribution | `src/greenkube/core/savings_attributor.py` |
| API routes | `src/greenkube/api/routers/recommendations.py` |
| Prometheus gauges | `src/greenkube/api/metrics_endpoint.py` |
| Startup scan | `src/greenkube/api/startup.py` |
| Storage adapters | `src/greenkube/storage/recommendation_mapper.py`, `src/greenkube/storage/sqlite/recommendation_repository.py`, `src/greenkube/storage/postgres/recommendation_repository.py` |
| Migrations | `src/greenkube/core/migrations/scripts/{sqlite,postgres}/0010_*.sql`, `0011_*.sql` |
| CLI | `src/greenkube/cli/recommend.py` |
| Frontend | `frontend/src/routes/recommendations/+page.svelte`, `frontend/src/lib/api.js` |
| End-to-end tests | `tests/integration/test_recommendation_lifecycle_e2e.py` |
