# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

GreenKube is now an optimization loop: aggregate recommendations, deliver them as GitOps pull requests, prove the savings. Read the **Upgrade notes** under "Changed" before upgrading from 0.3.1, and see `docs/upgrading.md`.

### Added

**Measurement and data integrity**
- Gap-aware collection on wall-clock steps: a stored row with `timestamp = T` covers `[T, T + step)`, a per-installation watermark advances only after a window is committed, and a catch-up job collects missing windows oldest first. New settings `PROMETHEUS_SCRAPE_INTERVAL` (`30s`), `COLLECTION_SAFETY_LAG` (`1m`), `COLLECTION_MAX_BACKFILL` (`24h`) and `COLLECTION_K8S_LIST_MAX_FAILURES` (3; after that the window is stored in degraded mode) with Helm `config.prometheus.scrapeInterval`, `config.collection.safetyLag`, `config.collection.maxBackfill` and `config.collection.k8sListMaxFailures`. Rate windows span `max(step, 4 × PROMETHEUS_SCRAPE_INTERVAL)`; an hourly check logs an ERROR when the real scrape interval leaves fewer than 2 scrapes per rate window.
- Collection coverage: every step is recorded as `collected`, `empty`, `failed` or `gap` (`collection_windows`); `GET /api/v1/collection/coverage?start=&end=` returns the coverage ratio and holes, and `GET /api/v1/health` adds the watermark, its lag and the last hole.
- Per-dimension provenance on pod and container rows (`cost_source`, `usage_source`, `power_source`, `intensity_source`, plus `node_source` and `metadata_source`), OpenCost cost components (`cost_cpu`, `cost_ram`, `cost_storage`, `cost_network`, `cost_lb`), container limits, native sidecars (`kind="sidecar"`), and the collection pass that wrote each row (`collection_run_id`, `collected_at`).
- Pod metadata history (`pod_snapshots`) and node snapshots: historical rows take owner, requests, containers, zone, PUE and embodied profile from the state valid at the sample time; pods never observed live fall back to kube-state-metrics. `INTENSITY_MAX_AGE` (Helm `config.intensityMaxAge`, `6h`) bounds the age of a carbon intensity.
- Short-lived containers get CPU usage from the counter start (`cpu_from_counter_start`); pods without a CPU sample still get a row with `cpu_usage_millicores` NULL; a pod-level memory fallback covers kubelets without container series; OpenCost costs too small for its 5-decimal output are rebuilt at OpenCost's node unit prices (trusted `cost_source` `opencost_derived`). OpenCost reconciliation: each step records the OpenCost total of its window, and `greenkube_collection_cost_unattributed_ratio{source}` reports the share no stored row carries.
- Installation identity: the controller stores the `kube-system` namespace UID once and refuses to collect under a different cluster UID unless `config.collection.allowClusterUidChange` (`ALLOW_CLUSTER_UID_CHANGE`) is set; the refusal is visible in `/readyz`, `GET /api/v1/health`, `greenkube_collection_identity_mismatch` and the `GreenKubeClusterIdentityMismatch` alert.
- `greenkube start --last <range> --force` replaces stored rows even when the re-collection looks worse; `greenkube recompute --since … [--until] [--all] [--dry-run]` re-allocates raw rows with the current Impact Model.

**Impact Model and projections**
- Impact Model (`docs/methodology/impact-model.md`): reservation-aware allocation of energy, operational and embodied CO2e and cost, with an explicit `__idle__` row per node (`node_idle_metrics`) so pod and idle rows add up to node totals; memory power (`POWER_MEMORY_W_PER_GIB`, Helm `impact.memoryWattsPerGiB`, 0.392 W/GiB) and GPU power from DCGM or a bundled board-power table. Stored rows record `model_version`.
- Cost fallback chain: observed OpenCost cost, observed per-resource node costs, a bundled on-demand `PricingCatalog` (AWS, Azure, OVHcloud, Scaleway), then custom rates. Settings `IMPACT_PRICING_CPU_CORE_HOUR`, `IMPACT_PRICING_RAM_GIB_HOUR`, `IMPACT_PRICING_CURRENCY` (Helm `impact.pricing.*`), `IMPACT_REALIZATION_FACTOR` (`impact.realizationFactor`, override only) and `CARBON_PRICE_PER_TONNE` (`impact.carbonPricePerTonne`, 100).
- Recommendations carry an `impact` projection (cost and CO2e breakdown, measurable CO2e components, capacity released, unit prices and their source, realization factor) returned by the API.
- Realization context: the realization factor is derived per node pool from Cluster Autoscaler status, Karpenter NodePools, events and metrics, `do-not-disrupt`/`safe-to-evict` annotations and blocking PodDisruptionBudgets (no autoscaler 0.0, Karpenter 0.8, Cluster Autoscaler 0.6, blockers ×0.5), stored across restarts. `IMPACT_AUTOSCALER_HINT` (`none`, `cluster_autoscaler`, `karpenter`; Helm `impact.autoscalerHint`) declares an autoscaler detection cannot see; a projection made before the cluster was observed reports "billable saving unknown".

**Change measurement and verdicts**
- Change records: every applied workload revision is stored as an immutable change (recommendations, plan, pull request, merge commit, synced revision, pod-template, resources and image digests) with a frozen 7-day baseline snapshot and a content digest. `GET /api/v2/changes[?plan_id=]`, `/api/v2/changes/{id}` and `/api/v2/changes/{id}/measurements`.
- Change measurement lifecycle `applied → measuring → realized | not_realized | inconclusive | contaminated`, then `regressed` or `closed`: a load-driver counterfactual (Istio, Linkerd, ingress-nginx, Envoy, the `greenkube.io/load-metric` annotation, then CPU), a per-replica load regression with fallback to an hour-of-week baseline, a bootstrap 90% interval with a between-week term, constant-price cost and hourly carbon-factor CO2e valuation, embodied CO2e when both windows carry it, and confounders (other changes, spec, HPA, price card, instance type, node-pool capacity and neighbours) that contaminate or lower confidence. Method: `docs/methodology/savings-measurement.md`.
- Settings `CHANGE_MEASUREMENT_ENABLED`, `CHANGE_MEASUREMENT_PRE_WINDOW_DAYS` (at least 7), `CHANGE_MEASUREMENT_POST_WINDOW_DAYS` (at least 5), `CHANGE_MEASUREMENT_WARMUP_HOURS`, `CHANGE_MEASUREMENT_MIN_COVERAGE`, `CHANGE_MEASUREMENT_BOOTSTRAP_SAMPLES`, `CHANGE_MEASUREMENT_EPSILON_RATIO`, `CHANGE_MEASUREMENT_PRIOR_WEEK_CV` (0.05) and `CHANGE_MEASUREMENT_ACCELERATED` (demos only; such measurements never accrue), under Helm `config.recommendations.changeMeasurement.*`.
- Drift monitor: the `savings-drift` loop moves a realized change to `regressed` when `CHANGE_DRIFT_THRESHOLD` (0.5) of its saving is lost for `CHANGE_DRIFT_SUSTAINED_DAYS` (3) days (Helm `config.recommendations.changeMeasurement.driftThreshold`, `driftSustainedDays`), closes it on resource drift, recreation or workload deletion, and reverses the accruals since.
- Reliability increases are judged on their OOM-kill rate (exact binomial test); their cost is reported as `cost_of_fix_annual`, never as a saving. Immutable `savings_measurements` (one row per decision, re-measurements supersede), `POST /api/v1/recommendations/{id}/remeasure` and `POST /api/v1/recommendations/{id}/rollback-review/dismiss`.

**Savings ledger**
- Append-only savings ledger v2 (`savings_ledger_entries`): signed entries per quantity and method (`projected`, `measured_allocated`, `realized_billed`, `user_declared`) with accrual, reversal and restatement kinds, a per-cluster hash chain keyed by the cluster UID, and restatement of projections once a measurement decides. The leader-only `ledger-chain` loop verifies the chain (`LEDGER_CHAIN_VERIFY_INTERVAL`, Helm `config.ledger.chainVerifyInterval`, `1h`). Idempotent accrual once per recommendation, method and UTC hour, with backfill after downtime; the v1 ledger keeps being written and is projected into v2.
- `GET /api/v2/ledger/entries`, `/api/v2/ledger/summary` and `/api/v2/ledger/export` (`format=csv` or FOCUS-aligned `format=focus`), reconciling with `GET /api/v1/recommendations/savings`, which adds `measured_basis` and a `realization` block (billed saving as an expectation).

**Released capacity**
- Projected released capacity on every recommendation (core-, GiB-, replica- and node-hours per year, cores and GiB now, storage and LoadBalancers for deletions), shown in pull requests, the list and the detail page, never converted to money; increases are shown as reserved capacity.
- Measured released capacity and a capacity verdict (`released`, `partially_released`, `not_released`, `unknown`) on each decided change measurement, from the recorded specs, even when the cost verdict is inconclusive. `GET /api/v1/recommendations/export?format=csv|json` exports active recommendations with projected costs and released capacity.

**Recommendations, ranking and risk**
- Scheduled, leader-gated scans every `RECOMMENDATION_SCAN_INTERVAL` (`1h`, Helm `config.recommendations.scanInterval`) and an optimization run API: `POST /api/v1/optimization/runs` (202 with `run_id`), `GET /api/v1/optimization/runs/{id}` and `GET /api/v1/optimization/runs?limit=`, with per-source health and the diff against the previous set.
- Expected-value ranking: each recommendation carries a `value` block (`value_annual`, `p_success`, `incident_cost`, `effort_cost`, `ev`, lane `savings` or `reliability`) with factors in currency; settings `RECOMMENDATION_INCIDENT_COST` (100), `RECOMMENDATION_ENGINEER_HOUR_COST` (0) and `RECOMMENDATION_CAPACITY_VALUE_WEIGHT` (1), Helm `config.recommendations.incidentCost`, `engineerHourCost` and `capacityValueWeight`. Profiles `balanced`, `cost_first`, `carbon_first`, `low_risk`, `quick_wins` via `?profile=` and `?carbon_price=`.
- Source track record (`GET /api/v1/sources/track-record`) feeds `p_success`; risk model v2 reports per-replica headroom, OOM, throttling and restart history, HPA, VPA, PDB, JVM hints, in-place resize constraints and the `greenkube.io/criticality` label in `risk_factors`; evidence carries `oom_events`, `cpu_throttling_ratio` and restarts over the lookback.
- Two-way rightsizing: reliability increases (`RIGHTSIZING_INCREASE_THRESHOLD`, 0.9), capped per step by `RIGHTSIZING_MAX_INCREASE_FACTOR` (2.0) unless OOM kills back it, with startup points excluded (`RIGHTSIZING_WARMUP_MINUTES`, 10); limit advice by `RECOMMENDATION_LIMITS_POLICY` (`keep`, `remove_cpu`, `memory_equals_request`); decreases need `RECOMMENDATION_MIN_DECREASE_COVERAGE_DAYS` (7) of coverage. Helm `config.recommendations.rightsizingIncreaseThreshold`, `rightsizingMaxIncreaseFactor`, `rightsizingWarmupMinutes`, `limitsPolicy`, `minDecreaseCoverageDays`. `RECOMMENDATION_TARGET_MAX_STALENESS_HOURS` (2, Helm `config.recommendations.targetMaxStalenessHours`): no advice for workloads and containers that no longer exist; their recommendations become `stale` (`target_gone`).
- Server-side `eligibility` on every recommendation (`can_open_pr`, `delivery_modes`, blockers and warnings with code, message and fix, `eligible_at` while evidence warms up), documented in `docs/reference/eligibility-codes.md`.
- Recommendation v2 and revisions: `GET /api/v2/recommendations[/{id}]` (target, typed proposal, consensus, impact, risk, value, eligibility, revisions; `?source=`, `?kind=` filters); a recommendation never changes under an open plan, newer advice is queued as the next revision and flagged on the PR. Advice after an applied change names it (`previous_recommendation_id`, `previous_change_id`); ignored recommendations show `changed_since_ignored` when the advice changes.

**Recommendation sources and consensus**
- Source SDK and registry: every source (built-in analyzers, `vpa`, `greenkube.nodepool`, third-party plugins from the `greenkube.sources` entry points) runs isolated with timeouts, rate limit and circuit breaker; runs and signals are persisted. `GET /api/v1/sources`, `/api/v1/sources/{id}/runs` and `/signals`. Settings `SOURCES_PLUGINS_ENABLED` (Helm `sources.pluginsEnabled`) and `SOURCES_CONFIG` (Helm `sources.settings`, per-source `enabled: true|false|auto`). Guide `docs/sources.md`.
- Consensus engine: one recommendation per target from all sources, policy `RECOMMENDATION_CONSENSUS_POLICY` (`safest` default, `median`, `weighted`, `priority`; Helm `config.recommendations.consensusPolicy`), with agreement, contested flag and votes; sources sharing an engine (Goldilocks and VPA) vote once.
- New connectors: Goldilocks (via the VPA connector), Robusta KRR (`POST /api/v1/sources/krr/reports`, optional CronJob `sources.krr.job.*`), Kubecost 2.x/3.x, Fairwinds Polaris hygiene findings (`POST /api/v1/sources/polaris/reports`, optional CronJob `sources.polaris.job`), GKE recommender insights (Workload Identity), Azure Advisor for AKS, NodePool policy advice, and a push API `POST /api/v1/signals` with public schemas `schemas/signal.v1alpha1.json` and `signal-batch.v1alpha1.json`. Pages under `docs/sources/`.

**Deletion recommendations**
- Every deletion recommendation lists its evidence and unused duration; a LoadBalancer or PersistentVolume must look unused for `DELETION_MIN_UNUSED_DAYS` (7, Helm `config.recommendations.deletionMinUnusedDays`). Opt out with `greenkube.io/ignore: "true"` or `greenkube.io/keep`.
- Orphaned LoadBalancer and PersistentVolume deletions become reviewed pull requests (raw and Kustomize sources), never auto-approved and without automated rollback; open deletion PRs are re-checked every 5 minutes and closed with a "do not merge" comment when the resource is in use again. Zombie workloads and idle namespaces stay manual.
- Manual mode: `GET /api/v1/recommendations/{id}/manual` returns `kubectl` steps, a patch and a diff; `POST …/manual/applied` records a hand-applied change so it is measured; an **Apply manually** panel in the UI.

**GitOps delivery (pull requests, Argo CD, Flux, Helm, Kustomize)**
- Always-on automation: the `automation-readiness` loop infers Git providers from discovered Argo CD and Flux sources and activates delivery when the Secret `greenkube-git-credentials` (key `token`, or GitHub App `appId` + `privateKey`, optional `host`) exists, without upgrade or restart. `automation.policy.pauseDelivery` is the only off switch. Capability states are exported as `greenkube_capability_active{capability}`. GitHub App authentication with per-repository installation tokens, signed (Verified) commits and the App's installation selection as allowlist.
- Delivery modes `automation.delivery.mode` (`AUTOMATION_DELIVERY_MODE`): `direct` (default), `patch` or `ci`, with `GET /api/v1/automation/plans/{id}/patch`, `POST /api/v1/automation/plans/{id}/delivery` (scope `plans:deliver`), verified delivery reports, and CLI `greenkube plan apply-patch` / `report-delivery`.
- Policy-based auto-approval `automation.autoApprove` (`enabled`, `maxRisk`, `minConfidence`, `contested` must stay `false`, `increases`), with `auto_approve_blockers` in the response.
- PR template v2: readable title, Summary, Changes, Why, Risk, Assumptions, Rollback and Verification sections and a machine-readable footer; overridable with `automation.pr.template` and `automation.pr.titlePrefix`; derived labels (`automation.pr.defaultLabels`), drafts for high-risk plans (`automation.pr.draftHighRisk`), CODEOWNERS reviewers (`automation.pr.codeowners`), a `greenkube/validation` commit status (`automation.pr.commitStatus`), one lifecycle comment per PR (`automation.pr.lifecycleComments`, `lifecycleLookbackDays`) and commit trailers `GreenKube-Plan` / `GreenKube-Recommendations`. Deep links need `config.externalUrl`.
- Request increases, limits (raise, or remove CPU limits), native sidecars and init containers, and Argo Rollouts become pull requests; Kustomize patches can write limits.
- Remote Helm charts (HTTP repositories and OCI registries) for Argo CD (including multi-source `$values`) and Flux, pre-fetched at the deployed version and digest-verified, with a values-path mapping registry and operator mappings `automation.helm.chartMappings`. Settings `automation.helm.remoteCharts.enabled` and `automation.helm.remoteCharts.allowedHosts`.
- `automation.localClusterAliases` (`GITOPS_LOCAL_CLUSTER_ALIASES`) names extra destinations of the local cluster; `automation.providers[].webBaseUrl` gives Gitea a browser-facing origin.
- Plan API additions: `DELETE /api/v1/automation/plans/{id}` cancels an unapproved preview, `POST /api/v1/automation/plans/{id}/abandon` releases a plan stuck in `applying`, plans record their render inputs, and preview errors include the planner's `reason`. `automation.policy.missingEvidence` (`fail` or `warn`) and a VPA gate that blocks conflicting VPA writers; `no_effective_change` fails closed so a pull request that changes nothing is never opened.
- Git provider observability: per-operation call budget (200), `greenkube_git_provider_*` metrics including `greenkube_git_provider_rate_limited_total` and the `GreenKubeGitProviderRateLimited` alert, verification state and `degraded_features` in `GET /api/v1/automation/status`.
- Guided setup checklist `GET /api/v1/setup/status` and the `/setup` page with a banner; repository suggestions and `POST /api/v1/automation/repositories/allowlist` (admin) that returns the equivalent `automation.repositoryAllowlist` snippet.
- Renderer egress canary: the renderer checks daily that its egress-deny NetworkPolicy is enforced; the checklist and plan previews report `renderer_egress_unenforced`.

**Authentication and roles**
- Accounts and server-side sessions (`API_AUTH_MODE=session`, the new default): `/api/v1/auth/login|logout|password|me|sessions`, `HttpOnly` cookies shared by API replicas, CSRF protection and login rate limiting. First admin from `auth.admin.existingSecret` or a generated password in the Secret `<release>-initial-admin`; `greenkube auth reset-password`.
- Scoped API keys (`/api/v1/auth/api-keys`; scopes `read`, `write`, `approve`, `signals:write`, `signals:report`, `plans:deliver`, `metrics:read`, `admin`) and a generated Prometheus scrape token `GREENKUBE_METRICS_TOKEN` (`metrics:read`) used by the ServiceMonitors.
- Single sign-on: `config.apiAuthMode: proxy` (oauth2-proxy, Authentik, Pomerium; `auth.proxy.*`, requires `config.trustedProxyIps`) and `oidc` (`auth.oidc.*`, PKCE); group-to-role mapping `auth.roles.groups` and `auth.roles.defaultRole`; `auth.localLogin`; users and roles administration.
- Settings `SESSION_COOKIE_SECURE`, `AUTH_SESSION_IDLE_TIMEOUT`, `AUTH_SESSION_ABSOLUTE_LIFETIME`, `AUTH_SESSION_REMEMBER_LIFETIME`, `AUTH_LOGIN_RATE_LIMIT`, `AUTH_ADMIN_USERNAME`, `AUTH_ADMIN_PASSWORD`, `AUTH_INITIAL_ADMIN_SECRET`, `AUTH_LEGACY_API_KEY` (`auth.legacyApiKey`: `admin`, `metrics`, `disabled`), `HSTS_MAX_AGE`, `HSTS_INCLUDE_SUBDOMAINS` (`auth.session.hsts.*`). Guides `docs/authentication.md` and `docs/security-model.md`.

**Helm chart and operations**
- Credentials bootstrap hook: generates credentials once into the Secret `<release>`, never rotates them and keeps them on uninstall; render output no longer depends on cluster state.
- `values.schema.json` validates values before rendering; Ingress (`ingress.*`) and Gateway API HTTPRoute (`httpRoute.*`) templates that also derive `GREENKUBE_EXTERNAL_URL`.
- Per-component scheduling (`nodeSelector`, `tolerations`, `affinity`, `topologySpreadConstraints`, `priorityClassName`, `podAnnotations`, `podLabels`), `global.imagePullSecrets`, PodDisruptionBudgets and soft anti-affinity (`podAntiAffinity`) for multi-replica components.
- PostgreSQL: optional `postgres.backup` CronJob, `docs/operations/backup-restore.md`, `postgres.shmSizeLimit`, and `greenkube db copy` to move a 0.3.x SQLite database into PostgreSQL (`--include-metrics`, `--dry-run`, `--allow-nonempty`).
- Controller metrics and health on `CONTROLLER_METRICS_PORT` (Helm `controller.metricsPort`, 9102): `/prometheus/metrics`, `/healthz`, `/readyz`, `/loops`, a controller ServiceMonitor, loop metrics (`greenkube_loop_*`, `greenkube_controller_leader`), collection, ledger and auth metrics; controller heartbeats in `GET /api/v1/automation/status`.
- PrometheusRule groups `greenkube.controller`, `greenkube.loop`, `greenkube.ledger` and `greenkube.operations` (collection lag and degradation, scan failures, measurement stalls, ledger chain, API, database, PostgreSQL, backup and volume alerts), with `monitoring.prometheusRule.controllerAlerts`, `monitoring.prometheusRule.operations` and `monitoring.prometheusRule.thresholds.*`. `renderer.egressFirewall.privilegedInitContainer` (formerly under `rendererSandbox`) for clusters that need a privileged firewall init container; ConfigMap changes restart the pods; install notes print the sign-in and key commands.
- Guides: `docs/upgrading.md`, production checklist and sizing in `docs/installation.md`, `docs/ui.md`, `docs/concepts.md`, `docs/roadmap.md`, and a quickstart (`make quickstart`, `docs/examples/quickstart/`, `docs/get-started/first-pr.md`).
- `make e2e` end-to-end loop on Minikube (nightly and release gate), `make upgrade-test` from 0.3.1, `make test-pg`, Dependabot, CodeQL, `actionlint`, a Pod Security `restricted` check, promtool rule tests, signed release images with SBOM and provenance, and community files (`SECURITY.md`, `CODE_OF_CONDUCT.md`, issue and PR templates).

**UI**
- New pages: savings-first **Overview**, recommendation detail `/recommendations/{id}`, **Changes** board `/changes` and `/changes/{plan_id}`, **Ledger** `/ledger`, **Sources**, **Setup**, **Footprint**, account, sessions, API keys and users pages.
- Recommendation cards show expected value and success probability, filters by namespace, source, risk and capability, warm-up ETA, released capacity, **Rescan**, one-click **Open PR** and **Apply manually**; amounts use the pricing `currency` now returned by `GET /api/v1/config`; viewers see read-only hints.
- **Demo mode flag:** `greenkube demo` sets `GREENKUBE_DEMO_MODE`, reported as `demo` by `GET /api/v1/health`. In demo mode the UI shows one notice ("Demo data: pull requests are disabled in the demo") instead of the per-card `action_unavailable`, `automation_not_configured` and `automation_disabled` diagnostics, says "Demo" instead of "Sign-in disabled (development)", hides the service health dots, the connectivity pop-up and the setup banner, and the change page says the demo stores no plan instead of a plan-service error. Real installs are unchanged.
- The v1 savings ledger tables are append-only (migrations 0161 and 0162, SQLite and PostgreSQL; every column is compared): `DELETE` and every `UPDATE` except setting `superseded` from false to true are rejected. A reconciliation of the ledger v2 accrued book with the non-superseded v1 rows runs on every `ledger-chain` tick: `greenkube_ledger_reconciliation_ok{cluster_uid}` and the `GreenKubeLedgerReconciliationFailed` alert.
- Every ledger amount names its accounting basis (`allocated`, `billable_expected`, `billed`, `declared`): new `basis` column on `recommendation_savings_ledger` and `savings_ledger_entries` (migration 0160, SQLite and PostgreSQL; v1 rows are backfilled from their method, v2 entries stored before keep NULL and their hash, and readers infer the basis). `GET /api/v2/ledger/entries` returns `basis` (and `basis_stored`), `/summary` totals carry `accounting_bases`, the CSV export a `basis` column and the FOCUS export `x-greenkube-basis`. Entries written from now on use method version `ledger/2` and include `basis` in their hash payload.
- **Released capacity and the cost of reliability fixes in the savings ledger.** For each verified recommendation the ledger v2 accrues the released capacity its change measurement booked as its own quantities, `cpu_core_h` (core-hours) and `mem_gib_h` (GiB-hours), reason `released_capacity`, signed (an increase reserves capacity) and never converted to money or added to a savings total. The measured cost of a reliability fix (`cost_of_fix_annual`) is booked as a negative `cost` entry under `measured_allocated` with reason `cost_of_fix`, never netted into a savings total: `reliability_cost_to_date` in the savings report, `cost_of_fix` in the ledger summary, left out of the savings counters and dashboard windows. Both are hourly from `applied_at`, in the `restatement` book, and superseded with their recommendation. `GET /api/v2/ledger/entries` and `/summary` accept and report the new quantities; the FOCUS export leaves capacity out.
- Ledger chain verification also checks that every reversal cancels its target exactly (`reversal_mismatch`, drift reversals excepted) and that each entry belongs to the chain's cluster, and publishes each verified head as `greenkube_ledger_chain_head_seq{cluster_uid}` and `greenkube_ledger_chain_head_info` (LED-05, partial). The methodology now states what the unkeyed chain does and does not guarantee.
- Migration 0155 adds `collection_windows.cost_status`; gauge `greenkube_collection_cost_pending_steps`; counter `greenkube_opencost_unrebuilt_allocations_total`; gauge `greenkube_opencost_ambiguous_cluster`.
- Setting `OPENCOST_CLUSTER_ID` (Helm `config.opencost.clusterId`).

### Changed

**Upgrade notes (from 0.3.1)**
- Back up the database first. Migrations `0038` to `0147` run at controller startup only (PostgreSQL advisory lock, no statement timeout); the API answers `503 migrating` until they finish. Rollback after migrations is a restore, not `helm rollback` (`docs/operations/backup-restore.md`).
- Upgrade with `helm upgrade --reset-then-reuse-values` or `-f <your-values.yaml>`; `--reuse-values` stops before changing anything ("the chart defaults are missing").
- SQLite releases (`config.db.type=sqlite`) fail before Helm changes anything and print the `greenkube db copy` commands to move to PostgreSQL; the former claim is kept.
- One install topology: `<release>-api`, `<release>-controller`, `<release>-renderer` and the bundled PostgreSQL. The values `profile`, `replicaCount`, `production.*`, `automation.enabled`, `automation.mode`, `automation.workerMetricsPort`, `rendererSandbox.*` (now `renderer.*`), `collector.*` (`collector.metricsPort` is now `controller.metricsPort`), `resources`, `apiResources` and the SQLite values are mapped for one minor release with a **DEPRECATED VALUES** list in the install notes; `sources.priority` replaces `config.recommendations.sourcePriority`; one `config.recommendations.minSamples` sets all sample gates; the ConfigMap is `<release>-config`.
- Breaking for `rbac.create=false`: installs that relied on a generated API key or PostgreSQL password no longer render; set `secrets.existingSecret=<release>` or supply `secrets.apiKey` and `postgres.auth.password`. Other upgrades keep the Secret `<release>` and its key and password; with `secrets.existingSecret`, add the scrape token and set `secrets.existingSecretHasMetricsToken: true`.
- Stored data: CPU, memory and energy stored before this release are inflated (≈2× CPU); the first collection re-collects the last 24 hours and older rows are not rewritten. Rows of earlier calculation versions are not rewritten (new rows use `+range.6`). Default carbon intensities stored as history are deleted (migration 0039).
- Retired or re-measured data: migration 0063 retires active orphaned LoadBalancer and PersistentVolume recommendations until the evidence confirms them; migration 0061 clears `actual_*` requests reverted by old rescans and aligns open-PR rows; phantom "detected" changes are retired and their realized savings cleared; on the first scan, native decreases whose container has no readable restart/OOM history are retired (`reliability_signals_unknown`); migration 0132 merges duplicate active/ignored/`pr_open` recommendations; hours rolled before migration 0130 may lack restart history, so some decreases are withheld. Measurements decided by earlier strategy versions are kept unchanged.
- Ledger method `prorated` is relabelled `projected` (migration 0043); `total_*` savings fields now mean realized (measured) savings only.
- Changed defaults: automation is always on (stop delivery with `automation.policy.pauseDelivery`); `API_AUTH_MODE=session` (`config.apiAuthMode`); `config.corsOrigins` `""` (same-origin) instead of `*`; `config.boavizta.url` `""`; `automation.policy.missingEvidence` `fail`; `IMPACT_REALIZATION_FACTOR` unset (override only); `RECOMMENDATION_LOOKBACK_DAYS` 14; `monitoring.serviceMonitor.namespace` empty (release namespace; Prometheus must select it, and a hand-copied Secret `<release>` there can be deleted); controller memory limit 1Gi; `automation.repositoryAllowlist` `["@discovered"]`; `SESSION_COOKIE_SECURE` is `auto`/`always`/`never` (`true`/`false` map to `always`/`never`).
- Behaviour that now fails closed: collection under a different cluster UID, decreases with unknown OOM/restart history, `missingEvidence`, unmanaged or ambiguous resource bindings, remote GitOps destinations. Browser modes no longer generate `GREENKUBE_API_KEY`; push API keys must be named after their client (`push:<key name>`).
- Rankings change on every cluster (expected value with the capacity weight), and with the default `safest` consensus a lower VPA proposal no longer wins (`RECOMMENDATION_CONSENSUS_POLICY=priority` restores it).
- OpenCost is strongly recommended: without it and without a bundled price catalog no cost saving can be verified. On Cilium, Kubernetes API egress needs a CiliumNetworkPolicy (`docs/security-model.md`). Aggregated metrics retention stays unbounded by default.

**Measurement and data integrity**
- Range queries exclude pod cgroup and pause series, merge duplicate cAdvisor series with `max`, and choose container or pod-cgroup series per pod; OpenCost is queried on `/allocation/compute` over the exact window.
- Every collection window is committed in one transaction; re-collections never degrade stored rows (better cost source wins) and replace the window as one generation; `greenkube start --last` re-collects idempotently; failed carbon-intensity or Kubernetes list reads fail the window instead of dropping rows.
- PostgreSQL stores additive metric measures and ledger values in double precision and uses 64-bit ids (large tables converted online); time buckets are computed in UTC; Kubernetes lists are paginated.
- Unknown instance types get a power profile from CPU capacity; rows priced with the flat `DEFAULT_COST` carry `cost_source=none`; pods without OpenCost cost are charged at list price or custom rate; `is_estimated` gives way to provenance.

**Change measurement and verdicts**
- Fixed-horizon verification: windows start at rollout completion, apply detection waits for the full rollout, the baseline ends at the provider's merge time (or the apply time for manual changes), `not_realized` and `inconclusive` are terminal, and projection accuracy is reported, not gated.
- A saving is `realized` only when its interval's lower bound clears the noise floor and 5 days of post-window hours are aligned; cost is measured at constant prices and CO2e at the hourly carbon factor; template-only changes are a mild confounder.
- Health gates compare with the baseline window (restarts per replica-hour, throttling, readiness); OOM-driven increases compare OOM rates.
- Hand-applied and adopted recommendations are measured by change measurement; the interim verifier remains only with `CHANGE_MEASUREMENT_ENABLED=false` and decides on a 90% interval.

**Savings ledger**
- `GET /recommendations/savings` returns separate, never-summed blocks (`projected_annual`, `run_rate_annual`, `realized_to_date`, `projected_to_date`, `user_declared_*`) and answers `503` when the ledger is unreadable; user-declared savings on `PATCH /recommendations/{id}/apply` must be finite and at most 10× the projection and accrue as `user_declared`, never as measured.
- `greenkube_co2e_savings_attributed_grams_total` and `greenkube_cost_savings_attributed_dollars_total` are counters with a `method` label; `greenkube_dashboard_savings_*_total` carry measured savings only; savings gauges reload at most every 60 s; ledger entries keep their projection currency.
- `PATCH /recommendations/{id}/apply` accepts only `active` and `pr_open` records and ignore/restore follow the state machine (`409` otherwise).

**Recommendations and sources**
- Scans run in the controller, bounded in memory (summaries from the `workload_usage_hourly` rollup), and the API never scans on startup or on reads: `refresh=true` only queues a run (`X-GreenKube-Run-Id`). Scan failures of one source are a degraded success. The first scan runs right after the initial collection; evidence warm-up counts from the first collected sample.
- Percentiles use per-step values; projections annualize over each target's observed coverage (Impact Model 2.0.0); deletions on fixed-capacity clusters take the realization factor (Impact Model 3.0.0); fixed-node request changes show "Allocated cost (chargeback, not billed)".
- New `pr_open` status while a pull request is open; decreases no longer flap (decrease floor) and proposals that jitter keep their revision; OOM kills and restarts count only under the current container configuration.
- Deletions are never low risk; infrastructure namespaces, operators and DaemonSets are never targeted by zombie, idle-namespace or off-peak advice; pod-level usage never yields an increase.
- `RECOMMENDATION_RANKING_WEIGHTS` is ignored; push clients and pushed signals count as at most one independent vote; Kubecost 3.x reads node-group sizing from `/model/savings/clusterSizingETL`.

**GitOps delivery**
- Plan execution runs in the controller (`plan-executor` loop); optional worker pods via `worker.replicas`; the API is stateless and safe to scale (`api.replicas`, `controller.replicas` for HA).
- Previewing a workload again supersedes its unapproved plans; moved branch heads are accepted only when no render input changed; plan branches are created at the validated commit; HPA gates apply only to the resource and target type an HPA scales on. Verified webhooks for untracked pull requests are stored as `ignored` (`202`), never retried; planning a non-active recommendation answers `409 not_active`.
- A Git provider is marked `verified` after any successful authenticated call; Gitea creates missing labels; roles are enforced per mutating route (`write`, `approve`, `plans:deliver`, `admin`).
- Several containers per Helm plan; plans editing limits say to revert the PR instead of promising automated rollback; quantities are written in canonical units.

**Helm chart and operations**
- Controller and worker use the `Recreate` strategy (`controller.strategy`, `worker.strategy`); a stopping leader releases the Lease at once; helper images are pinned by digest; API liveness uses `/api/v1/health/heartbeat`.
- `auth.session.secureCookie: auto` renders `Secure` cookies behind an HTTPS Ingress or HTTPRoute; the auto-detected cluster name is the same in every pod.
- Installation guide rewritten as one path (`helm install`, sign in, optionally one Secret for Git); README, concepts and roadmap follow the loop positioning; the Grafana dashboard is titled "GreenKube — Savings & Footprint" (uid `greenkube-fingreenops` kept).
- CI coverage gate raised to 80%, every GitHub Action pinned by SHA, `greenkube/greenkube:latest` tracks the newest release only.

**UI**
- Overview leads with measured savings, pending verification and projected opportunity, never summed; savings read **Measured allocated savings** ("Realized" is reserved for billed savings); navigation is Overview, Recommendations, Changes, Ledger, Footprint, Automation, Sources, Setup, Settings (`/report` and `/nodes` redirect under `/footprint`).
- Sort options are labelled truthfully (**Ranking score (default)**, projected cost and CO₂e sorts); closing an unapproved plan preview discards it.
- The Settings page and connectivity popup are read-only (configuration as code); the API client uses session cookies and CSRF tokens; one card each for Argo CD and Flux.
- A memory decrease whose OOM history could not be read (`oom_history_unknown`) is now an eligibility warning instead of a blocker: a person may open its pull request. The warning names the cause (`oom_unknown_no_usage_summary`, `oom_unknown_signal_failed`, `oom_unknown_signal_unavailable`) and what to check before merging; the UI shows it on the recommendation, the detail page and the plan review, and the pull-request summary repeats it.
- **README rewritten** (about 90 lines instead of 185): the promise, the four steps of the loop, released capacity for clusters that do not shrink, a three-step quick start (demo, Helm install, Git Secret), the principles and the documentation links. The "What works today" table is gone; the changelog's *Unreleased* section is the list of shipped features.
- **Recommendation sources docs:** a new [Vertical Pod Autoscaler](docs/sources/vpa.md) page (recommendation-mode VPAs, `engine_family: vpa` shared with Goldilocks, what is skipped and why); `docs/sources/goldilocks.md` now covers Goldilocks only, and `docs/sources/nodepool.md` is titled "Karpenter and the Cluster Autoscaler" and documents the NodePool consolidation recommendation. `docs/sources.md` and the README link both.
- **Demo data shows the new loop:** active rightsizings carry an Impact Model projection (released capacity, allocated and billable savings, one fixed-pool recommendation with a realization factor of 0), evidence, risk and confidence; applied recommendations are verified with measured rates (one stays inconclusive), and the savings ledger is backfilled through the production attributor (projected until the measurement, measured afterwards). Three measured changes are recorded with their decided measurement and a capacity verdict computed by `greenkube.verify.capacity` (`released`, `partially_released`), so the Overview, the Changes board and the change page are populated.
- **Screenshots redone** from the current UI and demo data: `assets/demo-dashboard.png`, `demo-recommendations.png`, `demo-automation.png` (now the Changes board), `demo-nodes.png`, `demo-report.png`, and a new `demo-change.png` (a measured change with its released capacity).
- **UI wording:** the sidebar tagline is "Kubernetes optimization loop" (the retired "FinGreenOps Platform" claim is gone), the Recommendations subtitle reads "Ranked, evidence-backed changes, ready as pull requests" and the report page no longer says "FinGreenOps".
- **Savings read as savings:** recommendation cards, the measured-savings tab and the Overview show "saves $232.53/yr" and "saves 69.90 kg CO₂e/yr" instead of "-$232.53" and "-69.90 kg"; a negative value (an increase) reads "costs ... more/yr".

### Deprecated
- `refresh=true` on recommendation reads (only queues a run).
- The bootstrap key `GREENKUBE_API_KEY` (still an admin key unless `auth.legacyApiKey` says otherwise); use accounts and scoped keys.
- A `signals:write` key on the KRR and Polaris report endpoints (use `signals:report`; accepted for one minor release with a `Deprecation` header). The flat `total_*`, `measured_*` and `prorated_*` fields of `GET /recommendations/savings`, `is_estimated`, `RECOMMENDATION_RANKING_WEIGHTS` and the worker's `--queue` option.
- The Helm values listed in the Upgrade notes (mapped for one minor release).

### Removed
- Routes `POST /api/v1/recommendations/{id}/apply-pr`, `GET /api/v1/recommendations/{id}/apply-pr/eligibility`, `GET /api/v1/automation/operations/{operation_id}` (now `404`; use the plans API and `eligibility`) and `POST /api/v1/repository-bindings`; `POST /api/v1/config/services` answers `410 Gone`.
- Settings `LOW_NODE_CPU_THRESHOLD` (`config.lowNodeCpuThreshold`), `PROMETHEUS_QUERY_RANGE_MAX_SAMPLES` (`config.prometheus.queryRangeMaxSamples`) and `SERVICE_URL_ALLOWLIST` (`config.serviceUrlAllowlist`).
- Helm: `values-production.yaml`, the standalone two-container pod, the production deployments, the migration Job, credential-check and ServiceMonitor CRD hooks, the `automation.providers[].tokenSecret` and `repositories` aliases (rendering fails with the replacement keys) and the API's release Secret writer Role.
- `UNDERUTILIZED_NODE` recommendations (duplicate of `OVERPROVISIONED_NODE`), batch-relative ranking normalization, the instant collection path, runtime configuration persistence (migration 0059 drops `configuration_overrides`) and the per-tab API-key sign-in.

### Fixed

**Measurement and data integrity**
- No more doubled CPU and memory from summed pod-cgroup or duplicate series; consecutive windows no longer share, skip or shift samples; multi-step ranges store each step's own values; NaN/Inf samples are dropped.
- Hourly compression no longer loses samples; same-named pods in different namespaces no longer share one OpenCost cost; NaN, missing or zero OpenCost costs are no longer trusted zeros.
- Stale or default carbon intensities are no longer used as measured; the intensity recompute updates container rows too.
- Collection windows with no data, re-collections and missing pods no longer erase or silently degrade stored rows; container rows survive a container without a sample; mixed-runtime clusters keep pods with pod-level series only. Upgrades of large PostgreSQL databases no longer crash-loop on statement timeouts, and the controller is no longer killed mid-migration.

**Savings ledger and verification**
- Ledger compression no longer loses half of the savings; accrual no longer double counts on restart or failover; failed, inconclusive or reverted outcomes no longer accrue; superseded changes stop accruing; deletions are never booked at configured default rates.
- Traffic normalization no longer inflates savings (~4×); multi-replica changes no longer always go to rollback review; container-scoped changes are measurable; OOM, readiness and throttling gates work; CPU and memory records no longer each claim the whole workload delta. Phantom applied changes and merged PRs showing identical before and after requests are fixed; regenerated advice is no longer recorded twice.

**Recommendations**
- Rightsizing prices CPU and memory separately and never claims more than the container costs; request cuts no longer claim usage-based CO2e; young workloads are no longer projected 168 times too low.
- Absurd memory increases from Metrics API page-cache inflation are bounded by the pod; OOM-killed containers get a decisive increase with limit headroom, never a decrease; failed OOM or restart queries are no longer read as zero.
- One failed source or analyzer no longer blocks or stales others; ranking no longer depends on the batch or filters; sorting is by score instead of text priority; ignoring survives the next scan.
- LoadBalancers and PersistentVolumes in use (Gateway API, EndpointSlices, StatefulSets, snapshots) are no longer recommended for deletion; bare-metal or address-less LoadBalancers no longer project a cloud bill; Karpenter NodePool requirements are read.
- The zombie analyzer no longer flags workloads without any CPU sample, and the Prometheus exposition no longer exports NULL usage as 0. Helm `config.recommendations.vpaEnabled: false` and `karpenterEnabled: false` are honoured.

**GitOps delivery**
- Sync is confirmed when a later commit contains the merge; GitHub PRs closed unmerged and GitLab fast-forward or squash merges are handled; PRs merged or closed while a lease was lost are adopted; plans stuck in `applying` are recovered.
- Flux Kustomizations with `targetNamespace`, namespace-less Argo CD overlays, Helm charts without `metadata.namespace` and Kustomize rollbacks plan correctly; changes Flux would alter after GreenKube's patch fail closed.
- Provider rate limits reschedule instead of failing plans; best-effort `403`s no longer mark providers in error; GitHub review requests skip the PR author and invalid logins.
- Two plans for one workload can no longer be created or approved concurrently; retention no longer breaks plan reads. Pull-request records of repositories removed from the allowlist back off and are detached instead of erroring every tick.
- Deletion pull requests are invalidated when the resource is used again; increase-only reliability plans pass a crash-looping or self-degraded rollout gate.

**Helm chart and operations**
- The chart installs again (controller metrics port name over 15 characters); the first Argo CD sync no longer blocks on the credential check; the retained-volume guard works under Argo CD.
- ServiceMonitors scrape again with the token in the release namespace (`monitoring.serviceMonitor.prometheusNamespace` for the RoleBinding); scrape tokens from mounted Secrets are read.
- The bundled PostgreSQL runs without a root init container (Pod Security `restricted`); `helm install --wait` with `postgres.backup.enabled` no longer times out on `WaitForFirstConsumer`; backups honour `config.db.sslMode`; `GreenKubeDatabaseUnavailable` is scoped to the release namespace; the documented restore drops the schema before replaying the dump.

**UI and API**
- The API key is no longer asked after every reload; page loads no longer trigger full scans; `401` always opens sign-in; namespace filters are consistent; amounts no longer hard-code `$`; viewers no longer see mutating actions; pull-request links land on real pages with the Gitea web URL.
- `GET /api/v1/recommendations/active?source=` matches consensus sources; the time to the first pull request is no longer overstated. Stale documentation corrected; the methodology states the known false-`realized` rates of a 7-day baseline.
- `automation.autoApprove` never approves a plan whose recommendation carries `oom_history_unknown` (new `auto_approve_blockers` code `oom_history_unknown`); before, a `maxRisk: medium` policy could approve such a decrease automatically.
- **VPA without `updateMode`:** a VPA whose `updateMode` is unset was read as recommendation mode (`Off`), although the VPA API defaults it to an active mode. It is now skipped and counted as `Auto (default)` in the `skipped_vpas` diagnostic.
- **One accounting basis in the savings ledger (LED-01).** Projected savings now accrue on the allocated basis, the basis change measurement compares with (the Impact Model's `allocated_cost_annual` and the allocated CO2e of the measured components), instead of the expected-billable basis (allocated x realization factor). On clusters with a realization factor of 0 (fixed capacity, minikube, most on-prem) the projection no longer accrues 0, and a restatement replaces a projection with its measurement like for like. The billable expectation stays a derived figure in the savings report (`realization.projected_allocated_cost_annual`, `realization.projected_billable_cost_annual_expected`), never accrued. Recommendations without an Impact Model projection keep their legacy value, labelled `billable_expected`.
- A verified change is restated for every hour of `[applied_at, verified_at)` that no projected or measured accrual covers (`restatement:slot:{rec}:{slot}:{quantity}`), so a zero or missing projection no longer loses the measured saving of the measurement window. The pass is state-derived: the first pass after upgrade writes the missing restatements of changes already verified.
- **User-facing savings numbers come from the hash-chained ledger v2 (LED-04).** The `*_to_date` blocks of `GET /api/v1/recommendations/savings` (and the overview hero), the `greenkube_*_savings_attributed_*` / `*_reversed_*` counters and the `greenkube_dashboard_savings_*` window gauges are now the restated ledger v2 totals, so a verified change shows its measured saving from `applied_at`, not only from `verified_at`, and reversals are visible to Prometheus. Blocks carry `source` (`ledger_v2`, or `ledger_v1` with the diagnostic `ledger_v1_fallback` before the ledger has a cluster identity) and the accrued book alone in `accrued_only_cost` / `accrued_only_co2e_grams`. The counters and dashboard windows switch to ledger v2 only once its accrued book reconciles with the v1 rows (the projector caught up), and stay on it for the life of the process; the switch can still move a counter once (restatements and reversals are counted from then on), which Prometheus may read as one reset.
- **Refuted projections are reversed and measured losses booked (LED-03).** On every ledger pass the projected accruals of a `not_realized` change are reversed (reason `not_realized`), or restated at the measured rate when the measurement found a loss (negative `measured_allocated`). A re-measurement restates again from the new measurement (the earlier restatement is reversed), also for projections an earlier `not_realized` verdict refuted. The projections of an `inconclusive` change are kept but reported apart in `projected_unverified_to_date` and left out of `projected_to_date`.
- **Rollback supersession is state-derived (LED-08).** Every ledger pass supersedes the accruals of recommendations in `rollback_review`, `reverted`, `failed` or `stale`, so a failed one-shot call or an accrual written after it is caught up; a dismissed rollback review reinstates the accruals written before it (reason `reinstatement`, `restatement` book). `greenkube_ledger_unreconciled_recommendations` counts the recommendations a pass had to catch up.
- An OpenCost outage, rejected request (HTTP 401, 503), invalid answer or an empty answer after OpenCost priced earlier windows no longer switches the window to a trusted list-price cost silently: the rows keep a modeled cost labelled `opencost_pending` (never trusted), the step records carry `cost_status = opencost_pending`, and the collection job re-prices the window from OpenCost once it answers again (COST-01, RECON-09).
- `OpenCostCollector.collect()` raises `OpenCostUnavailableError` instead of returning an empty list when the configured or discovered OpenCost fails, and reports `last_status` (`ok`, `empty`, `not_configured`).
- OpenCost allocations that carry no usable cost (no running time, a zero for a pod with requests or usage, or a sub-precision figure that cannot be rebuilt from node prices) are stored with `cost_source = none` and get the modeled cost, instead of a trusted `opencost` zero or rounded value; `is_cost_trusted` refuses a zero OpenCost cost on any row with usage (COST-05, RECON-11).
- In windows of several steps (catch-up, backfill) each row receives its pod's OpenCost cost by the pod's running seconds inside the row's step, so the stored cost of a window equals OpenCost's allocation whatever the chunking; pods that covered steps only partly were over-costed (COL-10, RECON-08). `CALCULATION_VERSION` `+range.7`.
- Container rows split the pod cost by reservation `max(request, usage)` (CPU cost by CPU reservation, RAM cost by memory reservation) instead of usage, in collection and in `greenkube recompute`; container costs and the unit prices of rightsizing projections of multi-container pods were wrong (COST-02, IMP-02). `CALCULATION_VERSION` `+range.8`.
- Orphaned PersistentVolume and LoadBalancer costs are read from OpenCost's `pvs` and `lbAllocations` fields of `/allocation/compute` (the former query parameter and fields did not exist, so the flat defaults always applied), annualized over the hours the asset existed, observed zeros kept, LoadBalancers matched on their exact namespace; recommendation texts print the configured currency instead of `$` (COST-03).
- Every OpenCost request (allocation, node prices, PersistentVolume and LoadBalancer costs, probe) carries the configured `OPENCOST_BEARER_TOKEN`, or `OPENCOST_USERNAME` and `OPENCOST_PASSWORD`; only the discovery probe sent them, so an authenticated OpenCost or Kubecost yielded no cost; a 401 or 403 answer is logged as an error and sets the gauge `greenkube_opencost_auth_failed` (COST-06).
- OpenCost is queried with `aggregate=cluster,namespace,pod` and, when `OPENCOST_CLUSTER_ID` is set, filtered on that cluster; an answer covering several clusters without an id stores no OpenCost cost instead of merging same-named pods of different clusters; allocations, node prices and PersistentVolume and LoadBalancer items of another cluster are also dropped on GreenKube's side, for OpenCost or Kubecost releases that ignore `filter=` (COST-10).
- The OpenCost entry of `GET /health/services` calls the allocation endpoint with the configured credentials and reports `degraded` with the status code when it fails (401, 403, 5xx), instead of probing `/healthz` only (COST-01, COST-06).
- While OpenCost is known for the cluster (a persisted `opencost_cost` watermark row, beyond the backfill horizon), a pod whose allocation carries no usable cost and an OpenCost whose URL cannot be resolved give `opencost_pending` rows and pending steps instead of a trusted modeled cost. Pending steps that leave the horizon become `opencost_pending_expired` with a warning and the counter `greenkube_collection_cost_pending_expired_steps_total`; only steps with rows count as re-priced (COST-01 review).
- A change measurement whose baseline cost and after-window cost come from different price bases (OpenCost allocation versus list prices) is contaminated (`price_basis_change`) instead of booking the price switch as a saving; hour buckets record their `cost_bases` (COST-01 acceptance d).
- The OpenCost health check reads the configuration it is given (TLS verification, credentials) and sends the cluster filter; a container with no request and an unknown usage no longer gets a zero cost share: the resource falls back to an equal split (review).

### Security
- Wildcard CORS is never combined with credentials; CORS is same-origin by default.
- API rate limiting keys per route and uses the rightmost untrusted `X-Forwarded-For` hop; sign-in lockout is per username and client IP; trusted-proxy checks judge the TCP peer.
- The default Git token is never sent to a host it was not issued for; the self-hosted provider probe contacts only `https://` non-local hosts; remote chart fetches never follow cross-host redirects with tokens or reach loopback, link-local or metadata addresses; Azure Advisor paging never leaves Resource Manager.
- The default credentials Secret is read through a Role limited to its name; the API can no longer patch the release Secret.
- Push clients are bound to their API key, and pushed votes cannot manufacture the two independent votes needed for auto-approval; a KRR report covering several clusters is refused unless `?cluster=` selects this one.
- Only a last-line PR footer and comments written by GreenKube's own identity are trusted.
- No full-admin key is generated in browser modes; `auth.legacyApiKey` restricts the legacy key; HSTS on HTTPS; PostgreSQL NetworkPolicy on by default (`postgres.networkPolicy.enabled`, `additionalIngress`); install notes warn about an accepted admin key or an exposure with empty `config.trustedProxyIps`. Release images and the chart are signed with provenance and SBOM (`SECURITY.md` explains verification).

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
