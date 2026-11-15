# Recommendation Lifecycle

> **Note:** Recommendations are the input to the GreenKube loop: aggregate → decide → deliver as a pull request → prove → ledger (see [Concepts](concepts.md)). This page documents recommendation generation, lifecycle, verification and savings. The plan, pull-request, sync and rollback workflow is documented in [GitOps automation](automation.md).

This page describes how GreenKube recommendations are generated, stored, shown, and turned into measured impact. It reflects the current code paths in `src/greenkube/core/optimization/`, `src/greenkube/models/metrics.py`, `src/greenkube/api/routers/recommendations.py`, the SQLite/PostgreSQL recommendation repositories, and the frontend recommendations page.

## What GreenKube Analyzes

Recommendations are derived from stored `CombinedMetric` records. These records already combine Kubernetes workload identity, resource requests, observed CPU and memory usage, energy, CO2e, cost, timestamps, grid intensity, and node metadata collected elsewhere in GreenKube.

Scheduled scans use `RECOMMENDATION_LOOKBACK_DAYS` (default 14 days, two weekly cycles) of history. On SQLite and PostgreSQL they do not load the metric rows of that window: they read one **usage summary** per workload container, computed in SQL from the `workload_usage_hourly` rollup (see [Usage summaries](#usage-summaries-memory-bounded-scans)). Projected savings are annualized over the span actually observed (from the first rolled hour in the lookback to the scan), so a cluster with three days of history is not extrapolated as if it had fourteen. Each target is annualized over its own observed coverage, from its first rolled hour to the scan (at least one hour): a workload deployed two hours ago is projected at its run-rate, not at 2/336 of it, and coverage below one week is flagged in the projection and lowers its confidence (see [Impact Model](methodology/impact-model.md#annualization-and-observed-coverage)).

The optimization context can also include these optional Kubernetes and cost inputs:

| Input | Purpose |
|---|---|
| Latest node snapshots | Enables node-level recommendations (overprovisioned nodes). |
| HPA targets | Prevents autoscaling recommendations for workloads that already have a HorizontalPodAutoscaler. |
| Orphaned PersistentVolumes | Enables PV cleanup recommendations. The `PVCollector` evaluates every PersistentVolume and reports only `Released` or `Failed` volumes whose claim is gone and that nothing else depends on (no StatefulSet replica, no VolumeSnapshot, not local), with the evidence. See [Why GreenKube thinks a resource is unused](#why-greenkube-thinks-a-resource-is-unused). Requires read access to `persistentvolumes`, `persistentvolumeclaims`, `statefulsets` and (optional CRDs) `volumesnapshots`/`volumesnapshotcontents`; the Helm chart's `ClusterRole` includes them. When OpenCost is reachable, real per-volume storage costs are fetched via `OpenCostCollector.collect_pv_costs()` and used for the projected savings; otherwise the capacity-based estimate applies. |
| Orphaned LoadBalancers | Enables LoadBalancer cleanup recommendations. The `LoadBalancerCollector` evaluates every Service of type `LoadBalancer` and reports only those it can prove unused (selector matching no pod or workload, no endpoint in Endpoints or EndpointSlices, no Gateway, Ingress or route reference, no gateway or mesh controller), with the evidence. See [Why GreenKube thinks a resource is unused](#why-greenkube-thinks-a-resource-is-unused). Requires read access to `services`, `endpoints`, `endpointslices`, `pods`, workloads, `ingresses` and (optional CRDs) Gateway API `gateways` and routes; the Helm chart's `ClusterRole` includes them. When OpenCost is reachable, real per-service LoadBalancer costs are fetched via `OpenCostCollector.collect_lb_costs()` and used for the projected savings; otherwise the flat `LOAD_BALANCER_COST_PER_MONTH` estimate applies. |

VPA and Karpenter are separate optional recommendation sources. VPA reads recommendation-mode objects when its CRD is available; Karpenter reads NodePools/NodeClaims when compatible CRDs are available. Both connectors are enabled by default and skip cleanly when their APIs are absent.

During scans, metrics from Kubernetes namespaces that no longer exist are filtered out when the Kubernetes API is reachable. This lets reconciliation mark old active recommendations from deleted namespaces as stale instead of regenerating them forever. On SQLite and PostgreSQL the filter is applied in SQL, so the history of deleted namespaces also counts neither in per-node averages (node and Karpenter analyses) nor in the observed window used to annualize savings.

## Usage Summaries (Memory-Bounded Scans)

A scan's memory grows with the number of workload targets, not with pods × lookback × samples (roadmap E3, BUG-12). Before, a scan materialized up to `RECOMMENDATION_MAX_METRIC_ROWS` (750,000) metric objects, about 2.6 GB.

**Rollup.** The `workload_usage_hourly` table (migration `0052`) holds one row per hour, workload target, container and node. The target is the stable recommendation target (owner kind and name, an inferred Deployment, or the bare pod with kind `Pod`); container `""` is the pod-level aggregate, which exists for every pod (rebuilt from the containers when a pod row is missing). Each row stores:

- sample, step and distinct-pod counts and the replica-seconds observed (`sample_count / step_count` is the average replica count);
- CPU and memory per replica: average, p50, p90, p95, p99 and maximum, plus the mean of squared CPU values (for the coefficient of variation);
- the latest requests and limits of the hour, the offset of the row's newest sample in the hour (`latest_sample_offset_seconds`, migration `0076`), and the sum of per-sample requests (requested capacity per node);
- allocated cost (with its components and `cost_source`), CO2e, energy, PUE, grid intensity and zone, restarts.

The compression job (every hour) re-rolls the last 24 hours, because the carbon-intensity recompute still changes them, and backfills up to the lookback on its first run. Older hours that receive rows later are re-rolled too: after each run the rollup saves the highest raw row id it has read per raw table (`workload_usage_rollup_marks`, migration `0053`), and the next run (compression or scan) re-rolls every hour of the lookback that received rows above those marks. Collection catch-up after an outage (`COLLECTION_MAX_BACKFILL` above 24 hours) and `greenkube start --last` are therefore never skipped; only those hours are re-rolled, not the whole span since the oldest one. Rows rewritten in place (an upsert of an existing sample) keep their id and are not detected; they only matter beyond the 24-hour window, where re-collected values are the same. An hour is rolled from its raw rows, or from the compressed hourly tables once raw rows are pruned (`source = hourly`). Each scan first rolls the hours collected since the last run, so it sees the newest data. The rollup is pruned with `METRICS_AGGREGATED_RETENTION_DAYS`.

**Per container and per replica.** Every pod row and container row is one point of the distribution: a summary never sums replicas or pods, so a 2-replica StatefulSet whose replicas each use 600 MiB has a 600 MiB p99, not 1.2 GiB. A pod's owner is read from any of its rows, so the replicas of a StatefulSet share one target. At collection, duplicate cAdvisor series of one container (kubelet `/metrics/resource` scraped next to `/metrics/cadvisor`, two scrape jobs, two cgroup paths of one container) are merged with `max`, never summed. Where the kubelet only exports pod-level cgroups (no `container` label, e.g. Minikube with the Docker driver), the pod's working set is collected as a pod-level value and is never attributed to a container; a pod cgroup working set below one memory page (cAdvisor reported 0 or 8 bytes for hours there) is a gap, not a measurement.

**A container never uses more than its pod.** On such clusters a container's usage only comes from the Kubernetes Metrics API, sampled once per scan for the latest step. Some kubelets (cri-dockerd) report a container working set that counts page cache the pod cgroup's working set excludes: on Minikube the `prometheus` container of `prometheus-k8s` read ~756 MiB in a pod whose own working set was ~566 MiB, and the gap grows with Prometheus's memory-mapped TSDB. When the Metrics API containers of a pod add up to more than the pod cgroup working set (or CPU) that Prometheus measured for the same step, by more than 5%, the collector scales them down to the pod value, keeping their split, and flags the rows `container_usage_exceeds_pod` (estimated). Data that could not be checked at collection (no pod value for that step, or rows collected before this rule) is bounded at analysis: see [How GreenKube sizes increases](#how-greenkube-sizes-increases).

**Warm-up.** Points in the first `RIGHTSIZING_WARMUP_MINUTES` (default 10) after a container restart (the pod's restart count changed) or after a new replica appears are left out of the percentiles: startup work such as a Prometheus WAL replay, JIT compilation or cache fill is not steady-state demand. The maximum keeps every point. A restart in the last minutes of an hour still excludes the next hour's first points (the rollup reads the raw pod rows just before the hour). Hours re-rolled from the compressed hourly tables (raw rows pruned) have one row per pod and hour whose restart count is the hour's maximum: a pod whose restart count rose since the previous compressed hour, or that was absent from it (a new replica), warmed up during the hour. Its row leaves the percentiles when other points remain, and otherwise counts with its hourly average instead of its peak; the peak still reaches the maximum.

**Latest request.** A summary's current request and limit are those of the newest sample of its last hour. When a rollout moves a pod to another node within the hour, the new pod's row holds the newest sample, so the new request is visible at once instead of the hour's maximum of the old and new requests. Rows rolled before migration `0076` have no sample offset and keep the maximum.

**Percentiles.** Hourly percentiles are exact nearest-rank percentiles over the hour's per-step values: the per-step maximum when the collector stored one, otherwise the step value (a rate over the step for CPU, a sample for memory), never the average of averages. Over the lookback, a percentile is the nearest-rank percentile of the hourly percentiles (the `ceil(p × hours / 100)`-th smallest), and the maximum is the largest hourly maximum. Across the nodes of one hour, the larger percentile is kept. The approximation is close for p95/p99 and conservative for a single burst hour; hours rolled from compressed hourly tables use each pod's hourly maximum and are conservative too.

**What a scan reads.** Three bounded SQL aggregations build the usage snapshot: the summaries (one per target container), the pod-level CPU profile per UTC hour of day (off-peak analysis) and the per-node average usage and requests (node and Karpenter analyses). A fourth returns **baseline rows** for the Impact Model and the evidence: pod-level rows per target, node, hour of day and requests; container rows per target, container, node and requests. Their `timestamp` is the last hour they cover, so the newest rows carry the current requests, and they carry the summary percentiles so evidence statistics match the analyzer's. Pod-level baseline rows also carry the DCGM GPU power (B5) as the duration-weighted average of `gpu_watt_seconds` (migration `0060`), so deletions and schedules of GPU workloads keep their GPU energy; hours rolled from the compressed hourly tables have none, because those tables do not keep GPU power. On SQLite these queries run with a 2 MiB page cache, no memory map and on-disk sorts, then restore the connection settings.

A scan of 500 pods over 14 days (336,000 rollup rows) peaks below 200 MB RSS and finishes in under 10 seconds on SQLite and PostgreSQL (`tests/integration/test_bounded_scan_performance.py`; the acceptance bound is 300 MB and 60 s). Backends without the rollup (custom repositories) keep the row-loading path bounded by `RECOMMENDATION_MAX_METRIC_ROWS`.

## Lifecycle States

A freshly generated in-memory `Recommendation` has no lifecycle state until it is converted into a `RecommendationRecord`. Every transition writes a row to `recommendation_events` (actor, payload, timestamp) exposed by `GET /api/v1/recommendations/{id}/events`.

| State | Meaning | How it is reached |
|---|---|---|
| `active` | The recommendation is currently valid and visible in active lists, top recommendations, Prometheus active gauges, Grafana cards, and the frontend Active tab. | Created by `RecommendationRecord.from_recommendation()` and inserted or refreshed by repository upsert. Ignored recommendations can also be restored to active. |
| `stale` | A previously active recommendation no longer appears in the latest generated set and every source that supported it completed the scan, or its proposal is already live without it being its application (`no_effective_change`). It is kept in history but no longer shown as active. | `reconcile_active_recommendations()` at the end of each scan (see [Per-source reconciliation](#per-source-reconciliation)); apply detection, with a `stale` event whose reason is `no_effective_change`, when the live request is a change already recorded on another recommendation of the same target or landed before the recommendation was created; the phantom-change repair (below the table). |
| `expired` | The recommendation passed `expires_at` (`generated_at + RECOMMENDATION_TTL_DAYS`) without being applied. It is hidden from active lists but retained for history. | `RecommendationLifecycle.expire()` during every scan and every lifecycle job. |
| `ignored` | A user intentionally hid the recommendation with an optional reason. Ignored records are preserved for review and can be restored. | `PATCH /api/v1/recommendations/{id}/ignore`, only from `active`, `stale` or `expired` (an open PR or an applied change returns `409`). |
| `pr_open` | A pull request that applies the recommendation is open, or merged and not yet observed in the cluster. Scans no longer rewrite its baseline and proposal, it never goes stale or expires, and the active listings (`GET /recommendations`, `/recommendations/active`) still return it with the `pull_request_open` blocker. | Set in the same transaction as the GitOps action status: `pr_open`, `merged` and `sync_pending` make an `active` recommendation `pr_open`; a PR closed unmerged, a failed action or a cleared plan returns it to `active` (events `pr_opened` and `pr_closed`). Apply detection moves it to `applied`. |
| `applied` | The change landed in the cluster. Applying freezes a verification baseline. A measured change whose saving was not realized (`verification_status=failed`) or inconclusive also stays `applied`. | `PATCH .../apply` (manual, only from `active` or `pr_open`; 409 otherwise), apply detection (Kubernetes API, once the rollout completed), or a merged PR. A dismissed rollback review returns here. |
| `verifying` | The observation window after apply is in progress (extended once because of insufficient samples, or started again by an explicit re-measurement). | `RecommendationVerifier` lifecycle job; `POST .../remeasure`. |
| `verified` | The measured saving was realized and no health gate failed; the ledger accrues the measured savings (method `measured`) from verification onward. | `RecommendationVerifier` lifecycle job (never directly from `active`). |
| `regressed` | A realized saving that no longer holds: the drift monitor found its per-replica saving lost beyond `CHANGE_DRIFT_THRESHOLD` for `CHANGE_DRIFT_SUSTAINED_DAYS` days, or the pod template no longer matches the measured change (spec drift, recreated workload). Accrual stops and the measured accruals since the drift (or the template change) began are reversed ([drift monitor](methodology/savings-measurement.md#10-drift-monitor-c8)). | Only from `verified`. |
| `rollback_review` | A health gate failed (restarts, OOM kills, readiness, CPU throttling, per-pod usage above the applied request × headroom): the change needs review and prior savings rows are superseded. | `RecommendationVerifier` lifecycle job. |
| `reverted` | The change was rolled back and the original per-container baseline was observed again. | The rollback plan reached `synced` and apply detection confirmed the original requests. |
| `failed` | The PR was closed without merge, or the change could not be completed. | PR tracking. |

**Phantom-change repair.** In earlier releases, apply detection could record a rightsizing as applied (`detected`) while the live request never moved from the baseline (a 20Mi → 23Mi proposal within tolerance of the untouched 20Mi), or from a rollout that predates the recommendation. Every `recommendation-lifecycle` tick runs an idempotent repair: a `detected` change in the applied family whose frozen baseline says the rollout completed before the recommendation was created, or whose `actual_*` request equals the baseline while the newest rolled-up request (observed in an hour that starts after `applied_at`) is still the baseline, moves to `stale` with a `retired` event (code `phantom_apply_detection`, with the evidence), its realized savings cleared and its savings-ledger rows superseded; its change measurement is closed (`retired`) when every member of the change is a phantom. The row is retired last, so if the ledger or the measurement cannot be updated it stays a candidate and the next tick retries. A phantom under `rollback_review` is left to the operator: once the review is dismissed (back to `applied`), the next tick retires it. Without that evidence (no recent usage, or a live request that reached the proposal: a real change whose `actual_*` an old rescan reverted) nothing changes.

**Apply succeeded vs recommendation succeeded.** `application_method` records how the change landed (`manual`, `detected`, `pr_merge`, `webhook`, `polling`); `verified_at` and `verification_status` record whether the outcome matched the projection. Only verified savings count as measured. A GitOps action is not eligible for apply detection until its plan reaches `synced`; a merged-but-unsynced plan moves to `sync_failed` after the configured timeout.

**Change measurement.** A change found by apply detection is recorded with a frozen baseline snapshot and measured once by the change-measurement pipeline: a 7-day post-window starting one hour after the rollout, an hour-of-week counterfactual adjusted by a load driver, a 90% moving-block bootstrap interval, confounder checks and an outcome (`realized`, `not_realized`, `inconclusive`, `contaminated`). The saving is split across the change's recommendations by cost component (or projected share), each recommendation receives its row in `savings_measurements` (with `change_id`) and moves to `verifying` during the post-window, then to `verified`, `applied` (`failed` or `inconclusive`) or `rollback_review` exactly as below. Health gates run after `VERIFICATION_WINDOW_HOURS` and again at the decision. See [Savings measurement](methodology/savings-measurement.md) and `GET /api/v2/changes`. The fixed-horizon verification below still measures changes recorded before this pipeline, recommendations applied with `PATCH .../apply`, and every change when `CHANGE_MEASUREMENT_ENABLED=false`.

**Verification (fixed horizon).** Every applied change gets one measurement, decided once:

- **Windows.** `applied_at` is the rollout completion time (`updatedReplicas == replicas == availableReplicas` and `observedGeneration >= generation`; Deployments report it through their `Progressing` condition, otherwise the detection time is used). The after-window is `[applied_at, applied_at + VERIFICATION_WINDOW_HOURS)`. The baseline window has the same length and ends at the PR merge (or the plan approval for a manual apply), never after `applied_at`. The frozen baseline records the evidence window it copies (`captured_at`, `evidence_window`), its freeze time (`frozen_at`) and `baseline_window_end`.
- **Trusted rows.** When rows carry cost provenance, only `cost_source` in `opencost`, `kubecost` or `pricing_catalog` with a finite, non-negative cost counts (judged by the same provenance helpers as every other consumer); legacy rows need `total_cost > 0` and `is_estimated = false`. A timestamp counts only when every replica row in it is trusted, and rates divide by the time the rows cover, so raw and hourly rows mix correctly. A container-scoped change is measured on its container rows when they carry trusted cost, otherwise on its workload (`measurement_scope=workload_fallback`).
- **No traffic normalization.** The traffic ratio is recorded for information only until load-driver counterfactuals ship.
- **Outcome.** `realized` (status `verified`) when the measured saving exceeds 2% of the baseline rate; `not_realized` (status `applied`, `verification_status=failed`) otherwise; `inconclusive` when trusted samples stay below `VERIFICATION_MIN_SAMPLES` after one extension. The projection is not a gate: `VERIFICATION_MIN_SAVINGS_RATIO` (default 0.5) only adds a note when a realized saving is far below its projection. Request rightsizing never claims a CO2e saving in the interim verifier; the CO2e delta is kept in the measurement as `not_claimed` and `measured_co2e_saved_grams` is 0. It is compared with the projected operational CO2e only (never with embodied emissions the rows do not carry) and, for records measured together, split by their projected operational CO2e, as change measurement does.
- **Attribution groups.** CPU and memory records on the same container applied by the same plan (whatever the delay between their detections), or, without a plan, applied within 15 minutes of each other, form one change and are measured once. The cost delta is split by the `cost_cpu` / `cost_ram` components when the rows carry them, otherwise by projected share. The group is recorded on each record (`baseline.attribution_group_ids`) and on its measurements, and it does not depend on the measurement window: a member measured again later (re-measurement, dismissed review) is still measured with its group, and its share is computed against every member still in effect, including members that keep an earlier realized measurement. The group never claims the workload delta twice.
- **Health gates.** Per-pod p95 usage must stay below the applied per-pod request × `VERIFICATION_USAGE_HEADROOM` (default 1.1); the restart delta (sum of per-pod increases of the cumulative counter, new pods counted from their first observation) must not exceed `VERIFICATION_MAX_RESTART_DELTA`; from Prometheus, no OOM kill (`kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}`), readiness at least `VERIFICATION_MIN_READINESS` (ready samples of `kube_pod_status_ready` over all samples of the observed pods, so a short-lived autoscaled pod weighs its lifetime, not a full share), and a CPU throttling ratio (`container_cpu_cfs_throttled_periods_total / container_cpu_cfs_periods_total`, over the workload's containers, never the pod cgroup or pause container) at most `VERIFICATION_MAX_THROTTLE_RATIO`. Health queries use `/api/v1/query` and fall back to `/prometheus/api/v1/query`. Any failed gate sends the whole change to `rollback_review`. A signal that cannot be read (no kube-state-metrics, no CPU limit, Prometheus unreachable) is reported `not_available` with the reason; it is never counted as a pass.
- **Immutable measurements.** Each decision is one row of `savings_measurements` (insert-only: database triggers reject `UPDATE` and `DELETE`) with its windows, signed per-quantity estimates, health gates, reasons and an evidence SHA-256. The recommendation keeps `baseline.measured_ledger_input` and `baseline.measurement_ids` as links; an inconclusive outcome keeps its measured input.
- **Terminal outcomes.** `not_realized` and `inconclusive` are never re-tested automatically. `POST /api/v1/recommendations/{id}/remeasure` starts a new measurement window that supersedes the previous measurement (the baseline still ends at the original change). `POST /api/v1/recommendations/{id}/rollback-review/dismiss` with a non-blank reason returns a false-positive rollback review to `applied` and measures it again. Both act on the whole attribution group: every member in the same state starts the same new window (its events carry `attribution_group_ids` and `requested_via`), so asking again for a sibling returns `409`.
- **Replays.** A decision persisted before a crash is replayed from its stored measurement on the next pass instead of being observed again, and the outcome metric is counted once, after the transition is written.

## Scheduled Scans

Recommendation scans run **only in the controller** (`greenkube start`), behind the controller Lease, never in API replicas:

- The `recommendation_scan` job polls every 15 seconds. When no cluster-wide scan was requested within `RECOMMENDATION_SCAN_INTERVAL` (default `1h`), it queues a `scheduled` run; then it claims and executes queued runs, oldest first.
- The poll stays idle until the controller has finished its initial work (collection, node analysis, lifecycle). That work ends with one cluster-wide scan that always runs, even when a scan from the previous pod still covers the interval, so the first scan after a (re)start sees the freshly collected data. In leader mode this happens when the controller first acquires the Lease.
- Every scan is an **optimization run** (`optimization_runs` table): `queued` → `running` → `succeeded` or `failed`. Queuing and claiming are single conditional SQL statements, so two API replicas plus one controller (or a Lease hand-over between controllers) produce exactly one scheduled scan per interval, and each queued run executes once.
- `POST /api/v1/optimization/runs` queues an on-demand run and returns `202 {"run_id", "status": "queued"}`. While a run is still queued for the same scope, its id is returned instead of queuing a duplicate; a queued cluster-wide run also covers namespaced requests. Poll `GET /api/v1/optimization/runs/{run_id}` until the status is terminal, then reload the recommendations.
- `refresh=true` on the recommendation GET endpoints is **deprecated**: it returns the current data with a run id in the `X-GreenKube-Run-Id` response header. When a run covering the scope (the same namespace, or cluster-wide) is queued, running, or finished within the last 5 minutes, that run's id is returned (a failed run is reused for 1 minute only, then a refresh queues a retry); otherwise a `refresh` run is queued. Repeated page loads therefore do not chain scans. GET endpoints never run analyzers or write recommendations.
- A run left `running` for more than 30 minutes (for example because its controller crashed) is marked `failed` by the next cycle. If its controller finishes it later, the `failed` outcome is kept.
- Succeeded and failed runs older than 30 days are deleted when the next cluster-wide run is queued.
- If no controller is running, queued runs stay `queued`.

Each finished run records:

| Field | Meaning |
|---|---|
| `recommendation_count` | Recommendations written by the run (a last known record kept while one of its sources is incomplete is not counted). |
| `sources` | Per-source health: `succeeded`, `incomplete`, `failed` (with `error`, e.g. `timeout`) or `unavailable`, plus the number of recommendations and the duration. |
| `failed_source_count` | Sources that failed or returned a partial result. A run with some failed sources is still `succeeded` (degraded, with `error` naming them); it is `failed` only when every attempted source failed, or when loading metrics or persisting failed. |
| `new_count`, `changed_count`, `resolved_count`, `unchanged_count` | Diff against the active set of the scanned scope just before the run (for a namespaced run, that namespace only; cluster-scoped output of such a run is not counted). *Changed* means the proposal changed (winning source, current or recommended requests, schedule, node); projected-savings drift alone is not a change. *Resolved* recommendations became `stale`. |
| `diff` | Up to 100 fingerprints per list (`new`, `changed`, `resolved`), with `truncated` when a list was longer. |

## Generation Flow

1. Metrics are collected and written to storage by the normal GreenKube collection pipeline.
2. `OptimizationEngine.run_scan()` builds an `OptimizationContext`: a recent metrics window plus, when available, node snapshots, HPA targets, orphaned PersistentVolumes and orphaned LoadBalancers.
3. Enabled **sources** ([Source SDK](sources.md)) run over the context, each in isolation: the availability probe is bounded to 30 seconds and the collection to 5 minutes, a source failing three times in a row is skipped for 15 minutes, and each source's run and signals are recorded (`source_runs`, `source_signals`, `GET /api/v1/sources`). A source that raises or times out is recorded as `failed` and the scan continues with the others. Each native analyzer is its own source (`greenkube.rightsizing_cpu`...); together they form the v1 source `greenkube`, which is incomplete for the run when one of them fails, so none of its recommendations go stale. The native analyzers run (grouping metrics by stable target: Kubernetes owner kind/name when present, inferred Deployment from ReplicaSet-style pod names when possible, otherwise the pod name). The VPA source reads recommendation-mode VPAs, and the Karpenter source evaluates NodePool consolidation from live NodePool/NodeClaim state.
4. Every source's **signals** are combined by **consensus** into one recommendation per target, capability and type (see [Consensus across sources](#consensus-across-sources)): no source's proposal is dropped unseen, every supporting source is recorded in `sources` and in the per-source spread, and third-party or pushed signals become recommendations only through this step.
5. Each recommendation is **enriched** with a review-grade evidence block, a risk/confidence/effort assessment, a machine-readable `patch` plan and an expiry date, then scored with its risk-adjusted expected value (EV, see below). Before persisting, the scan folds each source's measured track record into the score.
6. Recommended CPU and memory requests are floored to configured minimums.
7. The scan converts recommendations to `RecommendationRecord` objects, upserts active records, reconciles missing active records as stale (per source, see below) and records the run outcome.

The controller is the only process that runs and persists scans; the API reads persisted records and queues runs. There is no CLI reporting command.

## Sources, Evidence And Ranking

**Sources.** Each recommendation carries `source` (the source whose proposal the consensus chose: `greenkube`, `vpa`, `karpenter`, or the id of a signal-only source such as `krr` or `push:<client>`), an optional `source_ref` (e.g. `namespace/vpa-name`), a `sources` list of every supporting source (the chosen one first), a `consensus` block and a `capability`. The VPA and Karpenter connectors are enabled by default (`RECOMMENDATION_VPA_ENABLED`, `RECOMMENDATION_KARPENTER_ENABLED`) and skip cleanly when their CRDs are absent; source precedence is controlled by `RECOMMENDATION_SOURCE_PRIORITY` (default `vpa,karpenter,greenkube`): it decides the `priority` consensus policy and breaks ties under the others.

### Consensus across sources

Several sources can propose a change to the same target: VPA, KRR and GreenKube's own analyzer may each size the CPU request of one container. The engine groups their signals by target, capability and finding kind (a signal proposing CPU and memory votes in both groups) and the consensus engine (`greenkube.signals.consensus`) picks one value per group with the policy `RECOMMENDATION_CONSENSUS_POLICY`:

| Policy | Rule |
|---|---|
| `safest` (default) | The least aggressive value: the highest proposed request, for decreases and increases alike. |
| `median` | The median per resource (the upper median for an even count, so the value is one a source proposed). |
| `weighted` (alias `trust-weighted`) | The upper median weighted by each source's measured realization rate (the Beta mean of its [track record](#sources-evidence-and-ranking)); without a track record every source weighs the same. |
| `priority` (alias `source-priority`) | The highest `RECOMMENDATION_SOURCE_PRIORITY` source wins: the behavior before consensus. |

Proposals that are not numeric (schedules, deletions, HPA settings) are compared by what they advise: the proposal supported by most independent sources wins, ties go to the higher-priority source.

**Engine families.** Sources that run the same engine vote once. Goldilocks runs the VPA recommender, so Goldilocks and VPA on the same VPA object are one vote (`engine_family` `vpa`: the signal's own `engine_family`, else `algorithm.name = "vpa-recommender"`, the source id `goldilocks`, or an explicit `algorithm.params.engine_family`). Both stay listed as supporting sources. The VPA connector emits one signal per VPA object: a Goldilocks VPA and a user VPA that disagree produce no v1 VPA recommendation, but both signals still vote (one vote, the family's least aggressive value), so their advice reaches the consensus.

**Pushed signals.** Each push client (`push:<client>`) counts as at most one vote per group, whatever engine families its signals declare. A pushed signal that declares another source's engine family (a client relaying VPA advice) joins that family without adding a vote and never replaces that source's own value. Pushed signals alone never produce a deletion: a pushed deletion only supports a deletion another source proposes on the same target.

**Provenance.** `consensus.source_ids` lists every source id that voted on the group, the chosen one first (`goldilocks`, `krr`, `kubecost`, `push:ci`...); the UI shows them as badges. `sources` keeps the reconciliation units (`vpa` for Goldilocks votes), which decide staleness (E8).

**Consensus block.** Each recommendation stores `consensus`: `policy`, the `resource` and `value` chosen, `chosen_from` (the sources proposing that value), `agreement` = 1 − (max − min) / max over the independent proposals (the share of independent support for non-numeric proposals), `contested`, its `reasons`, `spread` (per resource, `{source_id: value}`), `independent_votes`, `source_ids` and every vote with its signal id and whether it was counted. Example with VPA 300m, KRR 250m and GreenKube 280m under `safest`: the value is 300m and the agreement 0.83 ("vpa 300m · krr 250m · greenkube 280m → 300m"). The block is also copied into the evidence (`evidence.consensus`).

- **Agreement ≥ 0.8** between two or more independent sources raises the confidence by 10% (capped at 1), recorded as the `consensus_agreement` confidence factor.
- **Contested** (agreement below 0.5, or sources proposing opposite directions) adds the eligibility warning `consensus_contested`, which names the disagreement: a contested recommendation is never approved automatically.

**Projections.** When the chosen value is the chosen source's own proposal, its projection is kept. When the consensus restates a value on another source's recommendation (for example KRR's value on GreenKube's record), the change is projected again with the same Impact Model; a source's own savings claim is never copied, and a value that cannot be projected keeps no projection.

**Signal-only sources.** A third-party connector or a pushed signal has no v1 recommendation of its own: it becomes a recommendation only through consensus, under its own source id, and goes through the same enrichment (evidence, risk, eligibility) as any other recommendation.

**No empty changes.** A request recommendation whose consolidated value equals the current request is not emitted. Under `safest`, a source that reports the current value for a container (KRR reports a value for every container) therefore means "keep it": there is no recommendation, and reconciliation handles an existing record as usual. The same applies when the minimum floor (`RECOMMENDATION_MIN_CPU_MILLICORES`, `RECOMMENDATION_MIN_MEMORY_BYTES`) lands on the current request.

### Identity and revisions

Every recommendation has a cluster-qualified identity, `fingerprint_v4` = SHA-256 of the cluster (`CLUSTER_NAME`), the target key (`cluster/group.Kind/namespace/name[/container]`), the capability and the finding kind. The finding kind is part of the identity because CPU and memory requests of one container are still separate records sharing the `container_requests` capability; once one record carries both (roadmap H1) the identity reduces to the target and the capability. The fingerprint does not depend on the source or on the proposed value.

`revision` starts at 1 and increases each time the consolidated proposal changes (the proposed values, schedule or typed proposal, not the observed requests, projections or wording). The replaced proposal is kept in `recommendation_revisions` (`superseded`, with the proposal, the consensus and a v1 snapshot). The 20 most recent superseded revisions of a recommendation are kept, plus any revision a plan pins; older ones are pruned.

**Hysteresis.** A request proposal that moved by at most 5% from the stored one (same finding kind, same observed current request, same direction) is not new advice: the scan keeps the stored proposal and everything derived from it (description, reason, priority, projection, evidence, consensus) and only refreshes the rest, so the revision does not change. Usage jitter between scans (a working set that moves by a few kilobytes, a few millicores) therefore never revises a recommendation nor its pull-request action; a drift that accumulates beyond 5%, a change of the current request or an increase replacing a decrease is a new revision.

A recommendation never changes under an open plan (action status `plan_ready`, `pr_open`, `merged` or `sync_pending`, or the `pr_open` status): a scan that computes different advice stores it as the next revision with state `queued`, and the record keeps the revision the plan implements. Each persisted plan pins `(recommendation_id, revision)` (`recommendation_plan_pins`); the plan's pull-request status comment then shows "Newer advice available" with the newer revision and its values. Once the plan is closed, the next scan promotes the queued advice to the record. A scan that still sees the target refreshes the frozen record's `expires_at`, so it does not expire while its plan is open; nothing else of a frozen record is rewritten, even when its advice is unchanged. This matters for the CPU and memory recommendations of one container: they share one typed action carrying both values, and rewriting the unchanged member's copy from a scan in which its sibling's advice moved would make the copies diverge under the plan ("recommendations do not share one persisted action"). Every member of an open plan keeps the action the plan was built from. The revising and refreshing writes are conditional on no plan being open, so a plan opened between the scan's read and its write still freezes the record. `GET /api/v2/recommendations/{id}` lists the revisions and `queued_revision`.

Rows written before revisions (migration `0068`) start at revision 1; the scan fills their `target`, `proposal` and `fingerprint_v4` from their flat columns, and readers derive the same values meanwhile.

**Evidence.** Every recommendation embeds a `RecommendationEvidence` block: observation window and coverage, current vs proposed requests, CPU/memory distribution (avg, p50, p90, p95, p99, max), restart count, OOM kills (`oom_events`) and CPU throttling ratio (`cpu_throttling_ratio`) over the lookback (from the scan's usage summary; `null` when no source reported them), expected savings and method, confidence with factors, risk level with factors, rollback conditions, a `patch` action plan and `expires_at`. Retrieve the full block from `GET /api/v1/recommendations/{id}`; it is also rendered in the dashboard's evidence panel.

**Risk and confidence.** `risk_level` (low/medium/high) is derived from the observed headroom, restart/OOM history and blast radius; `confidence` (0–1) aggregates sample count, coverage and source authority. Both are explainable via `risk_factors` and the confidence factors inside the evidence block.

Risk model v2 (roadmap I4) adds the workload facts the pooled usage distribution hides. Each one is listed in `risk_factors`; a fact that was not observed never lowers or raises the risk:

| Factor | Effect |
|---|---|
| `replica_p95_exceeds_proposal` / `replica_p95_close_to_proposal` | The hottest replica's p95 usage is above, or within 10% of, the proposed per-container request: high / at least medium. |
| `singleton_workload` | One replica and the change restarts pods: one level higher. |
| `oom_history_unknown` | The OOM history of a memory decrease could not be read (no usage summary for the container, or the OOM signal failed or is unavailable and the rollup holds no restart history): at least medium, and the recommendation cannot become a pull request. A second factor names the cause: `oom_unknown_no_usage_summary`, `oom_unknown_signal_failed` or `oom_unknown_signal_unavailable`; the eligibility blocker is worded after it. |
| `oom_history`, `cpu_throttling_history` | OOM kills make a memory decrease high; CPU throttling above 5% (25%) makes a CPU decrease at least medium (high). On a reliability increase they (and `restart_history`) are the incident the change avoids: reported, without raising the risk, and they raise the incident probability the reliability lane counts. |
| `hpa_scales_on_resource`, `hpa_present` | An HPA scales on the changed resource: at least medium. An HPA with unknown metrics is only reported. |
| `vpa_conflicting_writer` | A VPA in `Auto`, `Recreate`, `InPlace` or `InPlaceOrRecreate` mode also writes the requests: high. |
| `pdb_blocks_disruption` | A PodDisruptionBudget blocks the rollout: at least medium. |
| `jvm_heap_not_observable`, `jvm_startup_cpu` | The workload is flagged as a JVM (`greenkube.io/jvm: "true"` or `greenkube.io/runtime: jvm`, label or annotation): a memory decrease is high, a CPU decrease at least medium. |
| `memory_decrease_best_effort`, `static_cpu_manager_incompatible` | In-place resize constraints: memory decreases apply on restart (reported); CPU resizes under the static CPU manager are high risk. |
| `criticality_critical`, `criticality_high`, `criticality_low` | The `greenkube.io/criticality` label: `critical` raises one level, `high` is at least medium. |
| `irreversible_deletion` | Every deletion: a revert cannot bring back a deleted pod, namespace, volume or external address. Zombie pods and orphaned load balancers are medium, idle namespaces and orphaned volumes (their data) high; never low. |

Scans observe replicas (pods sampled within half a scrape interval of the latest sample, so scrape jitter does not hide replicas and a pod replaced by a rollout is not counted), per-replica usage and HPA targets. Labels, annotations, VPAs and PodDisruptionBudgets come from GitOps discovery evidence: they shape the expected value (criticality) and the eligibility of a pull request (policy gates) when a recommendation is read.

**Ranking: risk-adjusted expected value.** Every recommendation carries a `value` block (roadmap I1) in the projection currency (`impact.pricing.currency`), per year:

```text
value_annual  = expected_billable_cost_annual + carbon_price × tCO2e_expected_annual
p_success     = Beta posterior mean (confidence prior + source track record) × risk-class factor
incident_cost = risk_aversion × incident_cost × severity[risk_level] × blast_radius
effort_cost   = effort_hours[effort, delivery mode] × engineer_hour_cost × effort_weight
EV            = p_success × value_annual − (1 − p_success) × incident_cost − effort_cost
```

- `value_annual` uses the Impact Model projection (`impact`); rows without one use `potential_savings_*`. Carbon is valued only when the model covers it (`co2e_modeled`).
- `p_success` treats the analyzer's confidence as a prior worth four observations and updates it with the measured outcomes of the same source family and capability (see the track record below), then multiplies by 1.0, 0.95 or 0.85 for a low, medium or high risk.
- `incident_cost` is `RECOMMENDATION_INCIDENT_COST` scaled by severity (0.05, 0.25, 1.0 by risk level) and blast radius: ×3 for node or cluster scope, ×2 for a namespace, ×4 / ×2 / ×0.5 for a `critical` / `high` / `low` criticality label, ×1.5 for a single replica.
- `effort_cost` uses 0.5, 2 or 8 engineer hours by effort level, halved when GreenKube opens the pull request; `RECOMMENDATION_ENGINEER_HOUR_COST` defaults to 0 (effort ignored).
- Reliability recommendations (`value_annual < 0`: the change costs money) are in the `reliability` lane and also count the incident cost they avoid (`incident_cost_avoided`).

`ranking_score` is the EV and `ranking_factors` break it down in currency (`cost`, `carbon`, `incident_risk`, `effort`, and `reliability` when present); the factors sum to the score. Scores are absolute, so filtering a list never reorders what remains (BUG-23: the former score normalized each batch). `value.components` keeps every input (carbon price, prior, track record, blast radius, delivery mode) so the score can be reproduced.

**Profiles** are presets over the EV parameters. Select the default with `RECOMMENDATION_RANKING_PROFILE` and override it per request with `?profile=`; `?carbon_price=` overrides the carbon price of any profile.

| Profile | Carbon price | Risk aversion | Effort |
|---|---|---|---|
| `balanced` | `CARBON_PRICE_PER_TONNE` | 1 | weight 1 |
| `cost_first` | 0 | 1 | weight 1 |
| `carbon_first` | 4 × `CARBON_PRICE_PER_TONNE` | 1 | weight 1 |
| `low_risk` | `CARBON_PRICE_PER_TONNE` | 4 | weight 1 |
| `quick_wins` | `CARBON_PRICE_PER_TONNE` | 1 | weight 3, at least 100 per engineer hour |

**Source track record.** `GET /api/v1/sources/track-record` returns a Beta(α, β) distribution per source family and capability, with a uniform prior: a `realized` outcome adds one to α, `not_realized` or `rollback_review` adds one to β, and `inconclusive` outcomes are only counted (for example `vpa: 14/16 realized`). Outcomes are the decided savings measurements of change measurement, each recommendation counted once by its latest measurement (`method: measurement_outcomes`; see [savings measurement](methodology/savings-measurement.md)); a recommendation store without measurements falls back to the verification state of applied recommendations (`method: recommendation_verification_status`). Goldilocks counts as `vpa`, since it runs the VPA recommender.

**Eligibility.** Every recommendation read through the API carries an `eligibility` block: `can_open_pr`, `delivery_modes` (`direct`, `manual`), `blockers` and `warnings` with a `code`, a `message` and a `fix`, and `eligible_at` while evidence is warming up. It is computed when the recommendation is read, with the same policy evaluator that guards plan creation. The codes are listed in [Eligibility codes](reference/eligibility-codes.md).


## Recommendation Types

GreenKube currently has eleven recommendation types.

| Type | Scope | Current trigger |
|---|---|---|
| `ZOMBIE_POD` | pod or workload | Target has cost above `ZOMBIE_COST_THRESHOLD` and energy below `ZOMBIE_ENERGY_THRESHOLD`, observed over at least `DELETION_MIN_UNUSED_DAYS`. Never emitted for DaemonSets, operators, controllers or webhooks (idle by design), infrastructure namespaces or opted-out namespaces. Worded "appears idle" with the evidence. Projected as a workload deletion: the observed allocated cost and the allocated CO2e share (idle and embodied by reservation, dynamic by usage), annualized from the observed window. |
| `RIGHTSIZING_CPU` | pod or workload | Two-way (see [Two-way rightsizing](#two-way-rightsizing-and-limits)). **Decrease:** average CPU usage divided by the latest CPU request is below `RIGHTSIZING_CPU_THRESHOLD` and at least `RECOMMENDATION_MIN_DECREASE_COVERAGE_DAYS` (7) days are observed. The target request is based on P95 usage, observed max, average usage, and `RIGHTSIZING_HEADROOM`, then floored by `RECOMMENDATION_MIN_CPU_MILLICORES`. **Increase:** p99 usage is above `RIGHTSIZING_INCREASE_THRESHOLD` (0.9) of the request; the target is p99 × headroom, capped at `RIGHTSIZING_MAX_INCREASE_FACTOR` (2) × the request per step (see [How GreenKube sizes increases](#how-greenkube-sizes-increases)). Savings price only the released CPU reservation, at the container's observed OpenCost cost per reserved core when available, otherwise at the node's CPU rate; CO2e is the idle and embodied share released ([Impact Model](methodology/impact-model.md#container-requests-rightsizing-native-analyzers-and-vpa)). |
| `RIGHTSIZING_MEMORY` | pod or workload | Two-way. **Decrease:** average memory usage divided by the latest memory request is below `RIGHTSIZING_MEMORY_THRESHOLD`, with the same coverage rule; the target uses the same balanced sizing formula, rounded up to a MiB and floored by `RECOMMENDATION_MIN_MEMORY_BYTES`; a container OOM-killed or restarted in the lookback gets no decrease, nor one whose restart/OOM history is unknown (see Reliability signals). **Increase:** p99 above 90% of the request, OOM kills, or a working set above 90% of the memory limit; the target is p99 × headroom, raised to max × 1.1 only with OOM kills or limit pressure, and with OOM kills to at least the memory limit (or request) × `RIGHTSIZING_MAX_INCREASE_FACTOR` when the working set reached it, or × 1.5 after a short spike, within the cap, rounded up to a MiB and capped at `RIGHTSIZING_MAX_INCREASE_FACTOR` × the request unless OOM kills back it (see [How GreenKube sizes increases](#how-greenkube-sizes-increases)). Savings price only the released memory reservation, at the container's observed cost per reserved GiB when available, otherwise at the node's RAM rate; projected CO2e is the memory power released (`POWER_MEMORY_W_PER_GIB` per GiB reserved, Impact Model 1.1.0). CPU and memory savings on one container never add up to more than its observed cost. |
| `AUTOSCALING_CANDIDATE` | pod or workload | CPU usage has enough samples, coefficient of variation is above `AUTOSCALING_CV_THRESHOLD`, max/mean spike ratio is above `AUTOSCALING_SPIKE_RATIO`, and no matching HPA was found for a non-pod owner target. |
| `OFF_PEAK_SCALING` | pod or workload | Timestamped CPU usage shows at least `OFF_PEAK_MIN_IDLE_HOURS` consecutive hours below `OFF_PEAK_IDLE_THRESHOLD` of the daily peak. The recommendation includes a suggested UTC scale-to-zero window. Savings are the replica-hours removed in that window, priced at the workload's allocated rate and multiplied by the realization factor. Never proposed for DaemonSets, bare pods, Jobs and CronJobs, HPA-managed workloads, operators and controllers, infrastructure namespaces or opted-out namespaces. |
| `IDLE_NAMESPACE` | namespace | Namespace total energy is below `IDLE_NAMESPACE_ENERGY_THRESHOLD` while cost is positive, over at least `DELETION_MIN_UNUSED_DAYS` of observations. Requires the namespace metadata (fail closed when namespaces cannot be read). Never emitted for infrastructure namespaces (whatever `RECOMMEND_SYSTEM_NAMESPACES` says), `default`, namespaces younger than `DELETION_MIN_UNUSED_DAYS`, terminating or opted-out namespaces, or namespaces hosting an operator, controller, webhook or DaemonSet. A GitOps-managed namespace says to remove it from Git. Projected as deleting every workload of the namespace. |
| `CARBON_AWARE_SCHEDULING` | pod or workload | Target average grid intensity is more than `CARBON_AWARE_THRESHOLD` times the average for its electricity zone. Projected CO2e savings are estimated from the high-carbon share. |
| `OVERPROVISIONED_NODE` | node | Node average CPU utilization, and memory utilization when capacity is available, are below `NODE_UTILIZATION_THRESHOLD`. |
| `UNDERUTILIZED_NODE` | node | No longer emitted (E9): it duplicated `OVERPROVISIONED_NODE` with weaker logic. The value stays for stored recommendations. |
| `ORPHANED_PERSISTENT_VOLUME` | cluster | A PersistentVolume appears unused: `Released` or `Failed`, its claim gone, and nothing depends on it (see [Why GreenKube thinks a resource is unused](#why-greenkube-thinks-a-resource-is-unused)), continuously for `DELETION_MIN_UNUSED_DAYS`. The reason lists the evidence; with `reclaimPolicy: Retain` it also says that deleting the PersistentVolume object does not delete the disk. The PV name is stored in the `pod_name` field because PVs are cluster-scoped. Projected cost savings prefer the real storage cost reported by OpenCost for the volume (annualized from the observation window); when OpenCost has no cost data, the estimate falls back to the provisioned capacity and `STORAGE_COST_PER_GIB_MONTH` (default `$0.10`/GiB-month). Projected as an asset deletion (realization factor 1). CO2e savings are not projected because the energy model does not cover storage. |
| `ORPHANED_LOAD_BALANCER` | cluster | A Service of type `LoadBalancer` appears unused (see [Why GreenKube thinks a resource is unused](#why-greenkube-thinks-a-resource-is-unused)) continuously for `DELETION_MIN_UNUSED_DAYS`. The description says "appears unused" and the reason lists the evidence. The Service name is stored in the `pod_name` field and its namespace in the `namespace` field. Projected cost savings prefer the real LoadBalancer cost reported by OpenCost for the Service; when OpenCost has no cost data, the estimate falls back to `LOAD_BALANCER_COST_PER_MONTH` (default `$18.00`/month). An address handed out by a bare-metal allocator (MetalLB, Cilium LB-IPAM or kube-vip, recognized from the `loadBalancerClass`, the allocator's labels and annotations such as `metallb.io/ip-allocated-from-pool`, or Cilium's `IPAMRequestSatisfied` condition) has no cloud bill: without an OpenCost cost the recommendation projects no cost saving (`cost_source` `none`, detail `bare_metal_load_balancer:<allocator>`) and its description says why. Projected as an asset deletion (realization factor 1). CO2e savings are not projected because the energy model does not cover load balancers. |

### Why GreenKube thinks a resource is unused

A deletion cannot be undone, so GreenKube recommends one only with evidence that the resource is unused, never on a single missing signal, and a person always decides: zombie workloads and idle namespaces (`ZOMBIE_POD`, `IDLE_NAMESPACE`) are blocked from pull requests with the eligibility code `deletion_requires_review` ([eligibility codes](reference/eligibility-codes.md)), and orphaned PersistentVolumes and LoadBalancers can become a deletion pull request that is never approved automatically ([Deleting orphaned resources](automation.md#deleting-orphaned-resources)). Outside GitOps, the recommendation detail page gives the `kubectl delete` steps ([manual mode](automation.md#manual-mode)). The description says the resource *appears* unused and the reason lists the evidence and the unused duration. When GreenKube cannot prove a fact (a missing permission, a timeout), it recommends nothing (fail closed).

**Opt-out.** Annotate a Service, PersistentVolume or namespace with `greenkube.io/ignore: "true"` (or `greenkube.io/keep`, any value except `false`) and GreenKube never proposes to delete or stop it. A namespace annotation covers the zombie, idle-namespace and off-peak recommendations of its workloads.

**Persistence.** A LoadBalancer Service or PersistentVolume must look unused continuously for `DELETION_MIN_UNUSED_DAYS` (default 7, Helm `config.recommendations.deletionMinUnusedDays`). Each scan records the candidates in the `deletion_candidates` table (migration 0063) with the time they were first seen; the clock restarts when the resource is in use again, is re-created (new uid), or was not observed for more than 48 hours. A scan that could not evaluate the resources (Services or PersistentVolumes unreadable) records nothing. Without a SQL database (custom backends, the CLI `--live` path) the duration cannot be proven and nothing is recommended. Zombie workloads and idle namespaces need idle observations spanning `DELETION_MIN_UNUSED_DAYS`.

**LoadBalancer Services.** An empty legacy `Endpoints` object is not evidence: Gateway API implementations (Cilium, Istio, Envoy Gateway, Contour, Kong...) route traffic to selector-less Services, and dual-stack or large Services publish their backends in EndpointSlices only. A Service is reported only when all of the following hold:

1. it is not opted out, has a selector, has no `ownerReferences` (a Gateway, an operator or a mesh manages it), and carries no label or annotation of a gateway, ingress or mesh controller (`gateway.networking.k8s.io/*`, `io.cilium.gateway/*`, `istio.io/*`, `istio`, `gateway.envoyproxy.io/*`, `projectcontour.io/*`, `konghq.com/*`, `traefik.io/*`, `linkerd.io/*`...);
2. its `Endpoints` object and its `discovery.k8s.io/v1` EndpointSlices hold no endpoint, ready or not;
3. its selector matches no pod, and no Deployment, StatefulSet, DaemonSet or CronJob pod template (a workload scaled to zero comes back);
4. no Gateway lists its external address, no Ingress publishes its address or routes to it, and no Gateway API route (`HTTPRoute`, `GRPCRoute`, `TLSRoute`, `TCPRoute`, `UDPRoute`) references it in `backendRefs` or `parentRefs`. An API the cluster does not serve (HTTP 404, for example no Gateway API CRDs) counts as "no reference"; an API GreenKube cannot read (HTTP 403, timeout) blocks the recommendation.

**PersistentVolumes.** Only `Released` or `Failed` volumes whose `claimRef` names a PVC that no longer exists are candidates; `Available` volumes (static, or pre-bound to a claim not created yet) and `Bound` volumes never are. A candidate is dropped when it is a `local` or `hostPath` volume (no billable storage, and deleting the object does not free the disk), when its claim name matches a `volumeClaimTemplate` of an existing StatefulSet (a replica scaled down, data expected back), when a VolumeSnapshot was taken from its claim or a VolumeSnapshotContent points at its volume handle, or when a PVC with the same name exists again. With `reclaimPolicy: Retain`, deleting the PersistentVolume object does not delete the cloud disk: the recommendation says so.

**Zombie workloads, idle namespaces and off-peak scale-to-zero.** These never target infrastructure namespaces (`kube-*`, `*-system`, `openshift-*`, `istio-*`, monitoring, cert-manager, Argo CD, Flux, ingress controllers, storage, GreenKube...), whatever `RECOMMEND_SYSTEM_NAMESPACES` says, nor operators, controllers and webhooks (event-driven, idle by design) or DaemonSets. Idle namespaces additionally require the namespace metadata (opt-out, phase, age), skip `default` and namespaces younger than the minimum period, and say that GreenKube does not see custom resources, Secrets or external consumers; a GitOps-managed namespace says to remove it from Git. Off-peak schedules skip workloads that cannot be scaled (bare pods, Jobs, CronJobs) and HPA-managed workloads.

**Upgrade.** Migration 0063 retires the active `ORPHANED_LOAD_BALANCER` and `ORPHANED_PERSISTENT_VOLUME` recommendations produced by the previous rules: they become `stale` with a `retired` event (`code: deletion_evidence_required`) in their history. The ones the evidence confirms come back after the minimum unused period. Zombie and idle-namespace recommendations the new rules no longer emit become `stale` on the next scan through the normal reconciliation.

### Two-Way Rightsizing And Limits

Native rightsizing proposes **decreases** (efficiency) and **increases** (reliability) from the per-container usage summary:

- A decrease needs `RECOMMENDATION_MIN_DECREASE_COVERAGE_DAYS` (default 7) of observed coverage, so a weekly cycle is seen; a workload with less history (or unknown coverage) gets no decrease.
- A container that was OOM-killed or restarted within the lookback (`oom_kill_count`, `restarts_in_window`) never gets a CPU or memory decrease: its peaks may be shorter than the scrape interval. Unknown signals are never read as zero (see Reliability signals: `reliability_signals_unknown`).
- Memory proposals are whole mebibytes (decreases are rounded up), so a pull request writes `23Mi`, never a byte count such as `24114500`.
- An increase needs no minimum coverage. Triggers: p99 usage above `RIGHTSIZING_INCREASE_THRESHOLD` × request (CPU and memory), any OOM kill, or a memory working set above 90% of the memory limit. Memory increases, and CPU increases whose p99 is above the request, have priority `high`; their reason starts with "Reliability".
- An increase is projected by the Impact Model like any request change: it is a **negative** cost delta, kept as such in `potential_savings_cost` and `impact`. The expected-value ranking puts it in the `reliability` lane, which also counts the incident cost it avoids. Since container resources v2 (roadmap H1) an increase gets a GitOps action like a decrease: its pull request carries the `reliability` label and shows the projected cost, and it is never auto-approved unless `automation.autoApprove.increases` is set (see [Automation: container resources v2](automation.md#container-resources-v2-increases-limits-sidecars-h1)).
- Percentiles come from per-step values (see [Usage summaries](#usage-summaries-memory-bounded-scans)), so a short burst raises p95/p99 even when hourly averages hide it.
- Usage known only for the whole pod (no container series) never yields an increase: the analyzer skips it with the reason `pod_level_usage_only` (logged, and listed in the analyzer's `skipped`), because the pod total cannot say which container needs more. A pod-level decrease is still proposed, as advisory: its reason says "Pod-level usage only" and the values are the pod's total request and usage, to split between containers by their requests.

#### How GreenKube sizes increases

An increase must be defensible on its own: it costs money every hour and is reviewed by a human.

1. **The statistic is per container and per replica.** The p99 is taken over one point per replica and step, so it describes what a single replica uses, never a sum across replicas, pods or duplicate series (see [Usage summaries](#usage-summaries-memory-bounded-scans)).
2. **Startup is not steady state.** The p99 excludes the warm-up after each container (re)start (`RIGHTSIZING_WARMUP_MINUTES`, default 10).
3. **The target is p99 × `RIGHTSIZING_HEADROOM`** (default 1.2). The observed maximum (× 1.1) raises a memory target only when OOM kills or a working set above 90% of the limit show that peaks matter; a single spike otherwise does not size the request.
   An OOM kill also proves the container passed its memory limit (its request when it has no limit), even when the spike was shorter than the 30-second scrape and the observed maximum stayed far below it. The kill says nothing about how far above the limit the need is, so the OOM step is decisive rather than a small creep (live e2e: a 1.1× step took a request = limit = 32Mi container through 36Mi, 40Mi, ..., one pull request, merge and measurement window per step). The target is at least that limit × a step factor (basis `oom_limit`):

   - **killed at the limit** (observed maximum at least 90% of the limit, the same threshold as limit pressure): × `RIGHTSIZING_MAX_INCREASE_FACTOR` (default 2), the largest step one change may take; the container's true need is unknown and above the limit;
   - **short spike** (observed maximum below 90% of the limit): × 1.5, never above `RIGHTSIZING_MAX_INCREASE_FACTOR`. The samples show the working set usually well below the limit, so a full doubling is not warranted, but 1.1× crept; 1.5× is half way to the cap, and two such steps cover a spike of 2.25× the limit. The observed maximum × 1.1 still applies when it is higher.

   The OOM count does not scale the step: it tells how often the spike happens, not how far above the limit it goes. The step is bounded by `RIGHTSIZING_MAX_INCREASE_FACTOR` × the current request, a request is never lowered, and the memory limit is raised above the new request with headroom (see [OOM kills and the memory limit](#oom-kills-and-the-memory-limit)). Examples: a container requesting and limited to 32 MiB with its working set at the limit, OOM-killed, gets a 32 MiB → 64 MiB request and a 32 MiB → 71 MiB limit; requesting and limited to 64 MiB, OOM-killed twice with an observed maximum of 8 MiB (short spike), it gets a 64 MiB → 96 MiB request and a 64 MiB → 106 MiB limit; with a 1 GiB limit and a 100 MiB request it gets 100 MiB → 200 MiB (2× cap), and the next scan re-evaluates.
4. **The pod bounds its containers.** A container's statistics are compared with its target's pod-level summary. When the container's p99 or maximum exceeds the pod's by more than 5%, the container data contradicts the cgroup that contains it, and the increase is sized from the pod's statistics instead; the reason says so (`container_usage_exceeds_pod`). A container whose bounded usage no longer calls for an increase gets no recommendation at all, not a decrease built on contradicted data.
5. **One step is capped** at `RIGHTSIZING_MAX_INCREASE_FACTOR` (default 2) × the current request, unless OOM kills back the increase. The reason states the cap and the uncapped target ("Capped at 2x the current request (usage suggests 1320MiB)"); the next scan re-evaluates once the step is applied.
6. **The reason says which rule sized it**: "Sized from the per-replica p99 (622MiB, container warm-up excluded) x headroom 1.2", "Sized from the pod's p99 (634MiB, ...) ... (container_usage_exceeds_pod)", "Sized from the observed maximum ... because OOM kills or limit pressure show that peaks matter", "Sized from the memory limit (32MiB) the OOM kills prove was passed x 2, at most 2x the current request: the working set reached it (observed maximum 32MiB), so the need above it is unknown: one decisive step instead of small ones", or "Sized from the memory limit (64MiB) the OOM kills prove was passed x 1.5, at most 2x the current request: a short spike the samples missed (observed maximum 8MiB)".

Example (the reported case: Minikube, `monitoring/prometheus-k8s`, 2 replicas, container `prometheus` requesting 400 MiB, each replica's pod cgroup working set 550–645 MiB). The Metrics API reported the container at 650–960 MiB over the morning, because its figure includes page cache; GreenKube then recommended 400 MiB → 1287 MiB and 1214 MiB, one recommendation per replica, sized from max(p99 × 1.2, max × 1.1) of those samples. Replaying the recorded data, GreenKube now recommends one change for the StatefulSet, 400 MiB → 747 MiB (the refitted p99 622 MiB × 1.2), and the 2× cap plays no part. Duplicate cAdvisor series of one container (kubelet `/metrics/resource` next to `/metrics/cadvisor`) are merged with `max` as well: summed, they would double the usage the same way.

**Reliability signals.** Each scan fills the summaries with what the rollup does not hold (best effort; a signal that cannot be read stays unknown, never "healthy"):

- `oom_kill_count`: restarts over the lookback whose termination reason, at the 5-minute step of the restart, is `OOMKilled` (kube-state-metrics; a liveness-probe restart after an older OOM kill does not count), or cAdvisor `container_oom_events_total` when larger; 0 when kube-state-metrics is scraped and reports none.
- `restarts_in_window`: container restarts over the lookback, whatever their cause (`increase(kube_pod_container_status_restarts_total)`, kube-state-metrics); 0 when kube-state-metrics is scraped and reports none. Any restart blocks a CPU or memory decrease.

Each signal query runs on its own: a failed query (HTTP error, timeout, Prometheus sample limit) leaves only its own signal unknown, and a signal is a known zero only when kube-state-metrics is scraped and that signal's query answered. **Decreases fail closed on unknown signals:** a CPU or memory decrease needs `oom_kill_count == 0` and `restarts_in_window == 0`. When either is unknown, the scan falls back on the rollup's restart history (`restart_increase`). The rollup stores, for each hour, the restarts of every pod counted against that pod's own previous restart count (never the highest count across replicas, which is not a counter); a count that drops (a pod recreated under the same name), a new pod and a pod whose previous count is unknown add their whole count. Restart counts are stored per pod, so a container uses its pod's history. Hours rolled before migration 0130 carry no such value: a lookback that still holds one has an unknown history. With that history at zero the decrease is allowed (an OOM kill restarts the container, so none happened), and the evidence and risk profile record 0 OOM kills. With restarts in it, the decrease is not recommended, as with restarts reported by the signals. Without any stored restart count, the decrease is withheld with the reason `reliability_signals_unknown` ("decrease withheld: restart/OOM history could not be read"): the rightsizing source run reports a warning diagnostic naming the signals that failed, and only the records of those containers are treated specially. The previous decrease of a withheld container is retired (marked stale with the reason `reliability_signals_unknown`), and a previous memory increase of a container whose OOM kills are unknown is kept (it may have been sized from kills this scan cannot see). The source run stays complete, so every other record of the native source (zombie pods, idle namespaces, advice for deleted workloads) is reconciled as usual.
- `throttling_ratio`: throttled / total CFS periods over the lookback (cAdvisor; only containers with a CPU limit have these series).
- `jvm_hint`: from the pod specs, a JVM configuration variable (`JAVA_TOOL_OPTIONS`, `JAVA_OPTS`, `_JAVA_OPTIONS`, `JDK_JAVA_OPTIONS`, `ES_JAVA_OPTS`, ...), `-Xmx`/`-Xms`/`-XX:MaxRAMPercentage` or a `java` command, or a JVM image name (`eclipse-temurin`, `openjdk`, `elasticsearch`, `kafka`, `tomcat`, ...).

OOM kills trigger an increase sized from the observed maximum or, from the limit the kill proves was passed, × 2 when the working set reached it or × 1.5 after a short spike (bounded by the cap); they also raise the risk of a decrease (`oom_history`). A memory decrease whose OOM history is unknown (from any source, VPA included) is at least medium risk with the factor `oom_history_unknown`, which blocks pull requests (eligibility code `oom_history_unknown`). Throttling adds limit advice and raises the risk of a CPU decrease. A JVM hint marks memory decreases `jvm_heap_not_observable` (risk high), halves their confidence (`confidence_factors.jvm_suspected`) and adds "JVM runtime suspected" to the reason: the JVM sizes its heap itself, so the working set does not show what it needs.

`RECOMMENDATION_LIMITS_POLICY` sets how limits are treated; the advice is appended to the description and kept as structured `LimitAdvice` (resource, action `remove`/`raise`/`equal_to_request`, reason, current and minimum limit) on the analyzer's proposal:

| Policy | CPU limit | Memory limit |
|---|---|---|
| `keep` (default) | Left alone, unless it is below the new request ("remove it or raise it to at least max(request, p99 × 1.5)") or the workload is throttled in more than 5% of CFS periods. | Left alone, unless it is below max(new request, max × 1.1), or the increase is OOM-driven or reaches the limit: then it is raised with headroom (see [OOM kills and the memory limit](#oom-kills-and-the-memory-limit)). |
| `remove_cpu` | Remove it. | As `keep`. |
| `memory_equals_request` | As `keep`. | Set equal to the request; memory decreases then never go below max × 1.1. An OOM-driven increase, or one that reaches the limit, needs a limit above the request, which this policy forbids: it stays advisory and says so. |

**Limits in pull requests (H1).** The evidence of a rightsizing recommendation records the container's current limits (`evidence.current`) and the limit edits delivered with the request change (`evidence.limit_changes`: resource, `set` or `remove`, current and proposed value, reason), recomputed against the final proposed request:

- an **increase** carries every limit change it needs (a CPU limit below the new request or throttled is raised to max(request, p99 × 1.5), or removed under `remove_cpu`; a memory limit below max(request, max × 1.1) is raised to it; an OOM-driven memory increase, or one that reaches the limit, raises the memory limit with headroom as described below) and never lowers a limit: under `memory_equals_request` it raises the memory limit to at least max × 1.1;
- a **decrease** carries only the policy's own changes (`remove_cpu`, `memory_equals_request`); with `keep`, decreases never touch limits;
- a set limit is never below the proposed request, and a memory limit is never removed.

A request increase above a known limit without a limit change gets no action (it could not apply), and planning rejects any rendered `request > limit`.

#### OOM kills and the memory limit

The kernel OOM-kills a container when its working set reaches its memory **limit**; the request only reserves capacity for scheduling. A request increase that leaves the limit where it was, or sets it equal to the new request, cannot stop OOM kills (live e2e: request = limit = 24Mi, raised to a 48Mi request with no headroom; the kills went on and the change went to `rollback_review`). So when a memory increase is OOM-driven (`oom_kills`: OOM restarts from kube-state-metrics or cAdvisor OOM events in the lookback), or whenever the new request reaches or exceeds the current memory limit, the limit is raised too (limit change reason `oom_headroom` or `request_reaches_limit`):

```
new_limit = max(current_limit × new_request / current_request,   # the request's factor, which keeps the limit/request ratio
                110% of the observed maximum working set)
new_limit = min(new_limit, RIGHTSIZING_MAX_INCREASE_FACTOR × current_limit)
new_limit = max(new_limit, new_request × 1.1, current_limit)       # 10% headroom above the request, never lower
```

rounded up to a whole MiB. The bound applies to the scaled limit; the 10% headroom above the new request always applies, because an OOM-backed request step is itself uncapped. Examples: request = limit = 24Mi raised to 48Mi gets a 53Mi limit; a 100Mi request with a 200Mi limit raised to 120Mi gets a 240Mi limit, and raised to 250Mi gets 400Mi (2 × 200Mi). A non-OOM increase that stays below the limit keeps the plain rule above (raised only below max(request, max × 1.1)). A limit is never lowered and a memory limit is never removed; a container without a memory limit keeps none.

When the limit cannot be raised, the recommendation says so and stays advisory (no action, no pull request): under `memory_equals_request` the limit must equal the request, so the advice (reason `oom_headroom_forbidden`) names the limit to set by hand. The action builder also refuses any OOM-driven memory increase (`evidence.oom_events` > 0), or one whose request reaches the known limit, that does not raise the limit above the new request. Scans also record where a container lives (`evidence.container_kind`: `container`, `sidecar` for a native sidecar, `init`), so native sidecars are edited in `initContainers`.

Projected savings come from the [Impact Model](methodology/impact-model.md): each projected recommendation carries an `impact` object (model version, cost and CO2e breakdown, capacity released, realization factor, unit prices and their source). `potential_savings_cost` and `potential_savings_co2e_grams` are its expected annual values. Karpenter NodePool recommendations project the removable nodes' price and their idle and embodied emissions; the nodes kept must fit both the CPU and the memory requests. Autoscaling, carbon-aware and node-level native recommendations do not use the model yet.

Not every recommendation type has projected savings today. The top recommendations API and Grafana actionable cards only rank active recommendations with a positive projected value for the selected metric.

## Persistence And Reconciliation

Recommendations are stored in the `recommendation_history` table. SQLite and PostgreSQL implement the same repository contract.

The active identity used by the repositories is:

```text
scope + namespace + pod_name + target_node + type
```

This identity allows pod, workload, namespace, and node recommendations to coexist without collapsing unrelated targets. Active records with the same identity are refreshed in place with the latest description, reason, priority, projected savings, current requests, recommended requests, schedule, and node target.

Ignored records are left untouched by normal upsert so a user decision is not overwritten by the next scan. A `pr_open` record is frozen: a scan that regenerates it neither rewrites it nor inserts a duplicate, so the pull request, its baseline and its proposal stay consistent. If a generated recommendation matches a previously applied record, only the applied record's `updated_at` is refreshed (see [Realized savings](#realized-savings)). A rightsizing regenerated within six hours of a change that is `verifying`, `verified` or in `rollback_review` is dropped when it restates that change (same baseline, or a proposal equal to the recorded proposal or live value): it was built from the lagging hourly rollup, and inserting it would offer the change again. Outside that window, or with a different baseline and proposal (fresh data after the change), it is new advice and is stored as usual.

**Request fields.** `current_*_request` is the request the recommendation was made against (the baseline; it is frozen when the change is applied), `recommended_*` is the proposal and `actual_*` is the live request detected in the cluster once the change landed (or declared with `PATCH .../apply`). A scan never writes `actual_*`.

### Per-source reconciliation

At the end of each scan, reconciliation compares the latest generated identities with currently active records. An active record missing from the generated set becomes `stale` **only when every source that supported it** (its `sources` provenance, or its `source` for older rows) completed the scan. A source that failed, timed out, was unavailable or reported an incomplete result never causes staleness and never blocks persistence of the other sources: its previous recommendations stay active as "last known" until it completes again or their TTL expires. Recommendations from a source that is no longer configured reconcile normally.

When a source that supported an active recommendation (for example KRR) did not complete and is missing from the new consensus, its last known vote (kept in the record's `consensus.votes`) still counts, so the consensus does not flap during a transient connector outage, but it never holds back safer advice. A higher request is always the safer one (a smaller decrease, or an increase that protects reliability):

- when the new proposal of the remaining sources is at least the last known one (for example native rightsizing now asks for 400m, or for an increase, where the record said 280m), the new proposal is written;
- under `safest`, when every incomplete supporter's last known value is at most the new proposal, `safest` of the new and last known votes is the new proposal, which is written;
- otherwise (the new proposal is more aggressive than what the last known votes support, or the proposal is not numeric) the last known record stays until the source completes again or the record expires.

A record written this way keeps the incomplete sources in `sources` and their last known values in `consensus.last_known` (`{source_id: value}`).

A source that stays unavailable is treated the same way. For example, if the VPA CRD is removed while `RECOMMENDATION_VPA_ENABLED=true`, the VPA recommendations stay active, and their last known VPA votes keep counting in the consensus for their targets (a more aggressive native value does not replace them), until `RECOMMENDATION_TTL_DAYS` expires them. Disable the source when you remove its backend so its recommendations reconcile on the next scan. The run's `sources` list shows the source as `unavailable` meanwhile.

Native rightsizing never marks its source incomplete when it cannot read a container's restart/OOM history: it reconciles that container's records alone (its previous decrease is retired with the reason `reliability_signals_unknown`, its previous memory increase is kept while its OOM kills are unknown) and every other record as usual (see Reliability signals).

Connectors that degrade gracefully (return a partial list instead of raising) report an incomplete result by setting `last_collect_complete = False` on the source instance during `collect()`. The engine resets the flag to `True` before every `collect()` call, so a connector only sets it when the current call is partial.

## API Usage

All recommendation API paths are under `/api/v1`.

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/recommendations?namespace=&profile=&carbon_price=` | Returns persisted active records (never generates them) with their live `value` and `eligibility`, ordered by expected value. |
| `GET` | `/recommendations/active?namespace=&source=&risk_level=&capability=&profile=&carbon_price=` | Returns persisted active and `pr_open` records with their live `value` and `eligibility`, ordered by expected value (`ranking_score`, highest first, unscored last), then value, projected cost savings and identity. Optional filters by source, risk level and capability; `profile` and `carbon_price` re-score the list for this request (`422` for an unknown profile or a negative price). |
| `GET` | `/recommendations/top?limit=5&metric=co2&profile=&carbon_price=&namespace=` | Returns ranked active recommendations. `metric` is `co2` or `cost` (recommendations with a positive projection of that saving) or `value` (every recommendation, by expected value); `limit` is 1 to 50. With `profile` or `carbon_price`, recommendations are re-scored and ranked by expected value. |
| `GET` | `/sources/track-record` | Returns the measured realization rate of each source family and capability as Beta(α, β) entries (`realized`, `not_realized`, `rollback_review`, `inconclusive`, `alpha`, `beta`, `mean`, `summary`). |
| `POST` | `/optimization/runs` | Queues a scan for the controller (optional body `{"namespace": "..."}`); returns `202 {"run_id", "status"}`, deduplicated while a run is queued for the same scope. |
| `GET` | `/optimization/runs?limit=20` | Lists recent runs, newest first (`limit` 1 to 100), with per-source health and diff counts. |
| `GET` | `/optimization/runs/{run_id}` | Returns one run: `status` (`queued`, `running`, `succeeded`, `failed`), `requested_at`, `started_at`, `completed_at`, `recommendation_count`, `error`, `sources`, diff counts and `diff`. |
| `GET` | `/recommendations/{id}?profile=&carbon_price=` | Returns a single record including its full evidence block, risk/confidence assessment, patch plan, expiry, live `value` and `eligibility`. |
| `GET` | `/recommendations/ignored?namespace=` | Returns ignored records. |
| `GET` | `/recommendations/applied?namespace=` | Returns applied, verifying, verified and rollback-review records ordered by most recent application. |
| `GET` | `/recommendations/history?start=&end=&type=&namespace=` | Returns records in a creation-time range, any status. |
| `GET` | `/recommendations/{id}/events` | Returns the full lifecycle audit trail (actor, payload, timestamp) for a recommendation. |
| `GET` | `/recommendations/savings?namespace=&last=` | Returns separate, never-summed blocks: `projected_annual` and `run_rate_annual` (current yearly rates of applied changes, projected vs measured), `realized_to_date` (measured ledger accruals over the `last` window, all time without it), plus `projected_to_date`, `user_declared_annual` and `user_declared_to_date`. A window starting mid-hour includes that partial hour. Answers `503` when the ledger cannot be read; it never falls back to recommendation records. The flat `total_*`, `measured_*` and `prorated_*` fields are deprecated aliases (`total_*` = realized). |
| `PATCH` | `/recommendations/{id}/apply` | Marks a recommendation as `applied`, stores actual CPU or memory values when supplied and freezes the verification baseline; only `active` or `pr_open` records can be applied (`409` otherwise). Optional `carbon_saved_co2e_grams` / `cost_saved` are *user-declared* annual savings: they must be finite and at most 10× the projection (`422` otherwise); negative values are accepted and flagged. They accrue with method `user_declared` and are never reported as measured. |
| `PATCH` | `/recommendations/{id}/ignore` | Marks an `active`, `stale` or `expired` recommendation as `ignored` and stores the optional `reason`; `409` for an open PR or an applied change. |
| `DELETE` | `/recommendations/{id}/ignore` | Restores an ignored recommendation to `active`; `409` for any other status. |
| `GET` | `/recommendations/{id}/pull-requests` | Lists the pull-request attempts and their status for a recommendation. |
| `GET` | `/automation/status` | Reports secret-free automation readiness, worker heartbeat, queue depth, discovery freshness, renderer configuration, and provider capability/preflight state. |

`refresh=true` is still accepted on `/recommendations`, `/recommendations/active` and `/recommendations/top` but is deprecated: it returns the current data with an `X-GreenKube-Run-Id` header naming a run that covers the scope (a pending run or one finished within 5 minutes is reused, a failed one for 1 minute only; otherwise a run is queued).

### GitOps plan and PR workflow

The bot resolves workload sources from Argo CD and Flux discovery, not from mandatory workload annotations:

1. GreenKube discovers the Git source through Argo CD or Flux and persists a generation-bound workload binding.
2. The plan API accepts recommendation IDs only, snapshots the authorized Git revision, and validates the source and rendered diff for raw YAML/JSON, Kustomize, and the supported Helm subsets.
3. Approval binds to the exact preview digest. A durable idempotent worker creates one commit and PR per plan; duplicate requests reuse the same plan operation.
4. After the PR merges, the controller observes the Argo CD/Flux revision (the landed commit, or a later commit that contains it and still carries the planned values) and workload readiness. Apply detection compares the post-baseline workload identity and per-container requests; verification attributes measured savings only after its health and evidence gates pass.

A pull request that would change nothing is never opened (`no_effective_change`, fail closed): the eligibility check blocks a proposal equal to the current request after unit normalization (`1Gi` = `1024Mi`, `0.5` = `500m`), plan creation fails with `409 no_effective_change` when the Git source already declares the proposal, and the worker fails the plan (reason `no_effective_change`) before any branch or commit when the live workload already runs every proposed request. Apply detection then records such a recommendation as applied (`detected`) instead of offering it again.

Example lifecycle calls:

```bash
# Queue a scan, then poll the returned run until it is terminal
curl -X POST "http://localhost:8000/api/v1/optimization/runs"
curl "http://localhost:8000/api/v1/optimization/runs/17"

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

The web dashboard fetches active recommendations and realized savings in the background so the main dashboard can render quickly. Page loads only read persisted recommendations: they never ask the server to scan (no `refresh=true`).

The `/recommendations` page currently provides:

- Active, Pull requests, Ignored, and Realized Savings tabs.
- Filters for namespace, source, risk level and capability, sent to `GET /recommendations/active` as `namespace`, `source`, `risk_level` and `capability`. The Ignored tab applies the same filters; the Pull requests tab lists every namespace because its endpoint has no namespace filter.
- Type filtering for active and ignored records.
- Potential annual CO2e and cost savings summaries for active records.
- Source badges (GreenKube, VPA, Karpenter), risk badges and confidence indicators.
- The expected value on each card ("≈ €412/yr expected · 82% success"), its top contributing factor, and the full breakdown in currency on hover. Reliability recommendations carry a **Reliability** badge.
- The eligibility of each card: an **Open PR** button when the server says a pull request can be opened (`eligibility.can_open_pr`), otherwise the blockers with their fix (for example "Collecting evidence — eligible in ~31h").
- A sort selector. **Expected value (default)** keeps the server order. **Projected cost savings** and **Projected CO₂e savings** sort the loaded list by annual savings. The **Profile** options (balanced, carbon first, cost first, quick wins, low risk) reload the list with `?profile=`, so the server re-scores and re-orders every recommendation matching the filters.
- A **Rescan** button: it requests a scan with `POST /optimization/runs`, polls `GET /optimization/runs/{id}` and reloads the page's data when the run succeeds or fails. When the server also offers the optional run list (`GET /optimization/runs?limit=1`, newest first) and the page opens while a scan is already queued or running, it follows that run and reloads when it completes; a server without the list still accepts Rescan. Only when `POST /optimization/runs` itself answers `404` or `405` does the button fall back to reloading persisted results.
- An expandable evidence panel per active recommendation (window, coverage, utilization distribution, proposed change, rollback conditions, expiry).
- An **Open PR** action on cards the server marks eligible: the server resolves the GitOps binding, renders the change, and returns a digest-addressed diff. Approval submits that exact digest; the worker creates or adopts the pull request. The UI tracks the durable operation until the provider result is available. Closing the dialog of a preview that was not approved discards it with `DELETE /automation/plans/{id}`, so the next preview of the same workload is not blocked. A plan whose approval was requested is never discarded, even if the dialog is closed while the approval is still in flight.
- Ignore from the Active tab, with an optional reason kept for review.
- Restore from the Ignored tab.
- Applied recommendation details, verification state (`applied`, `verifying`, `verified`, `rollback_review`), measured vs projected savings and an expandable lifecycle event trail. Each request change reads baseline → live value after the change (with the proposal when it differs); a recorded change whose live request still equals the baseline is flagged **live request unchanged** instead of showing identical values. When the live value after a merged change is unknown (migration `0061` cleared the values that old rescans had reverted to the baseline), the card shows baseline → proposal.
- Realized savings summary split into **measured (verified)** and **projected (prorated)** totals.

## Prometheus And Grafana

`/prometheus/metrics` refreshes recommendation gauges from the database on scrape, so API restarts do not empty dashboards. The API does not scan at startup; the controller scans once right after its initial collection.

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

Applied recommendations can later be regenerated by a scan when the same identity is observed again:

- For CPU and memory rightsizing, a regeneration that only restates the applied change (same pre-change baseline, or a proposal equal to the landed value) within 6 hours of `applied_at` changes nothing but `updated_at`: the baseline, the actual value and the realized savings stay as recorded. A scan reads requests from the hourly usage rollup, whose latest bucket can still carry the replaced request for up to an hour after a rollout, so it is not a live observation; drift after a change is measured from the cluster (C8), not from a regenerated recommendation. Any other advice for the container (for example an OOM-driven increase computed from the landed request, or advice after the 6-hour window) is inserted as a new active recommendation, as for the `verifying`, `verified` and `rollback_review` statuses.
- Orphaned volumes and load balancers keep their savings (the regenerated observation means the deletion is pending).
- For other non-resource recommendation types, seeing the same issue again sets realized savings back to zero, because the implementation no longer appears effective.

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
| `RECOMMENDATION_LOOKBACK_DAYS` | `config.recommendations.lookbackDays` | `14` |
| `RECOMMENDATION_MAX_METRIC_ROWS` | `config.recommendations.maxMetricRows` | `750000` (row-loading path only) |
| `RIGHTSIZING_CPU_THRESHOLD` | `config.recommendations.rightsizingCpuThreshold` | `0.3` |
| `RIGHTSIZING_MEMORY_THRESHOLD` | `config.recommendations.rightsizingMemoryThreshold` | `0.3` |
| `RIGHTSIZING_HEADROOM` | `config.recommendations.rightsizingHeadroom` | `1.2` |
| `RIGHTSIZING_INCREASE_THRESHOLD` | `config.recommendations.rightsizingIncreaseThreshold` | `0.9` |
| `RIGHTSIZING_MAX_INCREASE_FACTOR` | `config.recommendations.rightsizingMaxIncreaseFactor` | `2.0` |
| `RIGHTSIZING_WARMUP_MINUTES` | `config.recommendations.rightsizingWarmupMinutes` | `10` |
| `RECOMMENDATION_MIN_DECREASE_COVERAGE_DAYS` | `config.recommendations.minDecreaseCoverageDays` | `7` |
| `RECOMMENDATION_LIMITS_POLICY` | `config.recommendations.limitsPolicy` | `keep` |
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
| `DELETION_MIN_UNUSED_DAYS` | `config.recommendations.deletionMinUnusedDays` | `7` |
| `RECOMMENDATION_VPA_ENABLED` | `config.recommendations.vpaEnabled` | `true` (CRD auto-detected) |
| `RECOMMENDATION_KARPENTER_ENABLED` | `config.recommendations.karpenterEnabled` | `true` (CRD auto-detected) |
| `RECOMMENDATION_SOURCE_PRIORITY` | `config.recommendations.sourcePriority` | `vpa,karpenter,greenkube` |
| `RECOMMENDATION_CONSENSUS_POLICY` | `config.recommendations.consensusPolicy` | `safest` (`median`, `weighted`, `priority`) |
| `RECOMMENDATION_TTL_DAYS` | `config.recommendations.ttlDays` | `14` |
| `RECOMMENDATION_RANKING_PROFILE` | `config.recommendations.rankingProfile` | `balanced` |
| `RECOMMENDATION_RANKING_WEIGHTS` | `config.recommendations.rankingWeights` | `""` (deprecated, ignored) |
| `RECOMMENDATION_INCIDENT_COST` | `config.recommendations.incidentCost` | `100` |
| `RECOMMENDATION_ENGINEER_HOUR_COST` | `config.recommendations.engineerHourCost` | `0` |
| `RECOMMENDATION_MIN_SAMPLES` | `config.recommendations.minSamples` | `36` |
| `VERIFICATION_WINDOW_HOURS` | `config.recommendations.verification.windowHours` | `72` |
| `VERIFICATION_MIN_SAMPLES` | `config.recommendations.verification.minSamples` | `36` |
| `VERIFICATION_MIN_SAVINGS_RATIO` | `config.recommendations.verification.minSavingsRatio` | `0.5` |
| `VERIFICATION_USAGE_HEADROOM` | `config.recommendations.verification.usageHeadroom` | `1.1` |
| `VERIFICATION_MAX_RESTART_DELTA` | `config.recommendations.verification.maxRestartDelta` | `0` |
| `VERIFICATION_MIN_READINESS` | `config.recommendations.verification.minReadiness` | `0.99` |
| `VERIFICATION_MAX_THROTTLE_RATIO` | `config.recommendations.verification.maxThrottleRatio` | `0.05` |
| `RECOMMENDATION_LIFECYCLE_INTERVAL` | `config.recommendations.verification.lifecycleInterval` | `5m` |
| `CHANGE_MEASUREMENT_ENABLED` | `config.recommendations.changeMeasurement.enabled` | `true` |
| `CHANGE_MEASUREMENT_PRE_WINDOW_DAYS` | `config.recommendations.changeMeasurement.preWindowDays` | `7` |
| `CHANGE_MEASUREMENT_POST_WINDOW_DAYS` | `config.recommendations.changeMeasurement.postWindowDays` | `7` |
| `CHANGE_MEASUREMENT_WARMUP_HOURS` | `config.recommendations.changeMeasurement.warmupHours` | `1` |
| `CHANGE_MEASUREMENT_MIN_COVERAGE` | `config.recommendations.changeMeasurement.minCoverage` | `0.8` |
| `CHANGE_MEASUREMENT_BOOTSTRAP_SAMPLES` | `config.recommendations.changeMeasurement.bootstrapSamples` | `1000` |
| `CHANGE_MEASUREMENT_EPSILON_RATIO` | `config.recommendations.changeMeasurement.epsilonRatio` | `0.02` |
| `RECOMMENDATION_SCAN_INTERVAL` | `config.recommendations.scanInterval` | `1h` (`1m` to `24h`, units `s`, `m`, `h`) |

`RECOMMENDATION_APPLY_TOLERANCE` drives the automatic **apply detection**: Kubernetes workload snapshots preserve CPU and memory requests per container, and a typed GitOps action with a plan in flight is considered only after its persisted action status reaches `sync_pending`. It is marked applied only when the targeted live request matches the recommended value within tolerance **and** moved from the baseline toward it: a live request still equal to `current_*` is never an applied change, even when the proposal lies within the tolerance of it (a 20Mi → 23Mi proposal on an untouched 20Mi request). A rollout that completed before the recommendation was created is not its application either. A decrease by itself, including an unrelated large decrease, is not sufficient. A recommendation with a typed action but no plan in flight (action status `none`, `failed` or `closed`) whose proposal is already live is recorded as applied (`detected`). A live request that equals a change already recorded on another recommendation of the same workload, container and resource (`applied`, `verifying`, `verified` or `rollback_review`, by its `actual_*`, or its proposal when `actual_*` is unknown) is never recorded a second time: the regenerated recommendation is retired as `stale` (`no_effective_change`), as is one whose rollout completed before it was created. The pre-apply workload-generation and per-container baseline is captured before provider mutation.

The lifecycle job (`RECOMMENDATION_LIFECYCLE_INTERVAL`, default every 5 minutes in the collector/scheduler container) runs apply detection (which also records changes and workload revisions), outcome verification, change measurement and TTL expiry.

## Source Map

| Area | Main files |
|---|---|
| DTOs and lifecycle fields | `src/greenkube/models/metrics.py`, `src/greenkube/models/evidence.py` |
| Optimization engine | `src/greenkube/core/optimization/engine.py`, `context_builder.py`, `context.py` |
| Scheduled scans and run queue | `src/greenkube/core/optimization/scan_service.py`, `src/greenkube/storage/{sqlite,postgres}/optimization_run_repository.py`, `src/greenkube/api/routers/optimization.py` |
| Per-source reconciliation and run diff | `src/greenkube/core/recommendation_reconciliation.py` |
| Analyzers | `src/greenkube/core/optimization/analyzers/` |
| Source SDK (signals, runner, registry, contract checks) | `src/greenkube/signals/`, `src/greenkube/api/routers/sources.py`, `src/greenkube/storage/{sqlite,postgres}/signal_repository.py` |
| Sources (native, VPA, NodePool) | `src/greenkube/core/optimization/providers/`, `registry.py` |
| Consensus engine | `src/greenkube/signals/consensus.py` |
| Consolidation, dedup, provenance | `src/greenkube/core/optimization/dedup.py` |
| Recommendation v2, revisions, plan pins | `src/greenkube/models/recommendation_v2.py`, `src/greenkube/storage/recommendation_revisions.py`, `src/greenkube/core/recommendation_revisions.py`, `src/greenkube/api/routers/recommendations_v2.py` |
| Evidence, risk, ranking | `src/greenkube/core/optimization/evidence.py`, `risks.py`, `scoring.py`, `enrich.py` |
| Lifecycle, apply detection, verification | `src/greenkube/core/optimization/lifecycle.py`, `applied_detector.py`, `verifier.py` |
| VPA discovery | `src/greenkube/collectors/vpa_collector.py`, `src/greenkube/utils/k8s_quantities.py` |
| Karpenter discovery | `src/greenkube/collectors/karpenter_collector.py` |
| Orphaned PV discovery | `src/greenkube/collectors/pv_collector.py` |
| Orphaned LoadBalancer discovery | `src/greenkube/collectors/lb_collector.py` |
| Deletion evidence and safety rules | `src/greenkube/collectors/k8s_object_reader.py`, `deletion_candidates.py`, `src/greenkube/core/deletion_safety.py`, `src/greenkube/storage/deletion_candidate_repository.py` |
| Ranking (API DTOs) | `src/greenkube/core/recommendation_ranking.py` |
| Realized savings estimation | `src/greenkube/core/recommendation_realization.py` |
| Savings ledger attribution | `src/greenkube/core/savings_attributor.py` |
| PR bot | `src/greenkube/automation/` |
| Recommendation API routes | `src/greenkube/api/routers/recommendations.py` |
| Automation API routes | `src/greenkube/api/routers/automation.py`, `plans.py`, `gitops.py`, `webhooks.py` |
| GitOps automation | `src/greenkube/gitops/`, `src/greenkube/automation/` |
| Prometheus gauges | `src/greenkube/api/metrics_endpoint.py` |
| Scan & lifecycle jobs | `src/greenkube/cli/start.py` |
| Migrations | `src/greenkube/core/migrations/scripts/{sqlite,postgres}/` |
| Storage adapters | `src/greenkube/storage/recommendation_mapper.py`, `src/greenkube/storage/sqlite/recommendation_repository.py`, `src/greenkube/storage/postgres/recommendation_repository.py` |
| Frontend | `frontend/src/routes/recommendations/+page.svelte`, `frontend/src/lib/api.js` |
| End-to-end tests | `tests/integration/test_recommendation_lifecycle_e2e.py` |
