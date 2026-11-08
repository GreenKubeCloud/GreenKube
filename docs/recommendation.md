# Recommendation Lifecycle

> **Note:** Recommendations are the input to the GreenKube loop: aggregate → decide → deliver as a pull request → prove → ledger (see [Concepts](concepts.md)). This page documents recommendation generation, lifecycle, verification and savings. The plan, pull-request, sync and rollback workflow is documented in [GitOps automation](automation.md).

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
| `ignored` | A user intentionally hid the recommendation with an optional reason. Ignored records are preserved for review and can be restored. | `PATCH /api/v1/recommendations/{id}/ignore`, only from `active`, `stale` or `expired` (an open PR or an applied change returns `409`). |
| `pr_open` | The PR bot opened a pull request that applies the recommendation. | `POST /api/v1/recommendations/{id}/apply-pr`. |
| `applied` | The change landed in the cluster. Applying freezes a verification baseline. A measured change whose saving was not realized (`verification_status=failed`) or inconclusive also stays `applied`. | `PATCH .../apply` (manual, only from `active` or `pr_open`; 409 otherwise), apply detection (Kubernetes API, once the rollout completed), or a merged PR. A dismissed rollback review returns here. |
| `verifying` | The observation window after apply is in progress (extended once because of insufficient samples, or started again by an explicit re-measurement). | `RecommendationVerifier` lifecycle job; `POST .../remeasure`. |
| `verified` | The measured saving was realized and no health gate failed; the ledger accrues the measured savings (method `measured`) from verification onward. | `RecommendationVerifier` lifecycle job (never directly from `active`). |
| `regressed` | Reserved for drift detection: a realized saving that no longer holds. Nothing sets it yet. | Only from `verified`. |
| `rollback_review` | A health gate failed (restarts, OOM kills, readiness, CPU throttling, per-pod usage above the applied request × headroom): the change needs review and prior savings rows are superseded. | `RecommendationVerifier` lifecycle job. |
| `reverted` | The change was rolled back and the original per-container baseline was observed again. | The rollback plan reached `synced` and apply detection confirmed the original requests. |
| `failed` | The PR was closed without merge, or the change could not be completed. | PR tracking. |

**Apply succeeded vs recommendation succeeded.** `application_method` records how the change landed (`manual`, `detected`, `pr_merge`, `webhook`, `polling`); `verified_at` and `verification_status` record whether the outcome matched the projection. Only verified savings count as measured. A GitOps action is not eligible for apply detection until its plan reaches `synced`; a merged-but-unsynced plan moves to `sync_failed` after the configured timeout.

**Verification (fixed horizon).** Every applied change gets one measurement, decided once:

- **Windows.** `applied_at` is the rollout completion time (`updatedReplicas == replicas == availableReplicas` and `observedGeneration >= generation`; Deployments report it through their `Progressing` condition, otherwise the detection time is used). The after-window is `[applied_at, applied_at + VERIFICATION_WINDOW_HOURS)`. The baseline window has the same length and ends at the PR merge (or the plan approval for a manual apply), never after `applied_at`. The frozen baseline records the evidence window it copies (`captured_at`, `evidence_window`), its freeze time (`frozen_at`) and `baseline_window_end`.
- **Trusted rows.** When rows carry cost provenance, only `cost_source` in `opencost`, `kubecost` or `pricing_catalog` with a finite, non-negative cost counts (judged by the same provenance helpers as every other consumer); legacy rows need `total_cost > 0` and `is_estimated = false`. A timestamp counts only when every replica row in it is trusted, and rates divide by the time the rows cover, so raw and hourly rows mix correctly. A container-scoped change is measured on its container rows when they carry trusted cost, otherwise on its workload (`measurement_scope=workload_fallback`).
- **No traffic normalization.** The traffic ratio is recorded for information only until load-driver counterfactuals ship.
- **Outcome.** `realized` (status `verified`) when the measured saving exceeds 2% of the baseline rate; `not_realized` (status `applied`, `verification_status=failed`) otherwise; `inconclusive` when trusted samples stay below `VERIFICATION_MIN_SAMPLES` after one extension. The projection is not a gate: `VERIFICATION_MIN_SAVINGS_RATIO` (default 0.5) only adds a note when a realized saving is far below its projection. Request rightsizing never claims a CO2e saving (the carbon model is usage-based); the CO2e delta is kept in the measurement as `not_claimed` and `measured_co2e_saved_grams` is 0.
- **Attribution groups.** CPU and memory records on the same container applied by the same plan (whatever the delay between their detections), or, without a plan, applied within 15 minutes of each other, form one change and are measured once. The cost delta is split by the `cost_cpu` / `cost_ram` components when the rows carry them, otherwise by projected share. The group is recorded on each record (`baseline.attribution_group_ids`) and on its measurements, and it does not depend on the measurement window: a member measured again later (re-measurement, dismissed review) is still measured with its group, and its share is computed against every member still in effect, including members that keep an earlier realized measurement. The group never claims the workload delta twice.
- **Health gates.** Per-pod p95 usage must stay below the applied per-pod request × `VERIFICATION_USAGE_HEADROOM` (default 1.1); the restart delta (sum of per-pod increases of the cumulative counter, new pods counted from their first observation) must not exceed `VERIFICATION_MAX_RESTART_DELTA`; from Prometheus, no OOM kill (`kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}`), readiness at least `VERIFICATION_MIN_READINESS` (ready samples of `kube_pod_status_ready` over all samples of the observed pods, so a short-lived autoscaled pod weighs its lifetime, not a full share), and a CPU throttling ratio (`container_cpu_cfs_throttled_periods_total / container_cpu_cfs_periods_total`, over the workload's containers, never the pod cgroup or pause container) at most `VERIFICATION_MAX_THROTTLE_RATIO`. Health queries use `/api/v1/query` and fall back to `/prometheus/api/v1/query`. Any failed gate sends the whole change to `rollback_review`. A signal that cannot be read (no kube-state-metrics, no CPU limit, Prometheus unreachable) is reported `not_available` with the reason; it is never counted as a pass.
- **Immutable measurements.** Each decision is one row of `savings_measurements` (insert-only: database triggers reject `UPDATE` and `DELETE`) with its windows, signed per-quantity estimates, health gates, reasons and an evidence SHA-256. The recommendation keeps `baseline.measured_ledger_input` and `baseline.measurement_ids` as links; an inconclusive outcome keeps its measured input.
- **Terminal outcomes.** `not_realized` and `inconclusive` are never re-tested automatically. `POST /api/v1/recommendations/{id}/remeasure` starts a new measurement window that supersedes the previous measurement (the baseline still ends at the original change). `POST /api/v1/recommendations/{id}/rollback-review/dismiss` with a non-blank reason returns a false-positive rollback review to `applied` and measures it again. Both act on the whole attribution group: every member in the same state starts the same new window (its events carry `attribution_group_ids` and `requested_via`), so asking again for a sibling returns `409`.
- **Replays.** A decision persisted before a crash is replayed from its stored measurement on the next pass instead of being observed again, and the outcome metric is counted once, after the transition is written.

## Generation Flow

1. Metrics are collected and written to storage by the normal GreenKube collection pipeline.
2. `OptimizationEngine.refresh()` builds an `OptimizationContext`: a recent metrics window plus, when available, node snapshots, HPA targets, orphaned PersistentVolumes and orphaned LoadBalancers. This loading logic is shared by API recommendation requests and the startup scan.
3. Enabled **sources** run over the context. The native source runs the analyzers (grouping metrics by stable target: Kubernetes owner kind/name when present, inferred Deployment from ReplicaSet-style pod names when possible, otherwise the pod name). The VPA source reads recommendation-mode VPAs, and the Karpenter source evaluates NodePool consolidation from live NodePool/NodeClaim state.
4. Generated recommendations are **normalized, arbitrated and deduplicated**: when several sources own the same target, capability and type, the highest-priority source wins (VPA replaces native rightsizing for the same workload) and provenance is recorded in `sources`.
5. Each recommendation is **enriched** with a review-grade evidence block, a risk/confidence/effort assessment, a machine-readable `patch` plan and an expiry date, then scored with the multi-criteria ranking profile.
6. Recommended CPU and memory requests are floored to configured minimums.
7. API and startup paths convert recommendations to `RecommendationRecord` objects, upsert active records, and reconcile missing active records as stale.

The API and frontend are the only consumers of the engine; there is no CLI reporting command.

## Sources, Evidence And Ranking

**Sources.** Each recommendation carries `source` (`greenkube`, `vpa`, `karpenter`), an optional `source_ref` (e.g. `namespace/vpa-name`), a `sources` provenance list and a `capability` (used for arbitration). The VPA and Karpenter connectors are enabled by default (`RECOMMENDATION_VPA_ENABLED`, `RECOMMENDATION_KARPENTER_ENABLED`) and skip cleanly when their CRDs are absent; source precedence is controlled by `RECOMMENDATION_SOURCE_PRIORITY` (default `vpa,karpenter,greenkube`).

**Evidence.** Every recommendation embeds a `RecommendationEvidence` block: observation window and coverage, current vs proposed requests, CPU/memory distribution (avg, p50, p90, p95, p99, max), restart count, expected savings and method, confidence with factors, risk level with factors, rollback conditions, a `patch` action plan and `expires_at`. Retrieve the full block from `GET /api/v1/recommendations/{id}`; it is also rendered in the dashboard's evidence panel.

**Risk and confidence.** `risk_level` (low/medium/high) is derived from the observed headroom, restart/OOM history and blast radius; `confidence` (0–1) aggregates sample count, coverage and source authority. Both are explainable via `risk_factors` and the confidence factors inside the evidence block.

**Ranking.** `ranking_score` combines carbon impact, cost impact, confidence, risk, effort, actionability and source authority. Profiles (`balanced`, `carbon_first`, `cost_first`, `quick_wins`, `low_risk`) can be selected per request; `ranking_factors` exposes the contribution of each criterion.


## Recommendation Types

GreenKube currently has eleven recommendation types.

| Type | Scope | Current trigger |
|---|---|---|
| `ZOMBIE_POD` | pod or workload | Target has cost above `ZOMBIE_COST_THRESHOLD` and energy below `ZOMBIE_ENERGY_THRESHOLD`. Projected as a workload deletion: the observed allocated cost and the allocated CO2e share (idle and embodied by reservation, dynamic by usage), annualized from the observed window. |
| `RIGHTSIZING_CPU` | pod or workload | Average CPU usage divided by latest CPU request is below `RIGHTSIZING_CPU_THRESHOLD`. The target request is based on P95 usage, observed max, average usage, and `RIGHTSIZING_HEADROOM`, then floored by `RECOMMENDATION_MIN_CPU_MILLICORES`. Savings price only the released CPU reservation, at the container's observed OpenCost cost per reserved core when available, otherwise at the node's CPU rate; CO2e is the idle and embodied share released ([Impact Model](methodology/impact-model.md#container-requests-rightsizing-native-analyzers-and-vpa)). |
| `RIGHTSIZING_MEMORY` | pod or workload | Average memory usage divided by latest memory request is below `RIGHTSIZING_MEMORY_THRESHOLD`. The target request uses the same balanced sizing formula and is floored by `RECOMMENDATION_MIN_MEMORY_BYTES`. Savings price only the released memory reservation, at the container's observed cost per reserved GiB when available, otherwise at the node's RAM rate; projected CO2e is zero until memory power is modeled. CPU and memory savings on one container never add up to more than its observed cost. |
| `AUTOSCALING_CANDIDATE` | pod or workload | CPU usage has enough samples, coefficient of variation is above `AUTOSCALING_CV_THRESHOLD`, max/mean spike ratio is above `AUTOSCALING_SPIKE_RATIO`, and no matching HPA was found for a non-pod owner target. |
| `OFF_PEAK_SCALING` | pod or workload | Timestamped CPU usage shows at least `OFF_PEAK_MIN_IDLE_HOURS` consecutive hours below `OFF_PEAK_IDLE_THRESHOLD` of the daily peak. The recommendation includes a suggested UTC scale-to-zero window. Savings are the replica-hours removed in that window, priced at the workload's allocated rate and multiplied by the realization factor. |
| `IDLE_NAMESPACE` | namespace | Namespace total energy is below `IDLE_NAMESPACE_ENERGY_THRESHOLD` while cost is positive. Common system namespaces are excluded unless `RECOMMEND_SYSTEM_NAMESPACES` is enabled. Projected as deleting every workload of the namespace. |
| `CARBON_AWARE_SCHEDULING` | pod or workload | Target average grid intensity is more than `CARBON_AWARE_THRESHOLD` times the average for its electricity zone. Projected CO2e savings are estimated from the high-carbon share. |
| `OVERPROVISIONED_NODE` | node | Node average CPU utilization, and memory utilization when capacity is available, are below `NODE_UTILIZATION_THRESHOLD`. |
| `UNDERUTILIZED_NODE` | node | Node has fewer than three pods and average CPU utilization below 15%. |
| `ORPHANED_PERSISTENT_VOLUME` | cluster | A PersistentVolume is in `Released` phase (its PVC was deleted but the volume was not reclaimed) or its `claimRef` references a PVC that no longer exists. The PV name is stored in the `pod_name` field because PVs are cluster-scoped. Deleting the volume releases the provisioned storage. Projected cost savings prefer the real storage cost reported by OpenCost for the volume (annualized from the observation window); when OpenCost has no cost data (e.g. on-premises or local storage), the estimate falls back to the provisioned capacity and `STORAGE_COST_PER_GIB_MONTH` (default `$0.10`/GiB-month). Projected as an asset deletion (realization factor 1). CO2e savings are not projected because the energy model does not cover storage. |
| `ORPHANED_LOAD_BALANCER` | cluster | A Service of type `LoadBalancer` has no ready backing endpoints, so its selector matches no pods and the provisioned cloud LoadBalancer routes traffic to nothing while continuing to bill hourly. The Service name is stored in the `pod_name` field and its namespace in the `namespace` field. Deleting the Service removes the cloud LoadBalancer. Projected cost savings prefer the real LoadBalancer cost reported by OpenCost for the Service (`OpenCostCollector.collect_lb_costs()` aggregates allocations by `service` and annualizes the window cost); when OpenCost has no cost data, the estimate falls back to `LOAD_BALANCER_COST_PER_MONTH` (default `$18.00`/month). Projected as an asset deletion (realization factor 1). CO2e savings are not projected because the energy model does not cover load balancers. |

Projected savings come from the [Impact Model](methodology/impact-model.md): each projected recommendation carries an `impact` object (model version, cost and CO2e breakdown, capacity released, realization factor, unit prices and their source). `potential_savings_cost` and `potential_savings_co2e_grams` are its expected annual values. Karpenter NodePool recommendations project the removable nodes' price and their idle and embodied emissions; the nodes kept must fit both the CPU and the memory requests. Autoscaling, carbon-aware and node-level native recommendations do not use the model yet.

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
| `GET` | `/recommendations/savings?namespace=&last=` | Returns separate, never-summed blocks: `projected_annual` and `run_rate_annual` (current yearly rates of applied changes, projected vs measured), `realized_to_date` (measured ledger accruals over the `last` window, all time without it), plus `projected_to_date`, `user_declared_annual` and `user_declared_to_date`. A window starting mid-hour includes that partial hour. Answers `503` when the ledger cannot be read; it never falls back to recommendation records. The flat `total_*`, `measured_*` and `prorated_*` fields are deprecated aliases (`total_*` = realized). |
| `PATCH` | `/recommendations/{id}/apply` | Marks a recommendation as `applied`, stores actual CPU or memory values when supplied and freezes the verification baseline; only `active` or `pr_open` records can be applied (`409` otherwise). Optional `carbon_saved_co2e_grams` / `cost_saved` are *user-declared* annual savings: they must be finite and at most 10× the projection (`422` otherwise); negative values are accepted and flagged. They accrue with method `user_declared` and are never reported as measured. |
| `PATCH` | `/recommendations/{id}/ignore` | Marks an `active`, `stale` or `expired` recommendation as `ignored` and stores the reason; `409` for an open PR or an applied change. |
| `DELETE` | `/recommendations/{id}/ignore` | Restores an ignored recommendation to `active`; `409` for any other status. |
| `POST` | `/recommendations/{id}/apply-pr` | Compatibility route to the validated GitOps plan path. `dry_run=true` returns the plan diff; a normal request approves and queues that plan. |
| `GET` | `/recommendations/{id}/pull-requests` | Lists the pull-request attempts and their status for a recommendation. |
| `GET` | `/automation/status` | Reports secret-free automation readiness, worker heartbeat, queue depth, discovery freshness, renderer configuration, and provider capability/preflight state. |

### GitOps plan and PR workflow

The bot resolves workload sources from Argo CD and Flux discovery, not from mandatory workload annotations:

1. GreenKube discovers the Git source through Argo CD or Flux and persists a generation-bound workload binding.
2. The plan API accepts recommendation IDs only, snapshots the authorized Git revision, and validates the source and rendered diff for raw YAML/JSON, Kustomize, and the supported Helm subsets.
3. Approval binds to the exact preview digest. A durable idempotent worker creates one commit and PR per plan; duplicate requests reuse the same plan operation.
4. After the PR merges, the controller observes the Argo CD/Flux revision (the landed commit, or a later commit that contains it and still carries the planned values) and workload readiness. Apply detection compares the post-baseline workload identity and per-container requests; verification attributes measured savings only after its health and evidence gates pass.

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
- A **Preview plan** action on eligible container-targeted rightsizing cards: the server resolves the GitOps binding, renders the change, and returns a digest-addressed diff. Approval submits that exact digest; the worker creates or adopts the pull request. The UI tracks the durable operation until the provider result is available.
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
| `greenkube_co2e_savings_attributed_grams_total` | Counter: CO2e accrued in the ledger, by recommendation type and `method` (`projected`, `measured`, `user_declared`). Never decreases. |
| `greenkube_cost_savings_attributed_dollars_total` | Counter: cost accrued in the ledger, by recommendation type and `method`. Never decreases. |
| `greenkube_co2e_savings_reversed_grams_total` / `greenkube_cost_savings_reversed_dollars_total` | Counters of reversals: every superseded accrual (for example after a rollback review) plus negative accruals such as negative declared savings. Net = attributed − reversed. |
| `greenkube_savings_run_rate_annual_co2e_grams` / `..._cost_dollars` | Current annual savings rate of applied recommendations, by `method`. |
| `greenkube_dashboard_savings_co2e_grams_total` | Realized (measured) CO2e savings for fixed dashboard windows. Prefer this for Grafana time-window panels. |
| `greenkube_dashboard_savings_cost_dollars_total` | Realized (measured) cost savings for fixed dashboard windows. Prefer this for Grafana time-window panels. |
| `greenkube_dashboard_savings_accrued_co2e_grams` / `..._cost_dollars` | Ledger accruals for fixed dashboard windows, by `method`. Never sum methods. |

The Grafana dashboard uses `greenkube_top_recommendations` in the Actionable Recommendations row. Dashboard variables let users choose the ranking metric (`co2` or `cost`) and displayed recommendation count.

## Realized Savings

Applying a recommendation records its annual savings estimate on the recommendation row. If the apply request includes explicit `carbon_saved_co2e_grams` or `cost_saved`, they are validated as user-declared savings (finite, at most 10× the projection, negative values flagged) and stored under `baseline.user_declared_savings`. Declared values never replace the projection stored on the recommendation row, which is always computed server-side as described below. If the declaration cannot be stored after the recommendation was applied, the API answers `503` and the recommendation keeps accruing its projection only.

When explicit savings are omitted:

- CPU and memory rightsizing scale savings by the actual reduction compared with the original recommendation. For example, if the recommendation was 500m to 200m CPU but the user applied 350m, GreenKube records half of the projected savings.
- Other recommendation types fall back to the projected potential savings.

Applied recommendations can later be refreshed when the same issue is observed again:

- For CPU and memory rightsizing, the current observed request updates the actual value and recalculates realized savings.
- For non-resource recommendation types, seeing the same issue again sets realized savings back to zero, because the implementation no longer appears effective.

### Savings ledger

The `SavingsAttributor` turns annual savings rates into *accruals* on a fixed UTC grid of one-hour slots: `accrual = annual rate × seconds of the slot after apply ÷ seconds per year`. The slot in which a change was applied is accrued pro rata, and so is the slot in which it was verified: the projection accrues until `verified_at` and the measured savings after it.

- **Idempotent.** Rows are keyed by `(recommendation_id, measurement_method, slot_start)`. The `accrual_watermarks` table stores the end of the last accrued slot; each run accrues every complete slot after it and advances it with a compare-and-swap in the same transaction as the inserts. Restarts, retries and a second controller cannot double count, and slots missed during downtime are backfilled (up to 7 days on the first run after an upgrade, at most 168 slots per run). A recommendation that has never accrued, for example a change detected after its `applied_at`, is also caught up for the slots since `applied_at` that are already behind the watermark (up to 7 days).
- **Append-only.** Ledger rows are never compressed, overwritten or deleted. Hourly rollups are read-side views (`savings_accruals_v1`, `savings_accruals_v1_hourly_rollup`). Hourly rows written by the compression job of earlier releases are kept and flagged `legacy_compressed`; the savings API adds a `legacy_compressed` diagnostic when a window includes them, because part of their value was lost (BUG-32).
- **Methods are never mixed.** `applied`/`verifying` records whose verification is pending accrue `projected` savings (or `user_declared` for declared quantities); `verified` records accrue `measured` savings. Records that are not realized, inconclusive, failed, in rollback review or reverted accrue nothing. When a recommendation enters `rollback_review`, its prior rows are flagged `superseded=true` (never deleted).
- **Isolated failures.** Each recommendation is processed on its own; an invalid one (non-finite values, missing `applied_at`) increments `greenkube_attribution_errors_total{reason}` and never blocks the others.

The API and the dashboard report realized (measured) savings separately from projected and user-declared accruals, and the `greenkube_savings_measured_vs_projected_ratio` gauge tracks the measured share of net accruals.

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

`RECOMMENDATION_APPLY_TOLERANCE` drives the automatic **apply detection**: Kubernetes workload snapshots preserve CPU and memory requests per container, and a typed GitOps action is considered only after its persisted action status reaches `sync_pending`. It is marked applied only when the targeted live request matches the recommended value within tolerance. A decrease by itself, including an unrelated large decrease, is not sufficient. The pre-apply workload-generation and per-container baseline is captured before provider mutation.

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
| Automation API routes | `src/greenkube/api/routers/automation.py`, `plans.py`, `gitops.py`, `webhooks.py` |
| GitOps automation | `src/greenkube/gitops/`, `src/greenkube/automation/` |
| Prometheus gauges | `src/greenkube/api/metrics_endpoint.py` |
| Startup scan & lifecycle job | `src/greenkube/api/startup.py`, `src/greenkube/cli/start.py` |
| Migrations | `src/greenkube/core/migrations/scripts/{sqlite,postgres}/` |
| Storage adapters | `src/greenkube/storage/recommendation_mapper.py`, `src/greenkube/storage/sqlite/recommendation_repository.py`, `src/greenkube/storage/postgres/recommendation_repository.py` |
| Frontend | `frontend/src/routes/recommendations/+page.svelte`, `frontend/src/lib/api.js` |
| End-to-end tests | `tests/integration/test_recommendation_lifecycle_e2e.py` |
