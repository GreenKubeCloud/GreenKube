# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **GitOps recommendation readiness:** The recommendations page checks automation readiness before enabling PR previews, explains when manual application is needed, and refreshes PR status when an operation opens a pull request.
- **Gitea web URL support:** `automation.providers[].webBaseUrl` supports a browser-facing Gitea origin distinct from its API URL, so returned PR links point to the configured web host.
- **Renderer firewall option:** `rendererSandbox.egressFirewall.privilegedInitContainer` supports clusters that require a privileged init container to configure the sandbox egress firewall.
- **Helm configuration rollouts:** API and automation Deployments now restart when the generated ConfigMap changes; in worker mode, the production API receives the automation settings needed to report readiness.
- **Helm: credentials bootstrap hook (N1, BUG-58).** A `<release>-credentials-bootstrap` pre-install/pre-upgrade hook (an Argo CD PreSync hook) creates the generated `GREENKUBE_API_KEY` and bundled PostgreSQL password once, in the Secret `<release>`, and afterwards only adds missing keys. The Secret is outside the Helm release and Argo CD tracking, so upgrades and syncs never rotate it and `helm uninstall` keeps it with the PostgreSQL volume. The hook refuses to generate a new password for a retained PostgreSQL volume and prints the recovery command.
- **Helm: install notes print the exact API-key command and the setup URL (N11).** The command targets the Secret that actually holds the key (generated, values or `secrets.existingSecret`).
- **CI: Dependabot** for uv, npm, GitHub Actions and Docker, targeting `dev` (O6).
- **CI: render-stability check** — two `helm template` runs must produce identical output for the standalone and production profiles, with automation on and off, and with credentials supplied in values (N1).
- **Release: Artifact Hub changes list generated from the CHANGELOG** by `scripts/artifacthub_changes.py`, run by `scripts/release.sh` and the release workflow (N10).
- Impact Model v1 (`greenkube.impact`, model version `1.0.0`): value types (`WorkloadRef`, `WorkloadImpact`, `ImpactDelta`, `UnitPrices`), the `ReservationAwareAllocation` policy as pure, conservation-tested functions with an explicit `__idle__` workload per node, and `ImpactModel.project` for container request changes, workload and asset deletions, replica/schedule changes and node-pool consolidation (roadmap B2, partial: allocation is not yet wired into collection).
- Cost fallback chain for projections: each sample is priced from its observed OpenCost cost (its CPU/RAM split, or its total apportioned with OpenCost's CPU/RAM weights), then observed per-resource costs on the node, then a bundled `PricingCatalog` of on-demand list prices in the configured currency, then custom rates defaulting to the OpenCost default custom pricing (roadmap B3).
- `scripts/generate_pricing_catalog.py` and generated catalogs from public price lists: AWS (Price List Bulk API), Azure (Retail Prices API), OVHcloud and Scaleway (public catalogs). GCP stays empty because its catalog API requires a key (roadmap B3).
- Settings `IMPACT_PRICING_CPU_CORE_HOUR`, `IMPACT_PRICING_RAM_GIB_HOUR`, `IMPACT_PRICING_CURRENCY` (Helm `impact.pricing.*`), `IMPACT_REALIZATION_FACTOR` (`impact.realizationFactor`, default 1.0) and `CARBON_PRICE_PER_TONNE` (`impact.carbonPricePerTonne`, default 100). `ImpactDelta.value_annual` combines expected cost and carbon at that price (roadmap B, I2 groundwork).
- Recommendations carry an `impact` JSON projection (model version, cost and CO2e breakdown, capacity released, realization factor, unit prices and their source), persisted in `recommendation_history.impact` (migration 0046) and returned by the recommendations API (roadmap B4).
- Methodology reference `docs/methodology/impact-model.md`: allocation policy, formulas, cost chain, realization factor, carbon price, SCI mapping and limitations (roadmap B1).
- Scheduled, leader-gated recommendation scans: the controller's `recommendation_scan` job queues a cluster-wide run every `RECOMMENDATION_SCAN_INTERVAL` (default `1h`, Helm `config.recommendations.scanInterval`) and executes queued runs. Queueing and claiming are atomic in the database, so two API replicas plus one controller produce exactly one scan per interval (E1, BUG-21).
- Optimization run API: `POST /api/v1/optimization/runs` queues a scan and returns `202 {"run_id", "status": "queued"}` (deduplicated while a run is pending for the same scope), `GET /api/v1/optimization/runs/{run_id}` returns its status and outcome, and `GET /api/v1/optimization/runs?limit=` lists recent runs (E1, E10).
- Each optimization run persists per-source health (`succeeded`, `incomplete`, `failed`, `unavailable`), recommendation counts and the diff against the previous active set (new, changed, resolved, unchanged), with up to 100 fingerprints per list (E10).
- Succeeded and failed optimization runs older than 30 days are pruned when the next cluster-wide run is queued (E10).
- The optimization-run and recommendation-scan metrics (`greenkube_optimization_runs_total`, `greenkube_last_successful_optimization_run_timestamp_seconds`, `greenkube_recommendation_scan_*` ...) are recorded in the controller and served by its metrics endpoint (`/prometheus/metrics` on `CONTROLLER_METRICS_PORT`, Helm `collector.metricsPort`, default `9102`), on every controller replica. The chart rejects a standalone `collector.metricsPort` equal to `config.api.port` (A12, BUG-14).
- Migration `0045_optimization_run_queue` turns `optimization_runs` into the run request queue (queued state, trigger, requester, claimer, counts, source outcomes, diff) on SQLite and PostgreSQL; existing rows are kept and tagged `legacy`.
- **Plan cancel endpoint (D6):** `DELETE /api/v1/automation/plans/{plan_id}` cancels an unapproved preview. A draft or validated plan becomes `superseded` (`204`); an unknown plan returns `404` and any other status `409`.
- **`automation.policy.missingEvidence` (D13):** `fail` rejects a plan when discovery could not collect rollout, controller-health, HPA, VPA or PDB evidence; `warn` allows it with a warning in `policy_result.warnings`. The chart defaults to `fail` for `profile: production` and `warn` for `standalone`.
- **VPA policy gate (D13):** a VPA in `Auto`, `Recreate`, `InPlaceOrRecreate` or `InPlace` mode that targets the workload blocks the plan as a conflicting writer. `Initial` mode passes with a warning.
- **Kustomize rollback (D14):** rollback previews for Kustomize sources render through the sandboxed renderer instead of failing with `rollback_unsupported` (BUG-54).
- **Supervised automation loops (D9):** worker loops retry failed iterations with jittered backoff and error codes, and crashed loop tasks restart with backoff. `GET /api/v1/automation/status` reports `worker.inline_loop` (state, heartbeat age, restarts, last error code).
- **Plan revision tracking (D7):** plans record `committed_on_revision` separately from the planned `base_revision`.
- **SQLite transaction helper (D8):** `DatabaseManager.transaction()` commits on success and always rolls back on error, using a savepoint inside an enclosing unit of work. A lint test rejects bare `BEGIN` statements outside the transaction helpers.
- **Recommendation filters and ranking on cards (K2):** The recommendations page filters by namespace, source, risk level and capability through the existing `GET /recommendations/active` query parameters (the Ignored tab applies the same filters). Each card shows its ranking score and top contributing factor, with the full factor breakdown on hover.
- **Rescan with run tracking (K9, E1):** A **Rescan** button requests a scan with `POST /optimization/runs`, polls the run and reloads the results when it completes. When the server also offers the optional run list (`GET /optimization/runs?limit=1`), a page opened while a scan is running follows that run and reloads on completion; a missing list never disables Rescan. Only a `404`/`405` from `POST /optimization/runs` makes the button fall back to plain reads.
- **Sign out (F13):** A **Sign out** button in the sidebar forgets the API key for the current tab.
- Git providers expose `compare_trees`, `get_blob_ids`, `get_tree` and `is_ancestor` (GitHub compare, GitLab merge base, Gitea compare) plus the `commit_ancestry` capability (D1, D5).
- Per-operation Git provider call budget (200 requests per plan operation) and the metrics `greenkube_git_provider_requests_total`, `greenkube_git_provider_operation_calls` and `greenkube_git_provider_budget_exhausted_total` (D5, BUG-47).
- Sync reasons `sync_confirmed_descendant`, `revision_behind_merge`, `revision_not_descendant`, `ancestry_unavailable`, `planned_change_not_present` and `planned_change_unverified` (D1).
- `PromQLCatalog` (`collectors/promql.py`) is the single definition of every PromQL expression; rate windows span `max(step, 4 × PROMETHEUS_SCRAPE_INTERVAL)` (A1).
- Gap-aware collection: half-open `CollectionWindow`, `collection_watermarks` table (migration 0038) and a catch-up job that collects every missing window oldest first, up to `COLLECTION_MAX_BACKFILL`, and advances the watermark only after a window is written (A2).
- Settings `PROMETHEUS_SCRAPE_INTERVAL` (`30s`), `COLLECTION_SAFETY_LAG` (`1m`) and `COLLECTION_MAX_BACKFILL` (`24h`), with Helm values `config.prometheus.scrapeInterval`, `config.collection.safetyLag` and `config.collection.maxBackfill` (A1, A2).
- Collection telemetry: `greenkube_collection_watermark_lag_seconds{source}`, `greenkube_collection_gap_windows_total{source}`, `greenkube_collection_windows_total{source}`, `greenkube_collection_window_errors_total{source,reason}`, `greenkube_collection_window_duration_seconds{source}`, `greenkube_collection_dropped_samples_total{reason}` and `greenkube_unknown_power_profile_nodes` (A2, A6, A10).
- `LeaderFence`: leader-gated controller jobs receive a fencing token (the Lease generation) and the collection job re-checks it before writing rows and before advancing the watermark (A14, BUG-13).
- Per-dimension provenance on pod and container metric rows and their hourly aggregates: `cost_source`, `usage_source`, `power_source`, `intensity_source` (migration 0039, `greenkube.models.provenance` with `is_cost_trusted()` / `is_usage_trusted()` helpers). Legacy rows are backfilled (`cost_source=opencost` when a positive cost was stored, `intensity_source=unknown`; container rows inherit their pod row's `cost_source` and `emaps_zone`). (A11)
- OpenCost cost components `cost_cpu`, `cost_ram`, `cost_storage`, `cost_network`, `cost_lb` on pod and container rows; container rows split CPU cost by CPU share and RAM cost by memory share. (A4)
- Container `cpu_limit` / `memory_limit` collection and persistence; native sidecars (init containers with `restartPolicy: Always`) are collected as steady-state containers (`kind="sidecar"`), classic init containers only on request (`kind="init"`). (A7, BUG-10)
- `INTENSITY_MAX_AGE` setting (Helm `config.intensityMaxAge`, default `6h`). (A8)
- Controller Prometheus endpoint `/prometheus/metrics` (alias `/metrics`) on `CONTROLLER_METRICS_PORT` (Helm `collector.metricsPort`, default `9102`), with a `<release>-controller-metrics` Service, a controller ServiceMonitor and NetworkPolicy ingress. New metrics: `greenkube_collection_source_requests_total`, `greenkube_collection_source_duration_seconds`, `greenkube_collection_run_duration_seconds`, `greenkube_metric_rows_written_total` (provenance mix). Verification telemetry recorded by the controller is now scrapeable. (A12, BUG-14)
- `metrics_compression_watermarks` table (migration 0040). (A3)
- **Idempotent savings accrual (C0.7, BUG-33):** savings accrue once per recommendation, method and UTC hour slot. A new `accrual_watermarks` table records the last accrued slot per cluster and is advanced by compare-and-swap in the same transaction as the inserts, so restarts, retries and a second controller cannot double count. Slots missed during downtime are backfilled (7 days on the first run after upgrade, at most 168 slots per run). The slots in which a change was applied and verified are accrued pro rata. A recommendation that has never accrued (for example a change detected after its `applied_at`) is caught up for the slots already behind the watermark, up to 7 days.
- **Read-side ledger views (C0.6):** `savings_accruals_v1` (raw accruals plus legacy compressed rows) and `savings_accruals_v1_hourly_rollup` replace the destructive hourly compression. They are named `v1` so that `savings_ledger_entries` stays free for the roadmap C6 ledger table.
- **Savings ledger counters (C0.14):** `greenkube_co2e_savings_reversed_grams_total` and `greenkube_cost_savings_reversed_dollars_total` report superseded accruals and negative accruals (such as negative declarations); `greenkube_savings_run_rate_annual_co2e_grams` / `..._cost_dollars` expose current annual rates by method; `greenkube_dashboard_savings_accrued_co2e_grams` / `..._cost_dollars` expose window accruals by method.
- **`greenkube_attribution_errors_total{reason}` (C0.8, BUG-34):** counts recommendations skipped by attribution and accrual batches that could not be stored.
- **Immutable savings measurements (C0.13, BUG-37):** every verification decision is stored once in the new `savings_measurements` table (migration `0041`; database triggers reject `UPDATE` and `DELETE` on SQLite and PostgreSQL) with its windows, signed per-quantity estimates, health gates, reasons and an evidence SHA-256. Re-measurements add a superseding row; a crashed verification replays its stored decision instead of deciding again.
- **Explicit re-measurement (C0.5, BUG-31):** `POST /api/v1/recommendations/{id}/remeasure` starts a new fixed-horizon measurement of a `not_realized` or `inconclusive` change (`202`; `409` otherwise).
- **Rollback-review dismissal (C0.11, BUG-37):** `POST /api/v1/recommendations/{id}/rollback-review/dismiss` returns a false-positive `rollback_review` to `applied` with a required reason and measures it again.
- **Workload health collector (C0.9, BUG-35):** the lifecycle job now gives the verifier OOM kills (`kube_pod_container_status_last_terminated_reason`), readiness (`kube_pod_status_ready`) and the CPU throttling ratio (`container_cpu_cfs_throttled_periods_total / container_cpu_cfs_periods_total`) from Prometheus. Pod presence is read over the whole window, so pods replaced before its end still count. A signal that cannot be read is reported `not_available` with its reason, never counted as a pass.
- **Lifecycle vocabulary (C0.11):** event types `not_realized`, `inconclusive`, `remeasure_requested`, `rollback_dismissed` and `regressed`; status `regressed` (reachable only from `verified`, reserved for drift detection).

### Changed
- **Positioning and documentation:** GreenKube is now presented as an optimization loop: aggregate recommendations, deliver them as GitOps pull requests, prove the savings.
  - The README is rewritten around that loop, with a "What works today" table and an explicit beta label on savings verification.
  - New `docs/concepts.md` defines the shared vocabulary: sources, recommendations, bindings, plans, sync, apply detection, verification, ledger.
  - The package, Helm chart, architecture, recommendation and contributing descriptions are aligned with the new positioning.
  - `docs/recommendation.md` now states that the VPA and Karpenter connectors are enabled by default.
  - New `docs/roadmap.md`: positioning, 17 prioritized categories with implementation specifications, a defect register and milestones. The plan includes one install topology (no profiles), GitOps automation that activates itself, and persistent sign-in.
- **Helm: Argo CD-safe Secrets (N1, BUG-58).** Rendering no longer uses `lookup` or `randAlphaNum`, so `helm template` output is a pure function of the values. Credentials supplied in values are rendered into a regular (non-hook) Secret `<release>-values`; each Pod key is projected from exactly one Secret. `secrets.existingSecret` is documented as the GitOps path. Upgrades from 0.3.x keep the existing Secret `<release>` and its API key and password unchanged. **Breaking for `rbac.create=false`:** installs that relied on a generated API key or PostgreSQL password no longer render. Before upgrading, set `secrets.existingSecret=<release>` (the Secret that 0.3.x created) or supply `secrets.apiKey` and `postgres.auth.password`.
- **Helm: `config.boavizta.url` defaults to `""` (N1).** GreenKube then uses the public Boavizta API. As with the Prometheus and OpenCost URLs, a URL saved on the Settings page now survives Pod restarts, and a URL set in values takes precedence. With `secrets.existingSecret`, a URL set in values is passed through the ConfigMap, as in 0.3.x.
- **Helm: CORS is same-origin by default (N4, BUG-59).** `config.corsOrigins` defaults to `""` instead of `*`.
- **Helm: Artifact Hub category is `integration-delivery`** and the changes list describes the current release (N10).
- **Helm: `.helmignore` excludes the helm-unittest suites** from the packaged chart.
- **CI (O6):** the coverage gate is raised from 70% to 80%; the Python job timeout is 20 minutes; the release is gated on frontend tests and a frontend build; the CI workflow is read-only.
- **Release: `greenkube/greenkube:latest` is pushed for the newest release (O6, BUG-62).** Pre-release tags such as `v0.5.0-rc1` do not count when the workflow picks the newest release.
- Recommendation evidence reports the Impact Model as its savings method (`impact_model/<version>/<kind>`) instead of the retired `request_reduction_ratio` and `observed_cost_annualized` labels.
- Karpenter NodePool consolidation keeps enough nodes for both the CPU and the memory requests of the pool.
- Off-peak scaling recommendations now project savings (replica-hours removed in the idle window). Karpenter NodePool recommendations project the price and idle/embodied emissions of the removable nodes instead of `pool cost × (1 − utilization)`. Zombie and idle-namespace recommendations project CO2e from the allocated share (idle and embodied by reservation, dynamic by usage, capped at the node) instead of the usage-based row value.
- Without an analysis window, projections annualize over the observed span of the samples instead of reporting the raw window total.
- The API no longer runs a recommendation scan at startup (one per replica) and recommendation GET endpoints never run the optimizer or write: `refresh=true` on `/recommendations`, `/recommendations/active` and `/recommendations/top` is deprecated and only queues a run, returning its id in the `X-GreenKube-Run-Id` header (E1, BUG-21).
- `GET /api/v1/recommendations` no longer generates recommendations on an empty first read.
- The controller's scan poll waits for its initial collection; the initial cycle then always runs one cluster-wide scan on the fresh data, so the first recommendations no longer wait a full `RECOMMENDATION_SCAN_INTERVAL` after a (re)start (E1).
- The deprecated `refresh=true` reuses a run covering the scope that is pending or finished within 5 minutes instead of queuing another scan; a failed run is reused for 1 minute only, then a refresh queues a retry (E1).
- The controller's default memory limit (`resources`, `production.components.controller.resources`) is raised from 512Mi to 1Gi because scans now run there (E1).
- Each recommendation source runs in isolation with a 30 s probe and 5 min collection bound; a hung connector is recorded as failed instead of stalling the scan (E8).
- A run with some failed sources is now a degraded success (failed sources are listed in `error` and `sources`); it fails only when every attempted source failed or persistence failed. Persistence errors now fail the run instead of being logged as a success.
- **Preview supersede (D6):** previewing the same workload again supersedes its draft or validated plans instead of returning `409 WORKLOAD_PLAN_ALREADY_EXISTS` for 30 minutes (BUG-48).
- **Moved branch heads (D7):** approval and the worker accept a branch head that moved after planning when every touched file is unchanged; the commit is created on the moved head. This includes rollback plans, whose identity stays bound to the planned base revision; the pull-request body lists both the planned base and the moved head. Only a change to a touched file requires a new preview (BUG-49).
- **HPA gate per resource (D13):** an HPA blocks only the resource it scales on. A CPU-utilization HPA no longer blocks memory-only changes; a `ContainerResource` metric blocks only its container (BUG-55).
- **Readiness (D9):** `GET /api/v1/health` returns `503` (`degraded`) and automation status reports `ready: false` while the inline worker loop is down or its heartbeat is stale (BUG-51). The chart's API liveness probes now use `/api/v1/health/heartbeat`, so a stalled loop never restarts the API container.
- **Unrelated pull-request webhooks (D11):** verified events for pull requests that GreenKube does not track are stored with the terminal status `ignored`, answered with `202`, never retried, counted as `greenkube_webhook_deliveries_total{status="ignored"}` instead of `error`, and pruned by retention (BUG-53).
- **Query shapes (D15):** migration `0044` adds indexes for plan listings by status, source and workload identity, re-creates the sync-queue partial index to match its query, and tracks branch-cleanup completion on pull-request rows. Plan listings hydrate files and recommendation links in batched queries and accept a `limit`; the expiry sweep reads only due plans in bounded batches; policy cooldowns read workload history in one query (BUG-57).
- **Truthful sort options (K2, BUG-20):** The default order is labelled **Ranking score (default)** and keeps the server order. **Projected cost savings** and **Projected CO₂e savings** are explicit sorts; ranking profiles are labelled as profiles and show their own scores.
- **Plan previews are discarded on close (D6, BUG-48):** Closing the dialog of an unapproved preview calls `DELETE /automation/plans/{id}`. A plan whose approval was requested is never discarded, even when the dialog closes while the approval is in flight, and there is no fallback to the cancel endpoint (which also cancels approved plans). A `409 WORKLOAD_PLAN_ALREADY_EXISTS` now explains what blocks the preview and when to retry.
- **Connectivity popup:** Its dismissal is remembered for the browser tab session instead of reappearing on every reload (K9).
- Opening a plan PR compares the plan branch with its base commit by tree and blob IDs and reads only the planned paths: about 12–14 provider calls for a 5,000-file repository instead of ~6×(N+1) (D5, BUG-47).
- GitHub commits validate preconditions from one cached recursive tree and keep the original file mode; GitHub and Gitea branch creation tries the write first; Gitea stages the atomic commit with `new_branch` in the same call (D5).
- A synced plan records the observed controller revision as `sync_revision` and its confirmation reason in the plan audit trail (D1).
- Merged plans record which commit landed in their audit reason: `pull_request_merged`, `pull_request_merged_squash_commit` or `pull_request_merged_fast_forward_head` (D4).
- **Data note:** CPU, memory and energy stored before this release are inflated (the pod cgroup series was summed with the container series, ≈2× CPU). New rows carry `calculation_version = <version>+range.2`. On upgrade the first collection run re-collects the last 24 hours (`COLLECTION_MAX_BACKFILL`) and overwrites those rows with corrected values; older raw and hourly rows are not rewritten (A1, BUG-01).
- A stored row with `timestamp = T` now describes `[T, T + step)` (rate evaluated at `T + step`), and every repository range read uses `timestamp >= start AND timestamp < end` (A2, BUG-03).
- The metrics collection job runs on wall-clock step boundaries plus `COLLECTION_SAFETY_LAG` instead of a jittered interval, keeping the watermark lag below two steps (A2).
- `greenkube start --last` re-collects the given range with an idempotent upsert instead of doing nothing when any row exists (A2, BUG-03).
- Nodes with an unknown instance type get a power profile derived from their CPU capacity and the provider's CCF per-vCPU coefficients (cross-provider average when unknown) instead of `DEFAULT_INSTANCE_PROFILE`; the remaining fallback is flagged as estimated and counted (A10, BUG-08).
- `PrometheusCollector.collect_range` raises `PrometheusQueryError` when Prometheus is unconfigured, unreachable or returns an error, so an outage is retried rather than recorded as an empty window (A2).
- OpenCost is queried on `/allocation/compute` with `aggregate=namespace,pod`, `accumulate=true`, `includeIdle=false` over the exact collection window; per-step cost uses the minutes the pod ran in the window instead of a trailing total divided by steps. (A4, BUG-41)
- Container rows inherit their pod's provenance and `is_estimated` instead of being blanket-flagged as estimated; `is_estimated` is deprecated in favour of provenance. (A11, BUG-29)
- Hourly container and pod readers keep `sample_count`. (BUG-29)
- Demo metrics carry provenance (`opencost`, `prometheus`, `instance_profile`, `consolidated`/`realtime`) so measurement gates trust them like an instrumented cluster. (A11)
- Electricity Maps and Wattnet collectors return no record when no measurement is available; zone and global defaults are applied at read time only. (A8)
- Raw metrics pruning removes whole hours below the compression watermark only, whatever `METRICS_RAW_RETENTION_DAYS`. (A3)
- The compressor logs a warning when `METRICS_COMPRESSION_AGE_HOURS` is below the 24-hour carbon-intensity recompute window, because compressed hours are never re-read. (A3)
- Rows priced with the flat `DEFAULT_COST` placeholder carry `cost_source=none` (as the 0039 backfill does), keeping `custom_rate` for configured per-unit rates. (A11)
- The Wattnet collector records every request outcome (`skipped`, `error`, `empty`, `success`) and its duration in `greenkube_collection_source_*`. (A12)
- **`GET /recommendations/savings` (C0.14, BUG-40):** returns separate, never-summed blocks: `projected_annual`, `run_rate_annual`, `realized_to_date`, plus `projected_to_date`, `user_declared_annual` and `user_declared_to_date`, with `diagnostics`. The flat `total_*`, `measured_*` and `prorated_*` fields remain as deprecated aliases; `total_*` now equals realized (measured) savings only.
- **Ledger method `prorated` is now `projected` (C0.4):** migration 0043 relabels existing rows, and projected accruals are excluded from every realized figure (API, gauges).
- **`greenkube_co2e_savings_attributed_grams_total` and `greenkube_cost_savings_attributed_dollars_total` (C0.14):** now Prometheus counters with a `method` label; they never decrease. `greenkube_dashboard_savings_*_total` now carry realized (measured) savings only, and `greenkube_cluster_*_saved_*_total` only count recommendations still accruing as projected.
- **User-declared savings on `PATCH /recommendations/{id}/apply` (C0.8):** values must be finite and at most 10× the projection (`422` otherwise); negative values are accepted and flagged. They are stored under `baseline.user_declared_savings` and accrue with method `user_declared`, never as measured; the recommendation's projection columns keep the server-side estimate. If the declaration cannot be stored after the apply, the endpoint answers `503`.
- **PostgreSQL ledger values** are stored as `DOUBLE PRECISION` instead of `REAL`.
- **Savings gauges on `/prometheus/metrics`** are reloaded from the database at most once every 60 seconds per API process instead of on every scrape.
- **Fixed-horizon verification (C0.5, C0.12):** the after-window starts at rollout completion and lasts `VERIFICATION_WINDOW_HOURS`; it no longer grows with late evaluation. The baseline window has the same length and ends at the PR merge (or plan approval). `not_realized` and `inconclusive` are terminal for a measurement.
- **Apply detection waits for the rollout (C0.12, BUG-38):** a change counts as applied only once every replica is updated and available and `observedGeneration >= generation`; `applied_at` is the rollout completion time (Deployment `Progressing` condition, otherwise detection time). `freeze_baseline` records the evidence window it copies (`captured_at`, `evidence_window`), its `frozen_at` time and `baseline_window_end`.
- **Projection is reported, not gated:** a change is realized when the measured saving clears a 2% noise floor of the baseline rate. `VERIFICATION_MIN_SAVINGS_RATIO` now only flags a realized saving far below its projection.
- **Rightsizing never claims CO2e:** the usage-based CO2e delta of a request change is kept in the measurement as `not_claimed`; `measured_co2e_saved_grams` is 0.
- **Trusted rows by provenance (C0.3, BUG-29):** cost rows are selected by `cost_source` (`opencost`, `kubecost`, `pricing_catalog`, finite non-negative cost; delegated to the shared A11 provenance helpers when installed) when present, otherwise by `total_cost > 0` and `is_estimated = false`; rates divide by covered time. Container-scoped changes are measured on trusted container rows, or on their workload when container rows carry no trusted cost.
- **Attribution groups (C0.10, BUG-36):** CPU and memory records on the same container applied by the same plan (whatever the delay between their detections), or without a plan within 15 minutes, are measured once; the cost delta is split by `cost_cpu`/`cost_ram`, or by projected share without components. Membership is recorded (`baseline.attribution_group_ids`, `group_recommendation_ids`) and does not depend on the measurement window; each share is computed against every member still in effect. Re-measurement and rollback-review dismissal act on the whole group with one new window.
- **State machine (C0.11, BUG-37):** `PATCH /recommendations/{id}/apply` accepts only `active` and `pr_open` records (`409` otherwise); `active → verified` is no longer allowed; every transition, including ignore, unignore and TTL expiry, is a compare-and-swap written in the same transaction as its event. `PATCH /recommendations/{id}/ignore` accepts only `active`, `stale`, `expired` (or already `ignored`) records and `DELETE /recommendations/{id}/ignore` only `ignored` ones (`409` otherwise).
- **Health signals:** readiness is the share of ready `kube_pod_status_ready` samples across the observed pods (weighted by pod lifetime, so short-lived autoscaled pods no longer drag it down); workload-scope throttling excludes the pod cgroup and pause-container series; health queries fall back to `/prometheus/api/v1/query` like the Prometheus collector.
- **Verification passes:** a decision persisted before a crash is replayed from its stored measurement instead of being observed again; `greenkube_verification_outcomes_total` is incremented after the transition is written, so a replay is counted once; metric reads are shared by the groups measured over the same window in one pass.
- **Verification metrics:** `greenkube_verification_health_gate_failures_total` is labelled by gate name instead of free-text reasons.
- The SQLite migration runner keeps trigger bodies (`BEGIN ...; END;`) in one statement.

### Fixed
- **Automation worker heartbeat recovery:** Idle workers refresh their heartbeat and clear expired lease state without overwriting an active lease.
- **API: a wildcard CORS origin is never combined with credentials (N4, BUG-59).** `CORS_ORIGINS=*` now allows any origin without `Access-Control-Allow-Credentials`; credentials are allowed only for explicitly listed origins.
- **Helm: the PostgreSQL credential check no longer blocks the first Argo CD sync (N1).** The pre-upgrade check skips when the credentials or the database do not exist yet, and fails only on a real authentication error.
- **Helm: the retained-volume guard works under Argo CD (N1, BUG-58).** It moved from a render-time `lookup` to the bootstrap hook.
- **Helm: the PostgreSQL credential check validates a newly set `postgres.auth.password` (N1).** It reads the new password from a short-lived pre-upgrade hook Secret, because `<release>-values` is updated only after the hooks run.
- CPU and memory rightsizing (native analyzers and the VPA connector) no longer apply the request-cut ratio to the total pod cost: each resource is priced separately and anchored to the container's observed OpenCost cost, so CPU and memory recommendations on one container can no longer claim more than the container is charged (BUG-15, roadmap B4).
- Request-cut CO2e projections now come from the idle and embodied share released times the realization factor, instead of scaling usage-based emissions that requests do not change. Memory cuts project no CO2e until memory power is modeled (BUG-16, roadmap B4).
- Active recommendation lists were sorted `ORDER BY priority DESC` on a text column (medium, low, high). They are now ordered by `ranking_score DESC NULLS LAST, potential_savings_cost DESC NULLS LAST, id` on both backends (E2, BUG-20).
- A single failed source blocked persistence and reconciliation for every source. Other sources now persist normally, and a recommendation becomes `stale` only when every source that supported it completed the scan; failed, unavailable or incomplete sources keep their last known recommendations, and a lower-priority source no longer overwrites the last known advice of an incomplete higher-priority one (E8, BUG-22).
- A run already failed as abandoned is no longer overwritten when its original controller finishes it late; on PostgreSQL, scheduled-run creation is serialized with an advisory lock so two controllers cannot schedule the same interval (E1).
- Optimization-run metrics (`greenkube_optimization_runs_total`, `greenkube_optimization_run_duration_seconds`, `greenkube_optimization_analyzer_failures_total`, `greenkube_recommendations_generated_total`, `greenkube_last_successful_optimization_run_timestamp_seconds`, scan resource gauges) and verification metrics are recorded in the controller, which had no metrics endpoint, so they stayed at zero where Prometheus scrapes once scans moved out of the API. They are now served by the controller's listener (E1, BUG-14).
- The engine resets a connector's `last_collect_complete` flag before every `collect()`, so one partial result no longer keeps a source incomplete forever; namespaced runs no longer count cluster-scoped output as new, and `recommendation_count` is the number of recommendations actually persisted (E8, E10).
- **Retention (D2):** pruning rendered file contents keeps a `file_plan_summary` (paths, original blob SHAs, content digests, diff statistics, validation, digest), so `GET /api/v1/automation/plans/{plan_id}` no longer returns `500` and new plans for the workload no longer fail with `policy_evidence_missing`. Retention never deletes `recommendation_events` (BUG-44). A rollback preview of a pruned plan returns `rollback_unsupported` with an explanation instead of a generic not-ready error.
- **SQLite connection poisoning (D8):** a compare-and-set conflict on a pull-request update no longer leaves the shared SQLite connection inside a transaction that broke every later write (BUG-50).
- **Lease loss after a PR was opened (D10):** a worker that reclaims an operation adopts the open PR on the plan's deterministic branch (`pr_open`) instead of marking the plan `stale` (BUG-52).
- **Render warnings (D12):** plan previews return the renderer's warnings (bounded and with credentials redacted) instead of `render-warning` placeholders (BUG-56).
- **`token_configured` (D12):** automation status reports `true` only when an enabled provider's credential Secret key resolves to a non-empty value, and reports the result per provider (BUG-56).
- **Plan insert race (D12):** `save_plan` inserts with compare-and-set, so a concurrent request for the same deterministic plan never overwrites a plan that already advanced (BUG-56).
- **Policy truthfulness (D13):** missing rollout or HPA evidence no longer passes silently as `not_available` (BUG-55).
- **API key asked again after every reload (F13, BUG-63):** The dashboard keeps the key in `sessionStorage` for the current tab (never `localStorage`), clears it on `401` and on sign out, and tolerates blocked storage. The key dialog is a real sign-in form (read-only account `api-key`, password field with `autocomplete="current-password"`) so browsers and password managers can save and autofill it. After signing in, the current page reloads its data.
- **Page loads scheduled full scans and requests could not be cancelled (K9, BUG-25):** The dashboard and recommendations pages no longer send `refresh=true`; every read passes an `AbortController` signal that is aborted when the page is left or reloaded.
- **401 bypass in mutation helpers (K9, BUG-25):** Service configuration updates, apply, ignore, restore, dashboard refresh and report download go through the shared request path, so a `401` opens the sign-in dialog.
- **Inconsistent namespace filter (K9):** Ignored and applied recommendations now honour the selected namespace like the active list and savings.
- A controller that runs a later commit containing the merge (another PR, Renovate, image write-back) now confirms sync instead of failing with `revision_mismatch`/`sync_timeout`, so apply detection and savings run. The later commit must still carry the planned values in the planned files (blob IDs, then a structural YAML/JSON comparison only for files a later commit edited), so a revert or overwrite never leaves a plan stuck in `synced` and never revives a superseded `sync_failed` plan; such timed-out plans are retired with `planned_change_not_present` instead of being re-checked forever (D1, BUG-43).
- GitHub PRs closed without merging no longer keep their plan in `pr_open` forever because of GitHub's leftover test-merge SHA (D3, BUG-45).
- GitLab fast-forward and squash merges use the squash commit or the merge request head instead of failing sync with `merge_commit_sha_missing` (D4, BUG-46).
- Gitea branch creation reports a missing base branch instead of silently succeeding.
- A deleted or missing plan branch is reported as a branch mismatch that asks for a re-plan again, instead of a provider error from the new tree comparison (D5).
- Range CPU, memory, disk I/O and ephemeral-storage queries exclude the pod cgroup (`container=""`) and pause (`container="POD"`) series; the CPU fallback reads the pod cgroup series only once (A1, BUG-01).
- `run_range` no longer returns stored rows when any row exists in the range, and inclusive `BETWEEN` reads no longer make consecutive windows share a sample, so no window is skipped, double counted or shifted (A2, BUG-03).
- Multi-step ranges (backfills, `--last`) store each step's own CPU, memory, network, disk and restart values instead of one chunk-wide value on every row (A2).
- Metrics-server usage is attached only to the latest sample of a current window and only when it was measured during or after that sample; container rows keep their sample timestamp and step duration (A5, BUG-05).
- Range parsing drops NaN/Inf samples and clamps negative rates to 0 (A6, BUG-09).
- Pods whose cAdvisor series lack a `node` label are placed on their node via `kube_pod_info` instead of a single 10 W pseudo-node (A10, BUG-08).
- Hourly compression lost samples in every hour crossing the raw-retention boundary (non-aligned cutoff + overwrite upsert); it now compresses closed hours once, from their complete raw set, behind a watermark, and never re-reads compressed hours. (A3, BUG-02)
- Same-named pods in different namespaces (e.g. `redis-0`) shared one OpenCost cost. (A4, BUG-04)
- Stale carbon intensities were used without age limit and static defaults were written into the intensity history as if measured. (A8, BUG-07)
- Zone defaults that earlier versions stored in `carbon_intensity_history` (`estimation_method=default_fallback`) are deleted by migration 0039 and ignored by every intensity lookup and by the recompute, so an upgraded install no longer labels default-priced rows `realtime`. (A8, BUG-07)
- When the recompute moves a row from a default to a measured intensity, it drops the "Carbon intensity uses the ..." entry of the deprecated `estimation_reasons` and clears `is_estimated` when that was the only reason; on PostgreSQL the pod and container updates run in one transaction. (A9)
- Intensities prefetched at the raw sample time were labelled `realtime` and could differ from the value the hourly recompute applies; the calculation now uses the classified point at the normalized lookup time. (A8)
- The intensity recompute updated `combined_metrics` only; container rows now follow (set-based on SQLite and PostgreSQL), and PostgreSQL now refreshes `is_estimated` when a provider consolidates a value. (A9, BUG-06)
- **Failed outcomes kept accruing savings (C0.4, BUG-30):** recommendations that were not realized, are inconclusive, failed, are in rollback review or were reverted no longer accrue anything.
- **Ledger compression lost about half of the savings (C0.6, BUG-32):** ledger rows are no longer compressed or deleted. Hourly rows left by earlier releases are kept, flagged `legacy_compressed`, and reported in a `legacy_compressed` diagnostic.
- **Double counting on restart and failover (C0.7, BUG-33):** accrual no longer uses wall-clock periods.
- **One invalid record stopped all attribution (C0.8, BUG-34):** each recommendation is attributed in isolation and signed values are accepted.
- **Savings windows dropped their partial first hour (C0.14, BUG-40):** rows that overlap a window boundary contribute their overlapping share.
- **Silent fallback when the ledger is unreadable (C0.14, BUG-42):** `GET /recommendations/savings` now answers `503`.
- **Traffic normalization inflated savings ~4× (C0.1, BUG-27):** normalization is disabled until load-driver counterfactuals ship; the traffic ratio is a rate recorded for information only, and the unused `seasonality_factor` read is removed.
- **Multi-replica changes always went to rollback review (C0.2, BUG-28):** the usage gate compares the per-pod p95 with the applied per-pod request × headroom; restarts are per-pod deltas of the cumulative counter, new pods counted from their first observation.
- **Container-scoped (GitOps) changes were never measurable (C0.3, BUG-29).**
- **Retest-until-pass (C0.5, BUG-31):** `not_realized` changes are no longer re-evaluated every few hours with a growing window.
- **OOM, readiness and throttling gates were inert in production (C0.9, BUG-35).**
- **CPU and memory records each claimed the whole workload delta (C0.10, BUG-36),** including when the two records were re-measured or dismissed at different times, only one of them was re-measured, or apply detection saw them more than 15 minutes apart.
- **A blank rollback-review dismissal reason returned `500` (C0.11):** whitespace-only reasons are rejected with `422`; reasons are stored trimmed.
- **Ignore and unignore bypassed the state machine (C0.11, BUG-37):** an applied, verifying or verified change could be ignored, and unignore restored any status to `active`.
- **`mark_inconclusive` wrote a `verified` event and deleted the measured input (C0.11, C0.13, BUG-37).**
- **`applied_at` was the detection time and the baseline `captured_at` was "now" (C0.12, BUG-38).**

### Removed
- **CI: the dead README-badge step** and its `contents: write` permission (O6).
- **Repository: the stale, tracked `coverage.json`** is untracked and ignored (O6).

## [0.3.1] — 2026-10-25

### Added
- **GitOps plan workflow:** Persisted recommendation IDs now resolve to server-side GitOps bindings and render-validated raw-manifest, Kustomize, and a fail-closed subset of Helm plans. Users approve an exact preview digest; a lease-protected worker revalidates the source and opens or adopts an atomic multi-file PR using allowlisted, Secret-backed GitHub/GitLab/Gitea providers.
- **Immutable provider archive snapshots:** GitHub, GitLab, and Gitea source snapshots are fetched at a full resolved commit SHA through authorized provider calls, streamed under the compressed-size cap, and passed through the existing traversal/symlink/decompression limits. Cross-origin redirects never receive provider authorization headers; unsupported archive access fails closed without switching to a mutable ref.
- **Isolated renderer service:** Helm/Kustomize receive bounded repository snapshots in a separate Pod without database/provider Secrets or a service-account token. Rendering fails closed when no isolated backend is available; the renderer egress-deny NetworkPolicy requires an enforcing CNI. An optional per-Pod firewall init container is available on clusters that permit `NET_ADMIN`. The renderer uses a dedicated UID/GID to avoid host-wide process-limit collisions.
- **GitOps automation policy:** Plan creation and approval enforce configurable risk, confidence, sample/coverage, open-PR budget, cooldown and change-freeze gates, with the result persisted for review.
- **Git provider lifecycle recovery:** The signed webhook endpoint authenticates provider-native signatures/tokens, persists deduplicated events and reconciles closed PRs against provider state. The plan worker also polls bounded pages of linked open PRs under a fenced database-backed singleton lease; provider calls are time-bounded and grouped recommendation PR records transition together. Stale pending attempts without immutable plan/provider/PR references are quarantined, while linked attempts remain provider-polled. Deterministic plan branches are removed only after terminal state and confirmation that no open PR uses the branch.
- **Periodic controller discovery:** Argo CD and Flux bindings refresh at startup and periodically. Flux HelmRelease matching now receives a bounded, complete live Deployment/StatefulSet/DaemonSet inventory.
- **Manual GitOps binding resolution:** Operators can resolve an existing ambiguous/unresolved workload by approving one current server-provided candidate. The resolution is source-generation-bound, expires at the binding TTL, writes a durable audit event, and is exposed through a protected API and the Automation & GitOps page.
- **GitOps sync and apply gating:** A controller runtime compares exact Argo CD/Flux source revisions and health to the merged commit. The worker persists an immutable pre-apply target UID/generation/per-container request baseline before provider mutation. Merged actions enter `sync_pending`, confirmed plans transition to `synced`, and a timeout transitions stalled plans to `sync_failed`. Typed apply and rollback confirmation require an observed post-baseline resource change and exact per-container requests; a recreated UID is accepted only after its current generation is observed.
- **GitOps operations telemetry:** The API and dedicated production worker expose authenticated `/prometheus/metrics` endpoints for their process-local registries. Metrics cover bounded polling/lease, plan/operation latency and queue age, PR lifecycle, webhook lag, discovery coverage/freshness, sync outcomes, stuck/failed backlog counts, and worker-heartbeat freshness; labels are constrained to fixed categories and omit repository, workload, plan, PR, credential, and error-text values. Optional Prometheus Operator alerts cover stuck operations/plans, sync failures, webhook deliveries, and stale workers.
- **Owner-aware recommendation identity:** Active recommendation identity includes workload owner kind/name so same-named workloads do not collide. Migration `0037` preserves unidentified legacy rows and quarantines duplicate active legacy identities with an audit record.
- **Safer GitOps target identities:** Flux post-render selectors escape resource-name regex characters while remaining compatible with older managed selectors. OCI repository normalization preserves host/path identity without retaining userinfo or query parameters.
- **Live policy evidence:** Argo CD, Flux and raw workload bindings now carry current workload labels, rollout state, GitOps controller suspend/health, and HPA/PDB interaction evidence. Configured change-freeze labels block only matching workloads, and rollout/controller/HPA/PDB gates fail closed with `not_available` when discovery cannot collect their evidence.
- **Configurable discovery cadence:** `automation.discoveryInterval` (`GITOPS_DISCOVERY_INTERVAL`) and `automation.bindingTtl` (`GITOPS_BINDING_TTL`) are wired through Helm and the API, with matching binding-freshness projections in SQLite and PostgreSQL.
- **Automation readiness detail:** `GET /api/v1/automation/status` now reports worker heartbeat health, queue depth, plan backlog, discovery age/coverage, renderer configuration and provider preflight/capability state.
- **Bounded recommendation scans:** concurrent `refresh=true` requests coalesce into one active scan; the lookback window is shortened before loading when combined and container rows exceed `RECOMMENDATION_MAX_METRIC_ROWS`; scan window, row count and peak RSS metrics are exposed.
- **Retention for audit events:** automation retention now also prunes `recommendation_events` and `workload_binding_events`.
- **Validated rollback flow:** RAW rollback is built in; Kustomize rollback requires an explicitly injected exact-target renderer; Argo CD `HELM_VALUES` rollback is supported only for the explicitly mapped local-chart subset; Flux `HELM_RELEASE` rollback is limited to Git-owned manifests with local same-revision charts, inline values, and GreenKube-managed post-renderers. Unsupported layouts fail closed. A rollback preview links to the original plan, restores only files still matching its exact forward output, and uses the normal digest approval and PR worker path. A recommendation becomes `reverted` only after the rollback PR is synced and the post-baseline workload generation and original per-container requests are observed.
- **Apply detection & verification (optimization engine Phase 3):** Recommendation lifecycle is split into "apply succeeded" (`applied_at`, `application_method`) and "recommendation succeeded" (`verified_at`, `verification_status`). A scheduled lifecycle job (default every 5 min in the collector container) detects landed changes through the Kubernetes API (`AppliedDetector`), observes the result for `VERIFICATION_WINDOW_HOURS` and evaluates cost, carbon and workload-health gates (`RecommendationVerifier`). Outcomes are `verified`, `rollback_review`, `inconclusive` or `applied` with `savings_realized=false`. Every transition is recorded in the new `recommendation_events` audit trail (`GET /api/v1/recommendations/{id}/events`), and recommendations now expire (`expired` status) after `RECOMMENDATION_TTL_DAYS`. Migration `0012` adds the columns and events table for SQLite and PostgreSQL.
- **GitOps PR bot (optimization engine Phase 4):** `POST /api/v1/recommendations/{id}/apply-pr` now resolves sources from native Argo CD/Flux discovery and persisted workload bindings; workload Git annotations are not required. A workload-level plan can combine resource changes across multiple containers into one PR. Argo Helm values mappings are inferred only when a render probe proves a unique path; ambiguous cases offer a server-validated one-time mapping stored only with that plan, and the optional `greenkube.cloud/helm-values-mapping` annotation remains an explicit override. PRs include evidence, impact, risk, source/controller provenance, rendered diffs and verification expectations. Provider tokens use namespaced Secret references and repository allowlists. The plan dialog provides focus trapping and restores focus when closed. Legacy `config.recommendations.git.*` and `secrets.gitToken` values are retired; see the [provider registry upgrade guide](docs/configuration.md#upgrading-from-the-legacy-single-provider-configuration).
- **Measured savings ledger (optimization engine Phase 5):** Ledger rows now carry `measurement_method` (`prorated`/`measured`), baseline/actual values and confidence; verified recommendations write measured savings, rollback reviews supersede prior rows, and the API/dashboard separate **measured (verified)** from **projected (prorated)** totals. Migration `0014` extends both ledger tables. New `greenkube_savings_measured_vs_projected_ratio` gauge.
- **Real VPA and Karpenter connectors (optimization engine Phase 6):** The VPA source is enabled by default and auto-detects the CRD; the Karpenter source now produces NodePool consolidation recommendations from live NodePool/NodeClaim state. Both degrade gracefully when their CRDs are absent.
- **Optimization engine — unified recommendation orchestration:** Recommendation generation is now centralized in a single `OptimizationEngine` (`src/greenkube/core/optimization/`) used by the API, the startup scan and the CLI. The engine builds one `OptimizationContext`, runs every enabled source, deduplicates/arbitrates, enriches and ranks the results, then persists them. The legacy `Recommender` façade and its energy-based methods were removed. The actionable flow is documented in [GitOps automation](docs/automation.md).
- **Multi-source recommendations with provenance:** New `RecommendationSource` adapters. A VPA connector reads recommendation-mode VerticalPodAutoscalers (`updateMode: Off`, `status.recommendation.containerRecommendations`) and a Karpenter connector is reserved. When VPA is enabled, its CPU/memory targets replace the native rightsizing recommendation for the same workload — duplicates are never shown. Recommendations now carry `source`, `source_ref`, `sources` (provenance), `capability` and `owner_kind`/`owner_name`. Enable with `RECOMMENDATION_VPA_ENABLED=true` (Helm `config.recommendations.vpaEnabled`).
- **Review-grade evidence on every recommendation:** Each recommendation now includes a structured evidence block — observation window, sample count/coverage, current vs proposed requests, utilization distribution (avg/p50/p90/p95/p99/max), restart count, expected savings and method, confidence with factors, reliability risk with factors, rollback conditions, a machine-readable `patch` action plan and an expiry date. Full detail is exposed by `GET /api/v1/recommendations/{id}` and rendered in the dashboard.
- **Multi-criteria ranking:** Recommendations are scored on carbon/cost impact, confidence, risk, effort, actionability and source authority. Profiles (`balanced`, `carbon_first`, `cost_first`, `quick_wins`, `low_risk`) are available via `GET /api/v1/recommendations/top?profile=...`; each row exposes its `ranking_score` and `ranking_factors` breakdown.
- **Risk, confidence and effort assessment:** New heuristics derive `risk_level` (low/medium/high) with explainable `risk_factors`, a 0–1 `confidence` based on samples/coverage/source authority, and `effort`. Dashboard cards show source, risk and confidence badges plus an expandable evidence panel.
- **Migrations `0010` (sources/provenance/owner) and `0011` (evidence/risk/ranking/patch/expiry)** for both SQLite and PostgreSQL. A shared row mapper (`storage/recommendation_mapper.py`) and shared column/value builders now back both repository implementations.
- **Helm:** new `config.recommendations` values (`vpaEnabled`, `karpenterEnabled`, `sourcePriority`, `ttlDays`, `rankingProfile`, `rankingWeights`, `minSamples`) and read-only RBAC for `verticalpodautoscalers` and Karpenter `nodepools`/`nodeclaims`.
- **Helm image digest pinning:** new `image.digest` value renders `repository@sha256:<digest>` and takes precedence over `image.tag`, so releases can be pinned to exact image bytes without splitting the digest across `image.repository` and `image.tag`.

### Changed
- **Documentation:** Added a complete installation guide and a user-facing GitOps automation guide, updated the configuration/API/architecture/recommendation references, refreshed the README and frontend screenshots, and removed the personal `docs/specs` material from version control.
- **Single orchestration path:** The duplicated metric/side-input loading previously present in the API router, startup scan and CLI now lives in `OptimizationContext`/`ContextBuilder`. Source failures are isolated and degrade gracefully.
- **SQLite/PostgreSQL recommendation repositories:** Insert/update statements are generated from a shared column definition, removing placeholder drift between backends.
- **Verification rates are sample-normalized:** before/after cost and carbon rates now use the observed sample duration (sample count x query step) instead of the wall-clock window, so verification behaves correctly when collection is sparse (e.g. a freshly installed instance).

### Removed
- **Elasticsearch storage backend:** The experimental Elasticsearch backend has been removed, including `storage/elastic/`, the `setup_elasticsearch` wiring, the `ELASTICSEARCH_*` configuration and Helm values, and the optional `greenkube[elasticsearch]` extra. `DB_TYPE` now accepts only `sqlite` and `postgres`.
- **Interactive CLI report and recommendation commands:** `greenkube report` and `greenkube recommend` have been removed, together with the `exporters`, `reporters` and `models.cli` modules and the `--no-color` global flag. Reporting and recommendations are available exclusively through the REST API and the dashboard. The CLI now only exposes `start`, `demo` and `version`.

### Fixed
- **Exhausted operation recovery:** a periodic plan reconciler now fails an `applying` plan and releases its recommendation action when the linked operation is terminal and no pull request exists, instead of requiring database surgery.
- **Consistent repository allowlists:** discovery and provider authorization now share segment-aware glob matching, so `acme/*` no longer resolves `acme/team/repo` during discovery and then fail during plan creation.
- **Argo CD tracking-ID fallback:** the fallback now reads the managed live resource annotation instead of only the Application metadata.
- **Apply detection no longer fires on missing requests:** a live workload with no CPU/memory request (observed `0`) is not treated as an applied change.
- **Verified cost-only savings are attributed:** a verified recommendation with zero measured CO2e but positive measured cost now writes a `measured` ledger row instead of being skipped.
- **Negligible carbon projections do not block verification:** carbon projections below 1 gCO2e/year are treated as noise and skip the carbon gate.
- **Helm: the collector waits for PostgreSQL** with an init container (`wait-for-postgres`) when the bundled database is enabled, instead of crash-looping on the first connection attempt during a fresh install.
- **Helm image references fail fast:** a `sha256:` digest passed through `image.tag` (which Kubernetes reports as `InvalidImageName`) is rejected at render time with a pointer to `image.digest`; malformed digests and digest/repository conflicts are rejected too.
- **Helm database credential safety:** a pre-upgrade hook verifies `DB_CONNECTION_STRING` against the bundled PostgreSQL before rolling Pods, and a fresh install fails early when a retained PostgreSQL PVC exists without its Secret. Both guards print the exact recovery command instead of leaving GreenKube in `CrashLoopBackOff` with `password authentication failed for user "greenkube"`.

## [0.3.0] — 2026-08-30

### Added
- **Orphaned LoadBalancer recommendation:** New `ORPHANED_LOAD_BALANCER` recommendation type that flags Services of type `LoadBalancer` that have no ready backing endpoints. A new `LoadBalancerCollector` lists all Services and Endpoints from the Kubernetes API and reports LoadBalancer Services whose selector matches no pods — the provisioned cloud LoadBalancer keeps billing hourly while routing traffic to nothing. Recommendations carry the Service name (`pod_name`) and namespace and suggest deleting the Service. Projected cost savings prefer the real per-Service LoadBalancer cost reported by OpenCost (`OpenCostCollector.collect_lb_costs()` aggregates allocations by `service` and reads the `loadBalancerCosts` map, annualized from the observation window); when OpenCost is unreachable or has no cost data, they fall back to the new `LOAD_BALANCER_COST_PER_MONTH` config (Helm: `config.recommendations.loadBalancerCostPerMonth`, default `$18.00`/month). CO2e savings are not projected because energy estimation currently only covers CPU usage. Wired into the API, startup scan, CLI, demo data, and the frontend (new type badge on the recommendations page and dashboard). The Helm chart `ClusterRole` now also grants read access to `endpoints`; existing releases need `helm upgrade` to pick up the new RBAC rule.
- **Orphaned PersistentVolume recommendation (#244):** New `ORPHANED_PERSISTENT_VOLUME` recommendation type that flags PersistentVolumes whose claim is gone. A new `PVCollector` lists all PVs and PVCs from the Kubernetes API and reports volumes that are in `Released` phase (PVC deleted but not reclaimed) or whose `claimRef` references a non-existent PVC. Recommendations carry the PV name (cluster scope) and suggest deleting the volume to release provisioned storage. Projected cost savings prefer the real per-volume storage cost reported by OpenCost (`OpenCostCollector.collect_pv_costs()` aggregates allocations by `persistentvolume` and annualizes the window cost); when OpenCost is unreachable or has no cost data, they fall back to the provisioned capacity using the new `STORAGE_COST_PER_GIB_MONTH` config (Helm: `config.recommendations.storageCostPerGibMonth`, default `$0.10`/GiB-month). CO2e savings are not projected because energy estimation currently only covers CPU usage. Wired into the API, startup scan, CLI, demo data, and the frontend (new type badge on the recommendations page and dashboard). The Helm chart `ClusterRole` now grants read access to `persistentvolumes` and `persistentvolumeclaims`; existing releases need `helm upgrade` to pick up the new RBAC rule.
- **Carbon intensity consolidation refresh:** After each hourly intensity collection, all combined metrics in the last 24 hours are recomputed against the refreshed history (`CombinedMetricsRepository.recompute_carbon_with_latest_intensities`, with a single bulk `UPDATE` on PostgreSQL). Provisional provider values (e.g. Wattnet data younger than ~4h, `valid=false`) are corrected in place once consolidated — metrics that already had an intensity are updated too, not only those without one.
- **Wattnet electricity provider:** Grid carbon intensity is now provider-agnostic via a new `BaseElectricityProvider` abstraction. A `WattnetCollector` fetches 15-minute carbon footprint data for 52 European zones from the EU-funded [Wattnet](https://wattnet.eu) API (Bearer token auth with automatic refresh). Select it with `ELECTRICITY_PROVIDER=wattnet` plus `WATTNET_EMAIL`/`WATTNET_PASSWORD` (Helm: `config.electricityProvider`, `secrets.wattnetEmail`, `secrets.wattnetPassword`). Zones outside Europe gracefully fall back to the default intensity map. See [docs/wattnet.md](docs/wattnet.md) for full details and the water-footprint roadmap.
- **Wattnet health check & runtime config:** `wattnet` service in `GET /api/v1/health/services`; Wattnet credentials can be updated at runtime from the Settings page and are persisted to the Kubernetes Secret (`wattnet_email`/`wattnet_password` in `POST /api/v1/config/services`).
- **Frontend:** Settings page and health popup now expose Wattnet credential fields.

### Fixed
- **Health status: inactive providers excluded** — Health checks for non-selected electricity providers (e.g. Electricity Maps while `ELECTRICITY_PROVIDER=wattnet`) now report as `inactive` and are excluded from the overall health status and from the frontend startup health popup, so users are no longer nagged to configure a provider they are not using.
- **Config: token warnings only for the active provider** — The startup warning for a missing `ELECTRICITY_MAPS_TOKEN` is now emitted only when Electricity Maps is the active provider; the Wattnet credentials warning only fires when Wattnet is selected.
- **Security: dependency upgrades to fix Trivy scan errors** — `aiohttp` upgraded `3.14.1` → `3.14.3`; `structlog` promoted to a runtime dependency (`26.1.0`); frontend transitive dependencies upgraded (`@sveltejs/acorn-typescript`, `acorn`, `brace-expansion`, `js-yaml`, `nanoid`, `postcss`, and others) to remediate Trivy-flagged vulnerabilities.

## [0.2.12] — 2026-06-26

### Added
- **Auth-proxy routing (#245):** `TRUST_AUTH_PROXY` config flag enables forwarding authenticated requests through an external auth proxy (Authentik, Caddy). When enabled, the API reads the authenticated user identity from the `X-Forwarded-User` / `X-Auth-Request-User` headers instead of performing its own authentication, removing the need to expose credentials directly to the proxy. Special thanks to [@kahnwong](https://github.com/kahnwong) for the detailed reproduction information that led to this fix.
- **Structured logging with `structlog`:** A new `LOG_FORMAT` config option (`json` or `text`, default `text`) controls the log output format. JSON mode emits machine-readable structured log lines suitable for log aggregation pipelines (e.g. Loki). Exposed in Helm `values.yaml` via `config.logFormat`.
- **DB vacuuming and savings-ledger pruning:** `MetricsCompressor` now runs `VACUUM` on the main metrics tables after each compaction cycle. A dedicated pruning pass removes stale hourly-savings-ledger records older than the configured retention window, keeping storage usage bounded on long-running deployments.
- **Metrics: DB-level pagination for `GET /api/v1/metrics`** — The endpoint now uses SQL `COUNT(*) + LIMIT/OFFSET` instead of loading the full result set into Python. A new `METRICS_LIST_MAX_RANGE_DAYS` config (default 30, exposed in Helm `values.yaml` via `config.metricsListMaxRangeDays`) rejects requests wider than the limit with HTTP 400 to prevent accidental OOM on large clusters.
- **Repository: `read_combined_metrics_page`** — New method on `CombinedMetricsRepository` (base Python fallback + optimised PostgreSQL `UNION ALL` implementation) for DB-level paginated reads.
- **Repository: `read_latest_per_pod`** — New method returning the single most-recent metric snapshot for each `(namespace, pod_name)` pair. PostgreSQL implementation uses `DISTINCT ON` for efficiency; base class falls back to Python deduplication.
- **Repository: `aggregate_grouped_row_count`** — New method returning the number of distinct `(group_key × time_bucket)` rows a grouped-aggregate export would produce, computed entirely in SQL (`COUNT DISTINCT`). Implemented for PostgreSQL and SQLite.
- **Metrics endpoint: Prometheus gauge refresh via `read_latest_per_pod`** — `refresh_metrics_from_db` now calls `read_latest_per_pod` instead of loading the full metrics history and deduplicating in Python, eliminating a major OOM source for large clusters.

### Changed
- **Toolchain: switched from `pip` to `uv`** — All CI workflows, the Dockerfile, and developer documentation now use `uv` for dependency management and virtual-environment creation. A `uv.lock` lockfile replaces the previous `requirements*.txt` files, ensuring fully reproducible builds across environments.
- **Settings: replaced `python-dotenv` with `pydantic-settings`** — `config.py` is now driven by `pydantic-settings` `BaseSettings`, giving automatic environment-variable parsing with type coercion, validation, and cleaner secret handling. `python-dotenv` removed from dependencies.
- **Type checking: Pyrefly added to pre-commit** — Pyrefly static type checker integrated into the pre-commit hook chain. Codebase cleaned up (dead code, redundant imports, incorrect annotations) as part of the initial Pyrefly pass.
- **Frontend: metrics page removed** — The `/metrics` route and its associated Svelte page have been removed. Dashboard panels reordered accordingly.

### Fixed
- **API: oversized response headers causing Caddy/Authentik 502/503 errors** — Bloated debug and CORS headers removed from all API responses; the fix resolves `upstream sent invalid header` 50x errors seen behind reverse-proxy setups.
- **Frontend: donut chart legend overflow** — Namespaces beyond the top-N are now collapsed into an `other` slice, preventing the chart legend from overflowing the panel.
- **Ruff version mismatch in CI** — Pinned `ruff` version in CI to match the local configuration, eliminating lint-step failures caused by version skew.
- **OOM (`OOMKilled`, exit code 137) in `greenkube-api`** — `GET /api/v1/metrics`, `GET /api/v1/report/summary?aggregate=true`, and the Prometheus gauge refresh all previously loaded up to 1 M+ `CombinedMetric` objects into memory. All three paths are now SQL-backed and never materialise full row sets in Python (#239).
- **`report/summary?aggregate=true` uses pure SQL** — The aggregate summary path calls `aggregate_grouped_row_count` (SQL `COUNT DISTINCT`) instead of invoking `aggregate_metrics()` over loaded rows, making large date ranges safe regardless of dataset size.
- **Memory leaks in Kubernetes API client** — `NodeCollector` and `PodCollector` now explicitly close the async `kubernetes_asyncio` API client after each collection cycle, preventing handle leaks on long-running pods.
- **Security: frontend dependency updates** — `package.json` / `package-lock.json` updated to remediate Trivy-flagged vulnerabilities in transitive frontend dependencies.

## [0.2.11] — 2026-05-26

### Added
- **Grafana: Streamlined dashboard layout** — Dashboard rebuilt to 4 sections and 9 panels: *GreenKube Impact Command Center* (radar, footprint mix, impact ledger, action priorities), *CO₂e by Namespace* (pie charts, now promoted to second position above the map), *Regional Node Cleanliness* (geomap), and *Top Emitters & Spenders* (top-15 pods by CO₂e and cost). Eight rows and their associated panels removed.
- **Recommendations: Annualised savings** — Each recommendation now exposes projected annual CO₂e savings (`annual_co2e_savings_grams`) and annual cost savings (`annual_cost_savings_usd`), extrapolated from the observation window. Values are surfaced in the API response, the frontend recommendations page, and CLI output.
- **Report: Yearly and custom date ranges** — Report endpoints accept relative windows and arbitrary `start`/`end` timestamps. Reports can additionally be grouped by namespace with `aggregate=true&group_by=namespace`.
- **Recommendations: Savings ledger integration (#237)** — Applied recommendations now trigger a savings-ledger entry. A new flow updates `RecommendationRealization` records and kicks off a background refresh of the savings attribution so the `greenkube_co2e_savings_attributed_grams_total` and `greenkube_cost_savings_attributed_dollars_total` gauges stay consistent as metrics accumulate.
- **Recommendations: `GET /api/v1/recommendations/top`** — New API endpoint returning the highest-impact active recommendations ranked by projected annual savings. Returns `TopRecommendation` DTOs with rank, projected annual CO₂e savings, and projected annual cost savings.
- **Prometheus: `greenkube_top_recommendations` gauge** — New Prometheus gauge exposing ranked active recommendations.
- **Grafana: Actionable Recommendations section** — New dashboard row showing the top-N ranked recommendations as horizontal bar cards with projected annual CO₂e and cost savings.
- **App startup recommendation scan:** The API now triggers a full scan for recommendations on startup, ensuring the dashboard and API have immediate data to work with even if no new metrics have come in since the last scan.

### Changed
- **Grafana: `$node` and `$region` template variables removed** — Both variables have been dropped from the dashboard.
- **Grafana: Dashboard generator cleaned up** — Unused panel helpers removed from `scripts/build_grafana_dashboard.py`, together with the constants no longer referenced by any remaining panel.

### Fixed
- **Recommendations: CPU and memory rightsizing** - Stop displaying rightsizing recommendations that suggest increasing requests.
- **Security: Frontend dependency updates** — `package.json` / `package-lock.json` updated to remediate Trivy-flagged vulnerabilities in transitive frontend dependencies.

## [0.2.10] — 2026-05-07

### Added
- **Recommendation full lifecycle:** Recommendations now support a complete status lifecycle (`open`, `in_progress`, `resolved`, `dismissed`, `snoozed`). New API endpoints allow updating status, bulk-dismissing, and snoozing recommendations. DB migrations `0006` (lifecycle columns) and `0007` (upsert null-fix) applied for both PostgreSQL and SQLite.
- **Frontend recommendation lifecycle UI:** The recommendations page now exposes status filters, per-recommendation status controls (dismiss, snooze, mark in-progress/resolved), and a lifecycle summary on the dashboard.
- **Recommendation deduplication by deployment:** The recommender now groups pods by their parent Deployment (via regex matching the standard ReplicaSet pod-name suffix) before generating rightsizing and off-peak recommendations. This avoids duplicate entries for every pod in a Deployment and produces one consolidated recommendation per workload.
- **Expanded test coverage:** Test suite extended with new files covering `CollectionOrchestrator`, `MetricAssembler`, `MetricsCompressor`, `Scheduler`, recommender v2, factory, `SummaryRepository` (SQLite), `TimeseriesCacheRepository` (SQLite), a full recommendation lifecycle end-to-end suite, and additional node repository / recommendation repository unit tests.
- **Real database integration tests:** New `tests/integration/test_real_database_repositories.py` runs the full repository layer against live SQLite and PostgreSQL instances. `docker-compose.test.yml` spins up a throwaway PostgreSQL container for CI. Documentation added in `docs/testing.md`.
- **Grafana dashboard overhaul (`scripts/build_grafana_dashboard.py`):** Complete rebuild of the Grafana JSON dashboard generation script with full PromQL aggregation correctness, instant-query bargauges for Top 3 panels, and a `reduce` transformation to fix bar-scale inflation from historical data.
- **New Prometheus metrics:** Additional Prometheus gauges for recommendation counts by status, attributed savings, and node activity; exposed via `src/greenkube/api/metrics_endpoint.py`. `ServiceMonitor` updated to include the new scrape path. Grafana dashboard v2 built on these metrics with corrected PromQL and consistent namespace/cluster filters across all panels.
- **Savings attribution system:** New `SavingsAttributor` service (`src/greenkube/core/savings_attributor.py`) prorates projected annual CO₂e and cost savings to the actual observation window. New `SavingsLedger` Pydantic model, abstract `BaseSavingsRepository`, and PostgreSQL/SQLite implementations. DB migrations `0008` applied for both engines. Two new Prometheus gauges: `greenkube_co2e_savings_attributed_grams_total` and `greenkube_cost_savings_attributed_dollars_total`.
- **Grafana dashboard: Sustainability Command Center row** with Sustainability Score gauge, CO₂e/Cost/Energy stats, attributed savings window panels, active-recommendation counters, and three sorted Top-3 bargauges (CO₂e by namespace, Cost by namespace, Recommendation Types).
- **Demo mode "GreenOptic":** New comprehensive demo mode simulating a fictional `GreenOptic` company with a fully pre-populated dataset — realistic node topology, multi-namespace workloads, historical metrics, and a backfilled savings ledger aligned with resolved demo recommendations. DB migration `0009` adds an `is_active` activity-status column to node records. New `RecommendationRealization` service (`src/greenkube/core/recommendation_realization.py`) links realised savings to specific recommendations in the ledger.
- **Redesigned frontend report page:** The `/report` page rebuilt with a new `reportOptions.js` module for configurable report parameters and a new `date_utils.py` helper on the backend. Report layout and export logic simplified.

### Fixed
- **Node recommendation memory usage:** The underutilised-node recommender now correctly factors in memory utilisation alongside CPU, preventing false positives on memory-heavy workloads.
- **Node-level recommendation persistence:** `pod_name` and `namespace` are now allowed to be `NULL` in the DB schema for node-scope recommendations, fixing an integrity error on save.
- **Embodied emissions default reduced to 100 kg:** `DEFAULT_EMBODIED_EMISSIONS_KG` lowered from 350 kg to 100 kg to better match the average embodied footprint of a cloud VM vCPU slice, producing more accurate Scope 3 estimates when Boavizta returns no data.
- **Settings page stale warning removed:** The frontend settings page no longer displays a warning about configuration not persisting, since settings are now written to the Kubernetes Secret and survive pod restarts.
- **Grafana: coverage metrics removed from GreenKube impact panel:** Test-coverage Prometheus metrics were incorrectly included in the GreenKube impact/emissions panel and have been removed.
- **Grafana dashboard: filters applied to all charts:** Namespace and cluster variable filters are now consistently wired to every panel in the dashboard, so selecting a namespace correctly scopes all charts rather than only some.

### Changed
- **CI: automated test-coverage badge update:** The README test-coverage shields are now refreshed automatically by CI on each push to `dev`.
- **All Grafana PromQL expressions deduplicated:** Every expression across all rows uses the appropriate aggregation (`sum(max by (cluster)(…))` for cluster-level scalars, `sum by (namespace)(…)` for namespace breakdowns, `max by (namespace, pod)(…)` for pod-level topk, `max by (node)(…)` for node metrics) to prevent value multiplication from multiple scrape instances.
- **Documentation restructured:** `README.md` streamlined to a project overview; detailed reference content moved to dedicated files — `docs/api.md`, `docs/cli.md`, `docs/configuration.md`, and `docs/prometheus-grafana.md`.

## [0.2.9] — 2026-04-21

### Added
- **Frontend config persistence via K8s Secret (#219):** UI-applied settings (`PROMETHEUS_URL`, `OPENCOST_API_URL`, `ELECTRICITY_MAPS_TOKEN`, `BOAVIZTA_API_URL`) are now patched into the GreenKube Kubernetes Secret immediately after being saved, so they survive pod restarts and `helm upgrade --reuse-values` without manual intervention. A namespaced `Role`/`RoleBinding` grants the service account `get`+`patch` access to exactly the GreenKube Secret (no cluster-wide secret access).
- **GreenKube favicon:** The browser tab now displays the real GreenKube logo (`favicon.ico`) instead of the Svelte placeholder SVG. The SVG favicon reference has been removed from `app.html` and `build/index.html`; `favicon.ico` is served with the correct `image/vnd.microsoft.icon` MIME type.
- **`GET /api/v1/metrics/by-namespace`:** New lightweight endpoint returning CO2e, embodied emissions, energy, and cost aggregated by namespace over a time window. Queries both `combined_metrics` (raw) and `combined_metrics_hourly` (archived) tables via a single `UNION ALL + GROUP BY` — avoids loading full row sets into memory.
- **`GET /api/v1/metrics/top-pods`:** New lightweight endpoint returning the top-N pods by CO2e over a time window, also using the dual-table `UNION ALL + GROUP BY` pattern. Dashboard donut and top-pods charts now call these two endpoints instead of the expensive `GET /metrics` route, eliminating OOM restarts when browsing large time ranges.
- **GHG Scope 2 / Scope 3 carbon classification:** Emissions are now formally categorised per the GHG Protocol Corporate Standard.
- **Pre-computed dashboard cache (`metrics_summary` + `metrics_timeseries_cache`):** Two new database tables (migrations `0004` and `0005` for PostgreSQL and SQLite) store pre-aggregated KPI scalars and time-series buckets for five fixed windows (`24h`, `7d`, `30d`, `1y`, `ytd`). Tables are refreshed hourly by the background scheduler, eliminating full-table scans on every dashboard load and preventing OOM errors on large datasets.
- **`SummaryRefresher`:** New `src/greenkube/core/summary_refresher.py` service that computes cluster-wide and per-namespace KPI totals and time-series buckets, then upserts them into the two cache tables. Supports adaptive granularity per window (hourly / daily / weekly / monthly buckets).
- **`SummaryRepository` and `TimeseriesCacheRepository`:** New abstract base classes in `storage/base_repository.py` with PostgreSQL and SQLite implementations.
- **Dashboard API endpoints:** Three new FastAPI routes for the pre-computed tables:
  - `GET /api/v1/metrics/dashboard-summary` — cached KPI scalars, optionally filtered by namespace.
  - `GET /api/v1/metrics/dashboard-timeseries/{window_slug}` — cached time-series buckets for `24h`, `7d`, `30d`, `1y`, or `ytd`.
  - `POST /api/v1/metrics/dashboard-summary/refresh` — trigger an on-demand background refresh (HTTP 202 Accepted).
- **`MetricsSummaryRow` and `TimeseriesCachePoint` Pydantic models:** New DTOs in `src/greenkube/models/metrics.py` representing rows from the two cache tables.
- **Adaptive chart granularity (frontend):** Dashboard charts now select the optimal time bucket per window — hourly for `24h`, daily for `7d`/`30d`, weekly for `1y`, monthly for `ytd` — resulting in consistently readable x-axes regardless of the selected range.
- **Boavizta fallback with configurable default:** When the Boavizta API does not recognise a cloud provider or instance type (returns no data), `EmbodiedEmissionsService` now injects a fallback embodied-emissions profile using `DEFAULT_EMBODIED_EMISSIONS_KG` (default: **350 kg CO2e**) instead of silently using 0 g, which was incorrect. The resulting `CombinedMetric` is flagged `is_estimated=True` with a descriptive `estimation_reasons` entry. Exposed as `config.boavizta.defaultEmbodiedEmissionsKg` in `values.yaml` and `DEFAULT_EMBODIED_EMISSIONS_KG` in `configmap.yaml`.
- **`EmbodiedEmissionsService.is_embodied_fallback()`:** New helper method returns `True` when a node's cached profile was produced by the fallback rather than a real Boavizta response, enabling the metric assembler to set estimation flags accurately.

### Fixed
- **Async K8s Secret patching (`kubernetes_asyncio`):** The in-cluster Secret patch now correctly uses `kubernetes_asyncio` (the async client that is actually installed) instead of the sync `kubernetes` package. `load_incluster_config()` is called without `await` (it reads files synchronously); failures are caught and logged without interrupting the API response.
- **Elasticsearch removed from production dependencies:** `elasticsearch` and `elasticsearch-dsl` packages moved to an optional extra (`pip install greenkube[elasticsearch]`). All imports are now lazy (loaded only when the ES storage backend is actually selected), removing heavy transitive dependencies and startup warnings for users on PostgreSQL or SQLite.
- **Trivy KSV-0109 false positive:** `GREENKUBE_SECRET_NAME` is a resource name, not a secret value — suppressed in `.trivyignore` with justification. `KSV-0113` (Role granting secret access) also documented as intentional for the UI persistence feature.
- **Electricity Maps API not called for OpenStack-based providers (zone = `nova`):** The scheduler's carbon-intensity collection loop now falls back to the node's geographic region when the provider-specific zone identifier is not a recognised Electricity Maps zone code. This restores carbon-intensity data collection on OVH, Infomaniak, and similar OpenStack-based clouds where the K8s node zone label is set to `nova` rather than a country/region code.
- **Race condition in collection orchestrator:** `CollectionOrchestrator` no longer collects nodes internally. Node collection is now an explicit Phase 1 in `DataProcessor.run()` that runs alone before any concurrent collection, preventing shared Kubernetes API client races and the cascade of Electricity Maps API errors they caused.
- **`DEFAULT_ZONE` spurious warning:** The `NodeZoneMapper` no longer emits a warning when the zone was actually resolved correctly — the warning was incorrectly triggered even when a valid `DEFAULT_ZONE` was set.
- **Pod CPU utilisation aggregation per node:** `CollectionOrchestrator` was averaging pod CPU usage per node across timestamps instead of summing, causing underestimated energy figures on nodes with multiple measured pods.
- **Chart legends overlapping (frontend):** ECharts legend layout fixed to prevent label overlap on small viewports.

### Changed
- **`DataProcessor.run()` pipeline restructured into four explicit phases:** Phase 1 (node discovery, sequential), Phase 2 (zone resolution), Phase 3 (parallel metrics + Boavizta), Phase 4 (carbon-intensity prefetch + assembly). This eliminates the previous race condition and removes the redundant second `collect_instance_types()` K8s call that used to happen at the end of the pipeline.
- **`CollectionOrchestrator` simplified:** `NodeCollector` dependency removed; node enrichment for Prometheus instance-type labels now uses the `nodes_info` dict passed in from Phase 1, avoiding any duplicate K8s API calls.

## [0.2.8] — 2026-04-11

### Security
- **Dockerfile hardening:** Base image for the frontend build stage upgraded from `node:20-alpine` to `node:22-alpine`. Both the builder and final runtime stages now run `apt-get upgrade` at build time to patch known OS CVEs (libssl3, zlib1g, ncurses, libc). The final image user (`greenkube`, UID/GID 10001) is created with an explicit `groupadd`/`useradd` and `/sbin/nologin` shell.
- **Helm deployment securityContext:** Full pod-level and per-container security hardening on both the collector and API containers — `runAsNonRoot: true`, `runAsUser/Group: 10001`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `capabilities.drop: [ALL]`, and `seccompProfile.type: RuntimeDefault`. `/tmp` directories served by `emptyDir` volumes (64 MiB each) to satisfy Python's runtime tmp needs under a read-only root.
- **Helm PostgreSQL securityContext:** Pod and container security hardening on the PostgreSQL StatefulSet — `runAsUser/Group: 70` (upstream requirement), `readOnlyRootFilesystem: true`, `capabilities.drop: [ALL]`, `seccompProfile.type: RuntimeDefault`. `/var/run/postgresql` and `/tmp` mounted as `emptyDir` volumes. PostgreSQL upgraded from `17-alpine` to `18-alpine` for longer upstream lifecycle.
- **PostgreSQL scram-sha-256:** `POSTGRES_INITDB_ARGS` set to `--auth-host=scram-sha-256 --auth-local=scram-sha-256` — replaces the default md5 password hashing with the stronger SCRAM-SHA-256 protocol. Liveness and readiness probes added via `pg_isready`.
- **ClusterRole secrets removal:** Removed `secrets` from the ClusterRole resource list, eliminating the critical RBAC over-permission (KSV-0041) that allowed the service account to read cluster-wide secrets.
- **API security headers:** New `SecurityHeadersMiddleware` (Starlette `BaseHTTPMiddleware`) added to the FastAPI app, injecting seven OWASP-recommended headers on every response: `X-Content-Type-Options`, `X-Frame-Options`, `X-XSS-Protection`, `Referrer-Policy`, `Permissions-Policy`, `Cache-Control`, and a strict `Content-Security-Policy`. CORS is now restricted to `GET`, `POST`, `OPTIONS` methods and `Authorization`/`Content-Type` headers (previously wildcard).
- **Automated vulnerability scanning (CI):** New `.github/workflows/security.yml` workflow running on every push/PR to `main`/`dev` and weekly (Monday 06:00 UTC) — five jobs: Trivy image scan for the GreenKube image (exit 1 on CRITICAL/HIGH), Trivy image scan for PostgreSQL (informational), Trivy IaC config scan for Dockerfile + Helm chart, Trivy filesystem scan for Python dependencies, and `npm audit` for the frontend. SARIF results uploaded to GitHub Security.
- **`.trivyignore`:** Documents eight upstream-unfixable CVEs (gosu/Go-stdlib CVEs in the Alpine postgres image, one OpenSSL CMS CVE, and one zlib utility CVE) with justifications and a quarterly review date.

### Added
- **Helm `secrets.existingSecret`:** New `secrets.existingSecret` value allows passing the name of a pre-created Kubernetes Secret instead of letting the chart manage one. When set, the chart skips Secret creation entirely and all `secrets.*` inline values are ignored — recommended for production to avoid storing credentials in `values.yaml`.
- **SQLite SCD2 node snapshots:** `SQLiteNodeRepository` now implements a Slowly Changing Dimensions Type 2 pattern to deduplicate node records across collection cycles. A separate `node_snapshots_scd` table stores only rows where tracked columns (`instance_type`, `vcpu`, `memory_gb`, `region`, `provider`, `zone`) actually changed, avoiding write amplification on stable clusters. Migration `0003` creates this table and the associated indexes.
- **Recommendation `scope` column:** `recommendation_history` table now includes a `scope` TEXT column (values: `pod`, `namespace`, `node`) to allow filtering recommendations by granularity. `pod_name` and `namespace` columns are nullable for node-scope and cluster-scope recommendations. Applied in migration `0003` for both PostgreSQL and SQLite.
- **Configurable PostgreSQL connection pool:** New `DB_POOL_MIN_SIZE` (default: `2`) and `DB_POOL_MAX_SIZE` (default: `10`) environment variables control `asyncpg`'s connection pool bounds. Exposed as `db.poolMinSize` / `db.poolMaxSize` in `helm-chart/values.yaml` and propagated via `configmap.yaml`.
- **Configurable statement timeout:** New `DB_STATEMENT_TIMEOUT_MS` environment variable (default: `30000` ms) sets a per-statement timeout on the PostgreSQL connection pool via `server_settings`. Exposed as `db.statementTimeoutMs` in `helm-chart/values.yaml`.
- **Database migration indexes (0003):** Compound indexes added on `combined_metrics(namespace, timestamp)`, `namespace_cache(last_seen)`, and `carbon_intensity_history(datetime)` to accelerate the most frequent query patterns.
- **Artifact Hub listing:** `helm-chart/Chart.yaml` enriched with full Artifact Hub annotations — `artifacthub.io/category`, `artifacthub.io/screenshots` (6 screenshots), `artifacthub.io/links`, `artifacthub.io/recommendations`, `artifacthub.io/changes`, `artifacthub.io/images` (linux/amd64 + linux/arm64), `artifacthub.io/maintainers`, and `artifacthub.io/readme` (fixes "no README" on the listing page). Chart now includes `keywords`, `home`, `sources`, and `maintainers` fields for richer search indexing.
- **`artifacthub-repo.yml`:** Artifact Hub repository metadata file with `repositoryID` for Verified Publisher badge. Automatically copied to `gh-pages` by the release workflow alongside `index.yaml`.
- **`llms.txt`** (`greenkube-website/public/`): LLM/AI crawler guidance file following the [llms.txt](https://llmstxt.org/) convention — enables AI assistants (Claude, ChatGPT, Perplexity) to understand GreenKube when crawling the website.
- **New dashboard screenshots:** `assets/demo-report.png` and `assets/demo-settings.png` added to README, `Chart.yaml` Artifact Hub screenshots, and `llms.txt`.
- **`scripts/pg_upgrade_17_to_18.sh`:** New maintenance script to upgrade an existing PostgreSQL 17 data directory to version 18 in-place using a Kubernetes Job and `pg_upgrade --link`, preserving all data with an automatic backup.

### Fixed
- **Aggregate queries from both raw and hourly tables:** `aggregate_summary` and `aggregate_timeseries` now correctly query both the raw `combined_metrics` table and the pre-aggregated `hourly_metrics` table, ensuring historical reports cover the full retention window without gaps at the boundary between live and archived data.
- **Infinite aggregated retention by default:** `METRICS_AGGREGATED_RETENTION_DAYS` now defaults to `-1` (infinite retention), preserving historical data for multi-year trend analysis. Set an explicit positive integer to enforce a rolling window.
- **Trivy KSV-0014 on `init-pgrun-perms`:** Added `readOnlyRootFilesystem: true` to the PostgreSQL init container's securityContext, resolving the HIGH misconfiguration finding.
- **Frontend npm audit (HIGH):** Updated `svelte`, `vite`, `rollup`, `picomatch`, `devalue`, and `@sveltejs/kit` to their latest compatible versions, resolving all HIGH-severity advisories.
- **CI Trivy image scan:** Split the GreenKube image scan into a `table`-format step (exit-code 1, visible in log) and a separate `sarif` step (exit-code 0, uploaded to GitHub Security tab). Added `pull: true` to the Docker build step so the base image layers are always pulled fresh from the registry, preventing stale GHA cache from hiding unfixed CVEs.

### Changed
- **`artifacthub-repo.yml`:** Owner `name` and `email` corrected to match the actual GitHub account (`Hugo Lelievre` / `hugo@greenkube.cloud`).
- **Storage layer refactoring:** The `src/greenkube/storage/` package is split into three sub-packages — `storage/postgres/`, `storage/sqlite/`, and `storage/elastic/` — each with its own `__init__.py`. All cross-package imports updated. Test suite reorganized to mirror the new structure with dedicated `tests/core/`, `tests/grafana/`, and `tests/helm/` directories.
- **`pyproject.toml`:** Added 20 SEO keywords, 5 new PyPI classifiers, and 4 additional project URLs (Documentation, Changelog, Docker Hub, Repository).
- **`release.yml`:** Release workflow now copies `artifacthub-repo.yml` to `gh-pages` on every release so Artifact Hub always picks up the latest metadata.
- **`scripts/sync_version.py`:** `update_helm_chart_yaml()` now also keeps the `artifacthub.io/images` annotation in sync with the new version on each release.



## [0.2.7] — 2026-04-05

### Added
- **Scaleway Kapsule support in `NodeCollector`:** `_detect_cloud_provider` now recognises Scaleway nodes via `k8s.scaleway.com/*` labels (primary signal set by the Scaleway Cloud Controller Manager on every Kapsule node) and falls back to `node.spec.provider_id` starting with `scaleway://` for clusters where those labels may be absent. `_extract_node_pool` returns `k8s.scaleway.com/nodepool-name` (with `nodepool-id` as a fallback). Scaleway region mappings (`fr-par`, `nl-ams`, `pl-waw` → Electricity Maps zones) and PUE profile (`1.37`) were already present in the data layer and are now fully wired up.
- **Collector health checks:** New `HealthCheckService` (`src/greenkube/core/health.py`) that performs periodic connectivity checks against all data sources — Prometheus, OpenCost, Electricity Maps, Boavizta, and Kubernetes. Each probe reports status (`healthy`, `degraded`, `unreachable`, `unconfigured`), latency, resolved URL, and whether the service was auto-discovered or manually configured.
- **`GET /api/v1/health/services` endpoint:** Returns aggregated health status for all data sources with per-service details. Supports `?force=true` to bypass the 30-second cache and trigger fresh probes.
- **`GET /api/v1/health/services/{service_name}` endpoint:** Returns health status for a single named service.
- **`POST /api/v1/config/services` endpoint:** Allows updating service URLs (Prometheus, OpenCost, Boavizta) and the Electricity Maps token at runtime from the frontend. Changes are session-scoped and do not persist across pod restarts.
- **Health models:** New `ServiceHealth`, `HealthCheckResponse`, and `ServiceConfigUpdate` Pydantic models in `src/greenkube/models/health.py`.
- **Frontend service health overview:** The Settings page now displays a color-coded health card for each data source (green=healthy, yellow=degraded, red=unreachable, gray=unconfigured) with latency, URL, and auto-discovery status.
- **Frontend service configuration:** New "Configure Services" section on the Settings page allows users to override Prometheus URL, OpenCost URL, Electricity Maps token, and Boavizta URL directly from the browser — with immediate health re-check feedback.
- **Frontend startup health popup:** On first load, if any data source is unreachable or unconfigured, a modal popup alerts the user and offers inline fields to configure the missing service URLs/tokens.
- **Sidebar health indicators:** The sidebar now shows per-service health dots for all data sources, giving an at-a-glance overview of system health from any page.
- **`HealthBadge` component:** Reusable Svelte component (`frontend/src/lib/components/HealthBadge.svelte`) for color-coded service health indicators.
- **`HealthPopup` component:** Modal component (`frontend/src/lib/components/HealthPopup.svelte`) for first-connection service configuration.
- **Health check caching:** Results are cached for 30 seconds to avoid hammering external services on repeated page loads.
- **CI/CD CLI flags:** New `--no-color` flag (and `NO_COLOR` env var support) to disable Rich formatting for clean pipeline logs. New `--fail-on-recommendations` flag on `greenkube recommend` to exit with code 1 when recommendations are found. New `--fail-on-co2-threshold` and `--fail-on-cost-threshold` flags on `greenkube report` to enforce carbon/cost policy gates in CI/CD pipelines.
- **Frontend test suite:** Comprehensive Vitest test suite (`frontend/tests/`) with 133 tests across 8 files covering all JS utility modules (formatters, API client, Svelte stores, ECharts option builders) and Svelte components (StatCard, Card, DataState, HealthBadge) using `@testing-library/svelte`. Added `npm test`, `npm run test:watch`, and `npm run test:coverage` scripts.
- **Test coverage badges in README:** Added Python coverage (79%), frontend coverage (93%), and total tests (771 passed) shields.io badges at the top of the README.

## [0.2.6] — 2026-04-05

### Added
- **Report page in the web dashboard:** New `/report` route in the SvelteKit SPA — a full-featured report builder that lets users configure time range (1 h → 1 y), namespace filter, aggregation (hourly/daily/weekly/monthly/yearly) and export format (CSV or JSON), preview totals before downloading, then trigger a direct browser download — no CLI or `kubectl exec` required.
- **`GET /api/v1/report/summary` endpoint:** Returns a preview of the report (row count, unique pods/namespaces, CO₂e, embodied CO₂e, energy, cost) for the current filter/aggregation parameters.
- **`GET /api/v1/report/export` endpoint:** Streams a downloadable file (CSV or JSON) with correct `Content-Disposition` headers. Supports the same `namespace`, `last`, `aggregate`, and `granularity` parameters as the CLI `greenkube report` command.
- **`ReportSummaryResponse` schema:** New Pydantic response model in `api/schemas.py`.

### Fixed
- **PUE fallback for unknown node provider:** `Config.get_pue_for_provider()` now falls back to the raw `DEFAULT_PUE` environment variable (default **1.3**) when a node's cloud provider is absent or not in `DATACENTER_PUE_PROFILES`, instead of incorrectly re-resolving through `self.DEFAULT_PUE` (which returns the *configured* `CLOUD_PROVIDER`'s profile — e.g. AWS=1.15 — even for unrelated unknown nodes). The `estimation_reasons` message now correctly reports **1.3** for unknown providers.
- **`CLOUD_PROVIDER` default changed from `aws` to `unknown`:** The env var and `helm-chart/values.yaml` previously defaulted to `"aws"`, silently applying AWS's PUE profile (1.15) on clusters where no cloud provider was configured. The default is now `"unknown"`, which correctly triggers the `DEFAULT_PUE` fallback (1.3) and produces an explicit warning log instead of a silent wrong value.
- **Settings page API status indicator:** The health dot was always red because the condition checked `health.status === 'healthy'` while the API returns `"ok"`. Fixed to `health.status === 'ok'`.

### Sustainability Score engine (previous unreleased entry)
- **Sustainability Score engine:** New `SustainabilityScorer` class (`src/greenkube/core/sustainability_score.py`) computes a composite **0–100 score** (100 = perfect cluster) across seven weighted dimensions:
  - **Resource Efficiency (25%)** — CPU and memory utilisation vs. requests
  - **Carbon Efficiency (20%)** — energy-weighted `grid_intensity × PUE`; penalises both dirty grids *and* inefficient datacentres equally
  - **Waste Elimination (15%)** — absence of zombie pods and idle namespaces
  - **Node Efficiency (15%)** — CPU and memory utilisation at the node level
  - **Scaling Practices (10%)** — HPA coverage and absence of over-provisioned autoscaling targets
  - **Carbon-Aware Scheduling (10%)** — share of workloads running in low-carbon zones
  - **Stability (5%)** — low container restart rate
- **PUE-aware carbon efficiency:** The carbon dimension uses `effective_intensity = grid_intensity × PUE` so that a high-PUE datacenter (e.g. OVH=1.37) is penalised relative to a hyperscaler-efficient one (e.g. GCP=1.09) even on the same electrical grid. Invalid/missing PUE safely defaults to 1.0.
- **`SustainabilityResult` Pydantic model:** Carries `overall_score` and a `dimension_scores` dict for structured downstream consumption.
- **New Prometheus gauges:**
  - `greenkube_sustainability_score{cluster}` — composite 0–100 score
  - `greenkube_sustainability_dimension_score{cluster, dimension}` — per-dimension breakdown
- **kube-state-metrics compatible labels:** All pod-level Prometheus metrics now carry `cluster`, `namespace`, `pod`, `node`, and `region` labels, matching kube-state-metrics conventions and enabling seamless Grafana variable-based filtering.
- **Grafana template variables:** `cluster` and `region` drop-down template variables added to the pre-built Grafana dashboard for multi-cluster/multi-region environments.
- **Grafana golden signal panels:** New panels in the Grafana dashboard:
  - Composite sustainability score gauge (0–100)
  - Per-dimension horizontal bar gauge
  - Sustainability score timeline
  - Carbon intensity by zone timeline
- **Methodology documentation:** `docs/sustainability-score.md` — full description of the 7-dimension scoring model, formulas, reference thresholds, and PUE impact table.

### Changed
- **`carbon_intensity` dimension → `carbon_efficiency`:** The scoring dimension was renamed and its formula extended to include PUE (`effective_intensity = grid_intensity × PUE`). The raw Prometheus gauges `greenkube_carbon_intensity_score` and `greenkube_carbon_intensity_zone` are kept unchanged for backward compatibility.
- **Helm configmap:** `CLUSTER_NAME` now propagated to the metrics endpoint so the `cluster` label is always populated.

## [0.2.5] — 2026-04-04

### Changed
- **CI/CD:** Replaced monolithic `ci-cd.yml` workflow with three focused workflows: `ci.yml` (lint & test on all PRs/pushes), `dev-build.yml` (dev Docker images on `dev` branch), `release.yml` (production builds triggered by semver git tags)
- **Docker tags:** Development images are now tagged `dev-<sha>` and `dev-latest`; release images use the semver version and `latest`
- **Release process:** Production Docker images and Helm charts are only published when a `vX.Y.Z` tag is pushed — no more mutable version tags
- **GitHub Releases:** Automated GitHub Releases with extracted changelog notes are created on each tag push

### Fixed
- **Helm chart:** `pre-install-check` CRD validation job now uses a dedicated `ServiceAccount` created via a `pre-install` hook, fixing the race condition where the job started before the main `ServiceAccount` existed
- **Helm chart:** `post-install-hook` ready-check job now uses a dedicated `ServiceAccount` with its own hook lifecycle, preventing "serviceaccount not found" errors during fresh installs and upgrades
- **OVH zone mapping:** `topology.kubernetes.io/zone=nova` (OpenStack default AZ name) is now ignored and the lookup falls through to the `region` label (`GRA11`, `RBX8`, …); numeric suffixes are stripped (`GRA11` → `GRA`) before CSV lookup — all OVH data-centres now resolve to the correct Electricity Maps zone
- **OVH provider detection:** Nodes labeled with `node.k8s.ovh/type` (current OVHcloud MKS generation) are now correctly identified as provider `ovh`; the previous check only matched the legacy `k8s.ovh.net/` prefix

### Added
- **OVH region mapping:** Extended `cloud_region_electricity_maps_mapping.csv` with uppercase trigrams (`GRA`, `RBX`, `SBG`, `WAW`, `BHS`, `LIM`, `ERI`, `VIN`, `HIL`, `YYZ`, `SGP`, `SYD`, `YNM`) and all new-API long-form region IDs (`eu-west-par`, `eu-west-gra`, `eu-central-waw`, `ca-east-bhs`, `us-east-vin`, `ap-southeast-sgp`, `ap-southeast-syd`, `ap-south-mum`, …)

## [0.2.4] — 2026-03-30

### Fixed
- **Helm chart:** `ServiceMonitor` and `NetworkPolicy` are now **disabled by default** — fresh installs no longer fail on clusters without the Prometheus Operator (`monitoring.coreos.com/v1` CRD)
- **Helm chart:** Added `pre-install-check` hook that validates the Prometheus Operator CRD is present before creating a `ServiceMonitor`, with a clear actionable error message
- **Grafana dashboard:** Wrapped cluster overview stat panels with `sum()` to prevent duplicate series when multiple targets report the same metric
- **Recommendation history:** Skip node-level recommendations (`pod_name=None`) when saving to history — prevents integrity errors and irrelevant entries
- **Container startup:** Fixed `greenkube start` hanging in Docker containers due to buffered stdout; invisible INFO logs now correctly flushed to the console

### Changed
- **Helm NOTES:** Replaced plain text banner with ASCII art logo; removed ServiceMonitor noise — only relevant info shown at install time
- **Helm values:** Clarified `monitoring` section comments to distinguish GreenKube→Prometheus (automatic) from Prometheus→GreenKube (optional, for Grafana)
- **README:** Clarified Prometheus dependency — GreenKube works with basic Prometheus, kube-prometheus-stack, or no Prometheus (graceful degradation); Prometheus Operator is never required

### Performance
- **SQL-level aggregation** for `/api/v1/metrics/summary` and `/api/v1/metrics/timeseries`: aggregation now happens directly in the database (SQLite and PostgreSQL) instead of loading all rows into Python — typically **10–20× faster** for large datasets and demo mode
- **Non-blocking dashboard recommendations:** Recommendations on the dashboard are now fetched asynchronously in the background, so the rest of the page renders instantly without waiting for the recommendation engine

## [0.2.3] — 2026-03-29

### Added
- **Grafana dashboard:** Pre-built `dashboards/greenkube-grafana.json` with KPIs, time-series, per-namespace breakdown, node utilization, grid intensity, and recommendations panels
- **Prometheus integration:** ServiceMonitor, NetworkPolicy, and Prometheus RBAC templates in the Helm chart for seamless kube-prometheus-stack scraping
- **Prometheus `/prometheus/metrics` endpoint:** Comprehensive metric exposition (CO₂e, cost, energy, CPU, memory, network, disk, restarts, nodes, grid intensity, recommendations) with correct label relabeling
- **Demo mode:** `greenkube demo` command generates 7 days of realistic sample data (22 pods, 5 namespaces) in a standalone SQLite instance — explore the dashboard without a live cluster
- **Database migration system:** Automated schema migration runner with versioned scripts for PostgreSQL and SQLite
- **`CarbonIntensityRepository` split:** Dedicated repository implementations per backend (Postgres, SQLite, Elasticsearch) following the same pattern as other repositories
- **DataProcessor refactor:** Monolithic processor split into focused collaborators — `CollectionOrchestrator`, `MetricAssembler`, `NodeZoneMapper`, `PrometheusResourceMapper`, `CostNormalizer`, `HistoricalRangeProcessor`, `EmbodiedEmissionsService`
- **On-premises documentation:** Secrets setup and zone configuration commands for bare-metal / on-prem clusters
- **Prometheus & Grafana guide:** Setup instructions for scraping GreenKube metrics and importing the Grafana dashboard
- Namespace input validation on all API endpoints (Kubernetes naming rules)
- Contributing guide (`CONTRIBUTING.md`)
- Architecture diagram in `docs/architecture.md`
- API curl examples in README
- **API security:** Optional bearer-token authentication (`GREENKUBE_API_KEY`), configurable CORS origins, rate limiting via slowapi
- **Pagination:** `GET /api/v1/metrics` now supports `offset` and `limit` query parameters
- **Docker healthcheck:** Built-in `HEALTHCHECK` instruction for standalone usage
- **Helm chart tests:** `helm test` connectivity validation via `test-connection.yaml`
- **Graceful shutdown:** `preStop` lifecycle hook on the API container
- **Integration tests:** End-to-end API tests with real SQLite backend and migration tests
- **Methodology section** in README explaining how energy and CO₂e are estimated
- Shared `parse_duration()` utility used by both CLI and API
- `Config.reload()` for clean test isolation

### Changed
- Minimum Python version raised from 3.9 to 3.10 (3.9 reached EOL October 2025)
- Helm chart generates a random PostgreSQL password when none is provided
- Replaced f-string logging with lazy `%`-formatting throughout the codebase
- `Recommendation` model uses typed `scope` field instead of sentinel `pod_name="*"`

### Fixed
- CLI `recommend` command now uses the unified recommendation engine (all 9 types) instead of legacy 2-type API
- CLI `recommend` reads from database by default (consistent with API); added `--live` flag for real-time mode
- `read_combined_metrics_from_database()` called with correct parameter names (`start_time`/`end_time`)
- Cost normalization in `run_range()` now divides range total by number of time steps
- `USER_AGENT` header dynamically reflects the actual package version
- Removed duplicate `DEFAULT_COST` class attribute from `Config`
- Helm `recommendSystemNamespaces` moved inside `recommendations` scope in `values.yaml`
- PostgreSQL credentials no longer shipped as plain text in Helm defaults
- DB connection string sourced from Secret instead of inline env var in deployment
- `collect_detailed_info()` now delegates to `collect()` to avoid inconsistent results
- Expanded test fixture env patching to prevent production defaults leaking into tests
- Removed `.tgz` artifacts from git tracking

### Performance
- **SQL-level aggregation for `/api/v1/metrics/summary` and `/api/v1/metrics/timeseries`:** Aggregation is now performed directly in the database (SQLite and PostgreSQL) instead of loading all rows into Python objects — typically **10–20× faster** for large datasets and demo mode
- **Non-blocking dashboard recommendations:** Recommendations on the dashboard are now fetched asynchronously in the background, so the rest of the page renders instantly without waiting for the recommendation engine

## [0.2.2] — 2026-02-15

### Added
- SvelteKit web dashboard with real-time charts, node inventory, and recommendations
- Full REST API (FastAPI) with metrics, nodes, namespaces, recommendations, and timeseries endpoints
- Multi-resource metrics: CPU, memory, network I/O, disk I/O, ephemeral storage, restarts
- 9-type recommendation engine: zombie, CPU/memory rightsizing, autoscaling, off-peak, idle namespace, carbon-aware, overprovisioned/underutilized node
- HPA-aware filtering for autoscaling recommendations
- Recommendation history storage and API endpoint
- PostgreSQL, SQLite, and Elasticsearch storage backends
- Prometheus metrics exposition (`/metrics`)
- Historical range reports with daily/monthly/yearly aggregation
- Node snapshot history for accurate time-range analysis
- Embodied emissions via Boavizta API integration
- Estimation transparency (flags and reasons for estimated values)
- Helm chart with PostgreSQL StatefulSet, RBAC, health probes, and auto-discovery
- Pre-commit hooks (Ruff, Gitleaks)
- CI/CD pipeline (GitHub Actions): lint, test, multi-arch Docker build, Helm publish

## [0.1.0] — 2025-08-01

### Added
- Initial release
- CLI-based carbon footprint reporting for Kubernetes pods
- Prometheus-based CPU metrics collection
- Energy estimation using cloud instance power profiles
- Carbon intensity data from Electricity Maps API
- Basic zombie pod and rightsizing recommendations
- CSV and JSON export
- SQLite storage backend

[Unreleased]: https://github.com/GreenKubeCloud/GreenKube/compare/v0.2.8...HEAD
[0.2.8]: https://github.com/GreenKubeCloud/GreenKube/compare/v0.2.7...v0.2.8
[0.2.7]: https://github.com/GreenKubeCloud/GreenKube/compare/v0.2.5...v0.2.7
[0.2.5]: https://github.com/GreenKubeCloud/GreenKube/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/GreenKubeCloud/GreenKube/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/GreenKubeCloud/GreenKube/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/GreenKubeCloud/GreenKube/compare/v0.1.0...v0.2.2
[0.1.0]: https://github.com/GreenKubeCloud/GreenKube/releases/tag/v0.1.0
