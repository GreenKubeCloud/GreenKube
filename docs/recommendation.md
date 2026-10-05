# Recommendation Lifecycle

> **Note:** The recommendation component is a multi-source optimization engine. The [Optimization engine specification](specs/optimization-engine.md) preserves its design and completed phased plan; this page documents current end-to-end behavior.

This page describes how GreenKube recommendations are generated, stored, shown, and turned into measured impact. It reflects the current code paths in `src/greenkube/core/optimization/`, `src/greenkube/models/metrics.py`, `src/greenkube/api/routers/recommendations.py`, the SQLite/PostgreSQL recommendation repositories, and the frontend recommendations page.

## What GreenKube Analyzes

Recommendations are derived from stored `CombinedMetric` records. These records already combine Kubernetes workload identity, resource requests, observed CPU and memory usage, energy, CO2e, cost, timestamps, grid intensity, and node metadata collected elsewhere in GreenKube.

The API and startup scan use `RECOMMENDATION_LOOKBACK_DAYS` to read the recent metrics window from the combined metrics repository. The default is 7 days. When the recommender receives the analysis window length, projected savings are annualized from the observed window.

The optimization context can also include these optional Kubernetes and cost inputs:

| Input | Purpose |
|---|---|
| Latest node snapshots | Enables node-level recommendations such as overprovisioned or underutilized nodes. |
| HPA targets | Prevents autoscaling recommendations for workloads that already have a HorizontalPodAutoscaler. |
| Orphaned PersistentVolumes | Enables delete-orphaned-PV recommendations. The `PVCollector` lists all PVs and PVCs from the Kubernetes API and reports volumes whose claim is gone or released. Requires read access to `persistentvolumes` and `persistentvolumeclaims` at cluster scope; the Helm chart's `ClusterRole` includes these resources by default. When OpenCost is reachable, real per-volume storage costs are fetched via `OpenCostCollector.collect_pv_costs()` and used for the projected savings; otherwise the capacity-based estimate applies. |
| Orphaned LoadBalancers | Enables delete-orphaned-LoadBalancer recommendations. The `LoadBalancerCollector` lists all Services and Endpoints from the Kubernetes API and reports Services of type `LoadBalancer` that have no ready backing endpoints. Requires read access to `services` and `endpoints` at cluster scope; the Helm chart's `ClusterRole` includes these resources by default. When OpenCost is reachable, real per-service LoadBalancer costs are fetched via `OpenCostCollector.collect_lb_costs()` (allocations aggregated by `service`, using the `loadBalancerCosts` field) and used for the projected savings; otherwise the flat `LOAD_BALANCER_COST_PER_MONTH` estimate applies. |

VPA and Karpenter are separate optional recommendation sources. VPA reads recommendation-mode objects when its CRD is available; Karpenter reads NodePools/NodeClaims when compatible CRDs are available. Both connectors are enabled by default and skip cleanly when their APIs are absent.

During API and startup scans, metrics from Kubernetes namespaces that no longer exist are filtered out when the Kubernetes API is reachable. This lets reconciliation mark old active recommendations from deleted namespaces as stale instead of regenerating them forever.

## Lifecycle States

A freshly generated in-memory `Recommendation` has no lifecycle state until it is converted into a `RecommendationRecord`. Every transition writes a row to `recommendation_events` (actor, payload, timestamp) exposed by `GET /api/v1/recommendations/{id}/events`.

| State | Meaning | How it is reached |
|---|---|---|
| `active` | The recommendation is currently valid and visible in active lists, top recommendations, Prometheus active gauges, Grafana cards, and the frontend Active tab. | Created by `RecommendationRecord.from_recommendation()` and inserted or refreshed by repository upsert. Ignored recommendations can also be restored to active. |
| `stale` | A previously active recommendation no longer appears in the latest generated set. It is kept in history but no longer shown as active. | `reconcile_active_recommendations()` after a refresh or startup scan. |
| `expired` | The recommendation passed `expires_at` (`generated_at + RECOMMENDATION_TTL_DAYS`) without being applied. It is hidden from active lists but retained for history. | `RecommendationLifecycle.expire()` during every refresh and every lifecycle job. |
| `ignored` | A user intentionally hid the recommendation with an optional reason. Ignored records are preserved for review and can be restored. | `PATCH /api/v1/recommendations/{id}/ignore`. |
| `pr_open` | The PR bot opened a pull request that applies the recommendation. | `POST /api/v1/recommendations/{id}/apply-pr`. |
| `applied` | The change landed in the cluster. Applying freezes a verification baseline. | `PATCH .../apply` (manual), apply detection (Kubernetes API), or a merged PR. |
| `verifying` | The observation window after apply is in progress (or was extended once because of insufficient samples). | `RecommendationVerifier` lifecycle job. |
| `verified` | Cost, carbon and health gates passed; measured savings replace the prorated estimate in the ledger. | `RecommendationVerifier` lifecycle job. |
| `rollback_review` | A health gate failed (restarts, OOM, usage above the proposed request × headroom): the change needs review and prior savings rows are superseded. | `RecommendationVerifier` lifecycle job. |
| `reverted` | The change was rolled back. | Reserved for the rollback follow-up. |
| `failed` | The PR was closed without merge or the savings gate failed (record stays visible with `savings_realized=false`). | PR tracking / verification. |

**Apply succeeded vs recommendation succeeded.** `application_method` records how the change landed (`manual`, `detected`, `pr_merge`); `verified_at` and `verification_status` record whether the outcome matched the projection. Only verified savings count as measured.

**Verification gates.** `VERIFICATION_WINDOW_HOURS` (default 72) and `VERIFICATION_MIN_SAMPLES` control the observation window; the cost/carbon gate requires the measured reduction to reach `VERIFICATION_MIN_SAVINGS_RATIO` (default 0.5) of the projection; the health gate requires the p95 usage to stay below the proposed request × `VERIFICATION_USAGE_HEADROOM` (default 1.1), no restart delta above `VERIFICATION_MAX_RESTART_DELTA`, and no OOM kills. Insufficient samples extend the window once, then produce `inconclusive`.

## Generation Flow

1. Metrics are collected and written to storage by the normal GreenKube collection pipeline.
2. `OptimizationEngine.refresh()` builds an `OptimizationContext`: a recent metrics window plus, when available, node snapshots, HPA targets, orphaned PersistentVolumes and orphaned LoadBalancers. This loading logic is shared by API recommendation requests and the startup scan.
3. Enabled **sources** run over the context. The native source runs the analyzers (grouping metrics by stable target: Kubernetes owner kind/name when present, inferred Deployment from ReplicaSet-style pod names when possible, otherwise the pod name). The optional VPA source reads recommendation-mode VPAs.
4. Generated recommendations are **normalized, arbitrated and deduplicated**: when several sources own the same target, capability and type, the highest-priority source wins (VPA replaces native rightsizing for the same workload) and provenance is recorded in `sources`.
5. Each recommendation is **enriched** with a review-grade evidence block, a risk/confidence/effort assessment, a machine-readable `patch` plan and an expiry date, then scored with the multi-criteria ranking profile.
6. Recommended CPU and memory requests are floored to configured minimums.
7. API and startup paths convert recommendations to `RecommendationRecord` objects, upsert active records, and reconcile missing active records as stale.

The API and frontend are the only consumers of the engine; there is no CLI reporting command.

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
| `GET` | `/recommendations?namespace=&refresh=false` | Returns active records. If none exist, the first read generates and persists them; `refresh=true` schedules a refresh after the response. |
| `GET` | `/recommendations/active?namespace=&refresh=false&source=&risk_level=&capability=` | Returns persisted active records. With `refresh=true`, runs generation and reconciliation first. Optional filters by source, risk level and capability. |
| `GET` | `/recommendations/top?limit=5&metric=co2&profile=&namespace=&refresh=false` | Returns ranked active recommendations with positive projected savings. `metric` is `co2` or `cost`; `limit` is 1 to 50. With `profile` (`balanced`, `carbon_first`, `cost_first`, `quick_wins`, `low_risk`), the multi-criteria `ranking_score` drives the order. |
| `GET` | `/recommendations/{id}` | Returns a single record including its full evidence block, risk/confidence assessment, patch plan and expiry. |
| `GET` | `/recommendations/ignored?namespace=` | Returns ignored records. |
| `GET` | `/recommendations/applied?namespace=` | Returns applied, verifying, verified and rollback-review records ordered by most recent application. |
| `GET` | `/recommendations/history?start=&end=&type=&namespace=` | Returns records in a creation-time range, any status. |
| `GET` | `/recommendations/{id}/events` | Returns the full lifecycle audit trail (actor, payload, timestamp) for a recommendation. |
| `GET` | `/recommendations/savings?namespace=&last=` | Returns realized savings split into measured (verified) and prorated (pending verification) totals. Without `last`, it uses applied recommendation records. With `last`, it prefers the savings ledger for exact window totals and falls back to records if the ledger is unavailable. |
| `PATCH` | `/recommendations/{id}/apply` | Marks a recommendation as `applied`, stores actual CPU or memory values when supplied, freezes the verification baseline, and records realized savings. |
| `PATCH` | `/recommendations/{id}/ignore` | Marks a recommendation as `ignored` and stores the reason. |
| `DELETE` | `/recommendations/{id}/ignore` | Restores an ignored recommendation to `active`. |
| `POST` | `/recommendations/{id}/apply-pr` | Resolves the workload's Git source from its annotations and patches the manifest. `dry_run=true` returns a diff immediately; a normal request queues a durable operation and returns `202 Accepted`. |
| `GET` | `/recommendations/{id}/pull-requests` | Lists the pull-request attempts and their status for a recommendation. |
| `GET` | `/automation/status` | Reports whether the PR bot is configured (provider, token, default branch). |

### GitOps PR bot

The bot is documented in detail in the [automation plan](automation-plan.md). In short:

1. It reads `greenkube.cloud/git-repo`, `greenkube.cloud/git-path` and `greenkube.cloud/git-branch` annotations from the workload (Deployment, StatefulSet or DaemonSet).
2. It fetches the manifest through the configured Git provider (`github`, `gitlab` or `gitea`), applies the rightsizing patch with a round-trip YAML editor (comments and formatting are preserved) and renders the PR body from the stored evidence block.
3. It creates a `greenkube/reco-<id>-<target>` branch, commits the change and opens a pull request. The recommendation moves to `pr_open` and an audit event is written.
4. After the PR is merged (and ArgoCD or the user syncs it), the **apply detector** observes the lowered request on the live workload, freezes the verification baseline and moves the recommendation to `applied` with `application_method=pr_merge`. The **verifier** later confirms the outcome and switches the ledger from prorated to measured savings.

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

## Lifecycle Mutations

Use the API to apply, ignore, or restore recommendations, and the dashboard to browse them: `GET /api/v1/recommendations/active`, `PATCH /api/v1/recommendations/{id}/apply`, `PATCH /api/v1/recommendations/{id}/ignore` and `DELETE /api/v1/recommendations/{id}/ignore`. The CLI no longer exposes report or recommendation commands.

## Frontend Usage

The web dashboard fetches active recommendations and realized savings in the background so the main dashboard can render even if recommendation refresh takes time.

The `/recommendations` page currently provides:

- Active, Pull requests, Ignored, and Realized Savings tabs.
- Type filtering for active and ignored records.
- Potential annual CO2e and cost savings summaries for active records.
- Source badges (GreenKube, VPA, Karpenter), risk badges and confidence indicators.
- An expandable evidence panel per active recommendation (window, coverage, utilization distribution, proposed change, rollback conditions, expiry).
- A ranking profile selector (projected savings, balanced, carbon first, cost first, quick wins, low risk).
- A **Create PR** action on eligible rightsizing cards: it previews the Git diff (dry run), lets the user adjust the base branch, and queues pull-request creation through the configured Git provider. The UI tracks the durable operation until the provider result is available.
- Ignore with a required reason from the Active tab.
- Restore from the Ignored tab.
- Applied recommendation details, verification state (`applied`, `verifying`, `verified`, `rollback_review`), measured vs projected savings and an expandable lifecycle event trail.
- Realized savings summary split into **measured (verified)** and **projected (prorated)** totals.

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

After verification, ledger rows switch to `measurement_method='measured'` and use the verifier's measured savings; pending records keep writing prorated rows. When a recommendation enters `rollback_review`, attribution stops and all its prior rows are flagged `superseded=true` (never deleted). The API and the dashboard expose measured and prorated totals separately, and the `greenkube_savings_measured_vs_projected_ratio` gauge tracks the measured share.

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
| `RECOMMENDATION_VPA_ENABLED` | `config.recommendations.vpaEnabled` | `true` (CRD auto-detected) |
| `RECOMMENDATION_KARPENTER_ENABLED` | `config.recommendations.karpenterEnabled` | `true` (CRD auto-detected) |
| `RECOMMENDATION_SOURCE_PRIORITY` | `config.recommendations.sourcePriority` | `vpa,karpenter,greenkube` |
| `RECOMMENDATION_TTL_DAYS` | `config.recommendations.ttlDays` | `14` |
| `RECOMMENDATION_RANKING_PROFILE` | `config.recommendations.rankingProfile` | `balanced` |
| `RECOMMENDATION_RANKING_WEIGHTS` | `config.recommendations.rankingWeights` | `""` (profile defaults) |
| `RECOMMENDATION_MIN_SAMPLES` | `config.recommendations.minSamples` | `36` |
| `VERIFICATION_WINDOW_HOURS` | `config.recommendations.verification.windowHours` | `72` |
| `VERIFICATION_MIN_SAMPLES` | `config.recommendations.verification.minSamples` | `36` |
| `VERIFICATION_MIN_SAVINGS_RATIO` | `config.recommendations.verification.minSavingsRatio` | `0.5` |
| `VERIFICATION_USAGE_HEADROOM` | `config.recommendations.verification.usageHeadroom` | `1.1` |
| `VERIFICATION_MAX_RESTART_DELTA` | `config.recommendations.verification.maxRestartDelta` | `0` |
| `VERIFICATION_MIN_READINESS` | `config.recommendations.verification.minReadiness` | `0.99` |
| `VERIFICATION_MAX_THROTTLE_RATIO` | `config.recommendations.verification.maxThrottleRatio` | `0.05` |
| `RECOMMENDATION_LIFECYCLE_INTERVAL` | `config.recommendations.verification.lifecycleInterval` | `5m` |
| `GIT_PROVIDER` | `config.recommendations.git.provider` | `github` |
| `GIT_API_BASE_URL` | `config.recommendations.git.apiBaseUrl` | `""` |
| `GIT_DEFAULT_BRANCH` | `config.recommendations.git.defaultBranch` | `main` |
| `GIT_TOKEN` (secret) | `secrets.gitToken` / `secrets.existingSecret` | unset (PR bot disabled) |

`RECOMMENDATION_APPLY_TOLERANCE` drives the automatic **apply detection**: the lifecycle job compares live workload requests against the stored current values and marks a recommendation applied when the request dropped by more than the tolerance, or matches the recommended value within it.

The lifecycle job (`RECOMMENDATION_LIFECYCLE_INTERVAL`, default every 5 minutes in the collector/scheduler container) runs apply detection, outcome verification and TTL expiry.

## Source Map

| Area | Main files |
|---|---|
| DTOs and lifecycle fields | `src/greenkube/models/metrics.py`, `src/greenkube/models/evidence.py` |
| Optimization engine | `src/greenkube/core/optimization/engine.py`, `context_builder.py`, `context.py` |
| Analyzers | `src/greenkube/core/optimization/analyzers/` |
| Sources (native, VPA, Karpenter) | `src/greenkube/core/optimization/providers/`, `registry.py` |
| Dedup / arbitration / provenance | `src/greenkube/core/optimization/dedup.py` |
| Evidence, risk, ranking | `src/greenkube/core/optimization/evidence.py`, `risks.py`, `scoring.py`, `enrich.py` |
| Lifecycle, apply detection, verification | `src/greenkube/core/optimization/lifecycle.py`, `applied_detector.py`, `verifier.py` |
| VPA discovery | `src/greenkube/collectors/vpa_collector.py`, `src/greenkube/utils/k8s_quantities.py` |
| Karpenter discovery | `src/greenkube/collectors/karpenter_collector.py` |
| Orphaned PV discovery | `src/greenkube/collectors/pv_collector.py` |
| Orphaned LoadBalancer discovery | `src/greenkube/collectors/lb_collector.py` |
| Ranking (API DTOs) | `src/greenkube/core/recommendation_ranking.py` |
| Realized savings estimation | `src/greenkube/core/recommendation_realization.py` |
| Savings ledger attribution | `src/greenkube/core/savings_attributor.py` |
| PR bot | `src/greenkube/automation/` |
| Recommendation API routes | `src/greenkube/api/routers/recommendations.py` |
| Automation API routes | `src/greenkube/api/routers/automation.py` |
| Prometheus gauges | `src/greenkube/api/metrics_endpoint.py` |
| Startup scan & lifecycle job | `src/greenkube/api/startup.py`, `src/greenkube/cli/start.py` |
| Migrations 0012–0014 | `src/greenkube/core/migrations/scripts/{sqlite,postgres}/` |
| Storage adapters | `src/greenkube/storage/recommendation_mapper.py`, `src/greenkube/storage/sqlite/recommendation_repository.py`, `src/greenkube/storage/postgres/recommendation_repository.py` |
| Migrations | `src/greenkube/core/migrations/scripts/{sqlite,postgres}/0010_*.sql`, `0011_*.sql` |
| CLI | `src/greenkube/cli/recommend.py` |
| Frontend | `frontend/src/routes/recommendations/+page.svelte`, `frontend/src/lib/api.js` |
| End-to-end tests | `tests/integration/test_recommendation_lifecycle_e2e.py` |
