# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] — 2026-11-22

GreenKube is now an optimization loop: recommend from its own measurements, deliver the change as a GitOps pull request, then measure the saving and book it in a savings ledger. Read the upgrade notes below and `docs/upgrading.md` before upgrading from 0.3.1.

### Upgrade notes

- **Back up the database first:** migrations run automatically at controller startup; the API answers `503 migrating` until they finish. Rolling back after the migrations is a restore, not `helm rollback` (`docs/operations/backup-restore.md`). Upgrade with `helm upgrade --reset-then-reuse-values` or `-f <your-values.yaml>`; `--reuse-values` stops before changing anything.
- **PostgreSQL only:** releases with `config.db.type=sqlite` fail before Helm changes anything and print the `greenkube db copy` commands that move the data into PostgreSQL.
- **One install topology:** `<release>-api`, `<release>-controller`, `<release>-renderer` and the bundled PostgreSQL; the ConfigMap is `<release>-config`. `profile`, `replicaCount`, `production.*`, `automation.enabled`, `automation.mode`, `automation.workerMetricsPort`, `rendererSandbox.*` (now `renderer.*`), `collector.*` (`collector.metricsPort` is now `controller.metricsPort`), `resources`, `apiResources` and the SQLite values are still mapped for one minor release and listed under **DEPRECATED VALUES** in the install notes. Values and environment variables of removed features (see Removed) no longer exist: leftover ones are ignored, but remove them.
- **Breaking for `rbac.create=false`:** installs that relied on a generated API key or PostgreSQL password no longer render; set `secrets.existingSecret=<release>` or supply `secrets.apiKey` and `postgres.auth.password`. With `secrets.existingSecret`, add the scrape token and set `secrets.existingSecretHasMetricsToken: true`. Other upgrades keep the Secret `<release>`.
- **Sign-in and keys:** the default is now `API_AUTH_MODE=session` (accounts); the first admin password is in the Secret `<release>-initial-admin` unless `auth.admin.existingSecret` is set. A 0.3.1 install in `api_key` mode keeps browser access with its key. Scrapers of your own that send `GREENKUBE_API_KEY` to the controller or worker metrics listeners must switch to `GREENKUBE_METRICS_TOKEN`.
- **Ledger chain key:** the credentials bootstrap Job generates `secrets.ledgerChainKey` (`LEDGER_CHAIN_KEY`) once. Do not lose or regenerate it: the ledger writer refuses to append when the head is keyed and the key is missing or different. Rotate with `secrets.ledgerChainPreviousKeys` and `greenkube db ledger-rotate-key`.
- **Changed defaults:** automation is always on (stop delivery with `automation.policy.pauseDelivery`); `config.corsOrigins` `""` (same-origin) instead of `*`; `config.boavizta.url` `""`; `automation.policy.missingEvidence` `fail`; `automation.repositoryAllowlist` `["@discovered"]`; `RECOMMENDATION_LOOKBACK_DAYS` 14; `monitoring.serviceMonitor.namespace` empty (release namespace, which Prometheus must select); controller memory limit 1Gi; `SESSION_COOKIE_SECURE` takes `auto`/`always`/`never`.
- **Stored data and recommendations:** CPU, memory and energy stored by 0.3.1 are inflated (about 2x CPU); the first collection re-collects the last 24 hours and older rows are not rewritten. Default carbon intensities stored as history are deleted. Run `greenkube recompute --since <raw retention>` to re-allocate raw rows with the new Impact Model. Some open recommendations are retired or merged at the first scan, ledger method `prorated` is relabelled `projected`, `total_*` savings fields now mean measured savings only, and rankings change on every cluster.
- **Prometheus and Grafana:** the API's `/prometheus/metrics` no longer exports footprint, recommendation or dashboard series, and `dashboards/greenkube-grafana.json` is removed; import `dashboards/greenkube-operations.json` (replacements per series in `docs/upgrading.md`).
- **Prerequisites:** OpenCost is strongly recommended (without it and without a bundled price catalog no cost saving can be verified). On Cilium, Kubernetes API egress needs a CiliumNetworkPolicy (`docs/security-model.md`).

### Added

- **Gap-aware collection:** collection runs on wall-clock steps behind a per-installation watermark and catches up missing windows oldest first. Settings `PROMETHEUS_SCRAPE_INTERVAL`, `COLLECTION_SAFETY_LAG`, `COLLECTION_MAX_BACKFILL` and `COLLECTION_K8S_LIST_MAX_FAILURES` (Helm `config.prometheus.scrapeInterval`, `config.collection.*`); `greenkube start --last <range> [--force]` re-collects a range.
- **Collection coverage and health:** every step is recorded as `collected`, `empty`, `failed` or `gap`; `GET /api/v1/collection/coverage` returns the coverage ratio and holes, and `GET /api/v1/health` reports the watermark, its lag, the last hole, compression lag and the container usage source. The controller refuses to collect under a different cluster UID unless `config.collection.allowClusterUidChange` is set (`GreenKubeClusterIdentityMismatch` alert).
- **Provenance and metadata history:** pod and container rows record the source of each dimension (`cost_source`, `usage_source`, `power_source`, `intensity_source`, ...), OpenCost cost components, container limits, native sidecars and the per-step memory peak. Pod and node snapshots give historical rows the owner, requests, zone and profile valid at the sample time; `INTENSITY_MAX_AGE` (Helm `config.intensityMaxAge`) bounds the age of a carbon intensity.
- **Impact Model:** reservation-aware allocation of energy, operational and embodied CO2e and cost, with an explicit `__idle__` row per node so pod and idle rows add up to node totals; memory power (`POWER_MEMORY_W_PER_GIB`); CPU used outside pods; GPU power from DCGM or, without DCGM, from a TDP estimate (`config.power.gpuAssumedUtilizationPct`). `greenkube recompute --since … [--until] [--all] [--dry-run]` re-allocates stored raw rows (`docs/methodology/impact-model.md`).
- **Embodied emissions:** values come from Boavizta, the node annotation `greenkube.io/embodied-kg` or `config.boavizta.embodiedKgByInstanceType`, with a per-vCPU fallback (`config.boavizta.embodiedKgPerVcpu`, `embodiedKgPerGib`); rows record `embodied_source`. Nodes sharing one host (kind, k3d, multi-node minikube) split its idle power and embodied emissions (`config.power.sharedHostDetection`).
- **Node idle share in reports:** `/metrics/summary`, `/metrics/timeseries` and `/report/summary` return the idle share and `cluster_total_*` (allocated + idle); `/report/export?include_idle=true` adds idle and cluster-total rows (`row_kind` column); the Footprint page shows allocated, idle and cluster totals side by side.
- **Impact projections and cost fallback:** every recommendation carries an `impact` projection (cost and CO2e, capacity released, unit prices and their source). Cost falls back from OpenCost to observed node costs, a bundled on-demand price catalog (AWS, Azure, OVHcloud, Scaleway), then custom rates (`impact.pricing.*`); `CARBON_PRICE_PER_TONNE`; `OPENCOST_CLUSTER_ID` (`config.opencost.clusterId`) selects the cluster in a multi-cluster OpenCost. The realization factor is derived per node pool from Cluster Autoscaler, Karpenter, disruption annotations and PodDisruptionBudgets; `IMPACT_AUTOSCALER_HINT` (`impact.autoscalerHint`) declares an autoscaler detection cannot see.
- **Released capacity:** projected (core-, GiB-, replica- and node-hours per year) on every recommendation and measured with a capacity verdict on each decided change, never converted to money. `GET /api/v1/recommendations/export?format=csv|json` exports active recommendations.
- **Change records and measurement:** every applied workload revision is stored as an immutable change with a frozen 7-day baseline (`GET /api/v2/changes[/{id}[/measurements]]`). Lifecycle `applied → measuring → realized | not_realized | inconclusive | contaminated`, then `regressed` or `closed`, with a load-driver counterfactual, a bootstrap 90% interval, constant-price cost, hourly carbon-factor CO2e and confounder detection. Settings `CHANGE_MEASUREMENT_*` (Helm `config.recommendations.changeMeasurement.*`); method in `docs/methodology/savings-measurement.md`.
- **Drift and reliability fixes:** a realized change moves to `regressed` when it loses `CHANGE_DRIFT_THRESHOLD` of its saving for `CHANGE_DRIFT_SUSTAINED_DAYS`. Reliability increases are judged on their OOM-kill rate and their cost is reported as `cost_of_fix_annual`, never as a saving. `POST /api/v1/recommendations/{id}/remeasure` and `/rollback-review/dismiss`.
- **Savings ledger v2:** append-only signed entries per method (`projected`, `measured_allocated`, `realized_billed`, `user_declared`) and accounting basis, with reversals and restatements once a measurement decides, released capacity and the cost of reliability fixes as separate quantities, and a per-cluster hash chain keyed with HMAC (`LEDGER_CHAIN_KEY`) and verified by the controller (`config.ledger.chainVerifyInterval`). `GET /api/v2/ledger/entries`, `/summary` and `/export` (`format=csv` or `focus`).
- **Ledger operations:** `greenkube db ledger-rotate-key` records a chain key rotation; `greenkube db ledger-accept-head` accepts the ledger head after a restore or failover lost entries. Attribution refuses to run when the process clock is more than 5 minutes off the database clock; metrics and alerts cover chain status, reconciliation and clock skew.
- **Scheduled scans and optimization runs:** leader-gated scans every `RECOMMENDATION_SCAN_INTERVAL` (`config.recommendations.scanInterval`); `POST /api/v1/optimization/runs`, `GET /api/v1/optimization/runs[/{id}]` with per-source health and the diff against the previous set.
- **Expected-value ranking and risk model:** each recommendation carries a `value` block (annual value, success probability, incident and effort cost) tuned by `RECOMMENDATION_INCIDENT_COST`, `RECOMMENDATION_ENGINEER_HOUR_COST` and `RECOMMENDATION_CAPACITY_VALUE_WEIGHT`; profiles via `?profile=`. Risk factors cover per-replica headroom, OOM, throttling, restarts, HPA, VPA, PDB and the `greenkube.io/criticality` label; `GET /api/v1/sources/track-record`.
- **Two-way rightsizing:** reliability increases (`RIGHTSIZING_INCREASE_THRESHOLD`, capped by `RIGHTSIZING_MAX_INCREASE_FACTOR`), limit advice (`RECOMMENDATION_LIMITS_POLICY`), a coverage requirement for decreases (`RECOMMENDATION_MIN_DECREASE_COVERAGE_DAYS`) and startup exclusion (`RIGHTSIZING_WARMUP_MINUTES`). Recommendations of workloads that no longer exist become `stale` (`RECOMMENDATION_TARGET_MAX_STALENESS_HOURS`).
- **Eligibility and recommendation v2 API:** every recommendation carries server-side `eligibility` (`can_open_pr`, delivery modes, blockers and warnings with their fix; `docs/reference/eligibility-codes.md`). `GET /api/v2/recommendations[/{id}]` returns the typed proposal, impact, risk, value and revisions; a recommendation never changes under an open plan, newer advice is queued as the next revision.
- **Recommendation sources:** a source SDK and registry (built-in sources, third-party plugins from `greenkube.sources` entry points) with isolation, timeouts and persisted runs; `GET /api/v1/sources`, `/runs` and `/signals`; `SOURCES_PLUGINS_ENABLED`, `SOURCES_CONFIG` (`sources.settings`). External inputs: VPA (including Goldilocks), Robusta KRR reports (`POST /api/v1/sources/krr/reports`, with a CronJob example in `docs/sources/krr.md`) and the push API `POST /api/v1/signals` (schemas in `schemas/`). Tool floors (`SIGNAL_TOOL_FLOORS`) and changes below `SIGNAL_MIN_CHANGE_*` do not count as votes, VPAs are ignored until mature (`VPA_MIN_SAMPLE_HOURS`; the ClusterRole reads `verticalpodautoscalercheckpoints`), and `consensus.ignored_votes` lists what was set aside.
- **Deletion recommendations:** orphaned LoadBalancers and PersistentVolumes must look unused for `DELETION_MIN_UNUSED_DAYS` and list their evidence; they become reviewed pull requests (never auto-approved), re-checked every 5 minutes and closed when the resource is in use again. Opt out with `greenkube.io/ignore: "true"` or `greenkube.io/keep`.
- **Manual mode:** `GET /api/v1/recommendations/{id}/manual` returns `kubectl` steps, a patch and a diff; `POST …/manual/applied` records a hand-applied change so it is measured; an **Apply manually** panel in the UI.
- **Always-on GitOps automation:** Git providers are inferred from Argo CD and Flux sources and delivery activates when the Secret `greenkube-git-credentials` exists (a token, or a GitHub App with signed commits), without restart. Delivery modes `automation.delivery.mode` (`direct`, `patch`, `ci`) with `greenkube plan apply-patch` / `report-delivery`; policy-based auto-approval `automation.autoApprove`.
- **Pull requests and plans:** PR template v2 with configurable title, labels, drafts for high-risk plans, CODEOWNERS reviewers, a `greenkube/validation` commit status and one lifecycle comment (`automation.pr.*`). Request increases, limits, native sidecars and init containers, Argo Rollouts and remote Helm charts (HTTP and OCI, `automation.helm.*`) can be delivered. `DELETE /api/v1/automation/plans/{id}`, `POST …/abandon` and a `cancelled` plan status with `status_reason`.
- **Git provider observability and setup:** per-operation call budget, `greenkube_git_provider_*` metrics and the `GreenKubeGitProviderRateLimited` alert; a guided setup checklist (`GET /api/v1/setup/status`, `/setup` page) with repository suggestions; a daily renderer egress canary.
- **Accounts and sessions:** `API_AUTH_MODE=session` with server-side sessions, `HttpOnly` cookies, CSRF protection and login rate limiting (`/api/v1/auth/*`, `auth.session.*`); `greenkube auth reset-password`. In `api_key` mode the browser signs in with an API key exchanged for a signed, expiring cookie.
- **API keys and single sign-on:** scoped API keys (`read`, `write`, `approve`, `signals:write`, `signals:report`, `plans:deliver`, `metrics:read`, `admin`) and a generated scrape token `GREENKUBE_METRICS_TOKEN` used by the ServiceMonitors; `proxy` (`auth.proxy.*`) and `oidc` (`auth.oidc.*`) modes with group-to-role mapping (`auth.roles.*`). Guides `docs/authentication.md` and `docs/security-model.md`.
- **Helm chart:** a credentials bootstrap hook that generates credentials once and keeps them on uninstall; `values.schema.json`; Ingress and Gateway API HTTPRoute templates; per-component scheduling, `global.imagePullSecrets` and PodDisruptionBudgets; optional `postgres.backup` CronJob and `postgres.shmSizeLimit`; `greenkube db copy` to move a 0.3.x SQLite database into PostgreSQL.
- **Operations monitoring:** controller metrics and health on `controller.metricsPort` (9102: `/healthz`, `/readyz`, `/loops`) with a ServiceMonitor; PrometheusRule groups for the controller, loops, ledger and operations (`monitoring.prometheusRule.*`); the **GreenKube Operations** Grafana dashboard `dashboards/greenkube-operations.json`.
- **UI and docs:** a savings-first **Overview**, recommendation detail, **Changes**, **Ledger**, **Sources**, **Setup** and **Footprint** pages, and account, session, API key and user pages; recommendation cards show expected value, released capacity, warm-up ETA, **Open PR** and **Apply manually**. `greenkube demo` sets a demo mode reported by `GET /api/v1/health` and shown in the UI. New guides: `docs/upgrading.md`, production sizing in `docs/installation.md`, `docs/concepts.md` and a quickstart (`make quickstart`).

### Changed

- **Process layout:** collection, scans and plan execution run in the controller; the API is stateless and safe to scale (`api.replicas`, `controller.replicas`, optional `worker.replicas`). The API never scans on startup or on reads: `refresh=true` only queues a run. Controller and worker use the `Recreate` strategy.
- **GreenKube's measured value decides the size:** VPA, Goldilocks, KRR and pushed signals no longer replace the native rightsizing value; agreeing sources raise its confidence and contested ones lower it, and a source alone never creates a recommendation.
- **Recommendations:** a new `pr_open` status while a pull request is open; percentiles use per-step values and projections annualize over observed coverage; decreases no longer flap; OOM kills and restarts count only under the current container configuration; deletions are never low risk; `RECOMMENDATION_RANKING_WEIGHTS` is ignored.
- **Verification:** the previous (interim) verifier remains only with `CHANGE_MEASUREMENT_ENABLED=false` and its savings are reported apart; otherwise change measurement decides, with windows starting at rollout completion and a saving `realized` only when its interval clears the noise floor.
- **Savings API and metrics:** `GET /api/v1/recommendations/savings` returns separate, never-summed blocks (`projected_annual`, `run_rate_annual`, `realized_to_date`, `projected_to_date`, `user_declared_*`, `opportunity`) read from ledger v2, and answers `503` when the ledger is unreadable. User-declared savings are bounded by the projection; `PATCH /recommendations/{id}/apply` and ignore/restore follow the state machine (`409` otherwise). The savings counters carry a `method` label.
- **Fail-closed behaviour:** collection under a different cluster UID, decreases with unknown OOM/restart history, missing evidence, unmanaged or ambiguous resource bindings and remote GitOps destinations are refused.
- **GitOps delivery:** previewing a workload again supersedes its unapproved plans; plan branches are created at the validated commit; HPA gates apply only to what an HPA scales; roles are enforced per mutating route; Helm plans can change several containers; webhooks for untracked pull requests are stored as `ignored`.
- **UI:** navigation is Overview, Recommendations, Changes, Ledger, Footprint, Automation, Sources, Setup, Settings (`/report` and `/nodes` redirect under `/footprint`); savings read "saves $X/yr" and "Measured allocated savings"; the Settings page is read-only (configuration as code).
- **Build and CI:** the frontend moved to SvelteKit 3 and the image builds it with Node 22; CI pins every action by SHA, gates coverage at 80% and runs an end-to-end loop on Minikube; `greenkube/greenkube:latest` tracks the newest release only.

### Deprecated

- `refresh=true` on recommendation reads (it only queues a run).
- The bootstrap key `GREENKUBE_API_KEY` (still an admin key unless `auth.legacyApiKey` says otherwise); use accounts and scoped keys.
- The flat `total_*`, `measured_*` and `prorated_*` fields of `GET /recommendations/savings`, `is_estimated`, `RECOMMENDATION_RANKING_WEIGHTS` and the worker's `--queue` option.
- The Helm values listed in the upgrade notes (mapped for one minor release).

### Removed

- **Advisory analyzers:** zombie pods, idle namespaces, off-peak scaling, autoscaling candidates, carbon-aware scheduling and over/under-provisioned nodes. Their types are retired and open rows marked `stale`; applied rows and the ledger are kept. Settings `ZOMBIE_COST_THRESHOLD`, `ZOMBIE_ENERGY_THRESHOLD`, `AUTOSCALING_CV_THRESHOLD`, `AUTOSCALING_SPIKE_RATIO`, `OFF_PEAK_IDLE_THRESHOLD`, `OFF_PEAK_MIN_IDLE_HOURS`, `IDLE_NAMESPACE_ENERGY_THRESHOLD`, `CARBON_AWARE_THRESHOLD`, `NODE_UTILIZATION_THRESHOLD` and their `config.recommendations.*` values are gone: remove them.
- **Karpenter NodePool connector and source priority:** `RECOMMENDATION_KARPENTER_ENABLED` (`config.recommendations.karpenterEnabled`) and `RECOMMENDATION_SOURCE_PRIORITY` (`config.recommendations.sourcePriority`) are gone. Karpenter and Cluster Autoscaler state is still read for the realization factor and risk.
- **Large Grafana dashboard:** `dashboards/greenkube-grafana.json` (Grafana.com dashboard 25377); import `dashboards/greenkube-operations.json` instead.
- **Footprint and recommendation series on `/prometheus/metrics`:** every `greenkube_pod_*`, `greenkube_namespace_*` and `greenkube_node_*` family, cluster footprint totals, grid intensity, PUE, `greenkube_dashboard_*` windows, recommendation gauges and the sustainability score (`greenkube_sustainability_score`, `greenkube_sustainability_dimension_score`; it had no API route, UI page or stored data). The API keeps collection freshness and the ledger savings counters; footprint data stays in the Footprint pages and exports (`docs/upgrading.md`).
- **Network, disk and storage dimensions:** network receive/transmit, disk read/write, ephemeral storage and PVC request/usage are no longer collected or returned by the metrics API; their columns keep old values until a later migration drops them.
- **Routes:** `POST /api/v1/recommendations/{id}/apply-pr`, `GET …/apply-pr/eligibility`, `GET /api/v1/automation/operations/{operation_id}` (use the plans API and `eligibility`) and `POST /api/v1/repository-bindings`; `POST /api/v1/config/services` answers `410 Gone`.
- **Settings and runtime configuration:** `LOW_NODE_CPU_THRESHOLD`, `PROMETHEUS_QUERY_RANGE_MAX_SAMPLES` and `SERVICE_URL_ALLOWLIST` (and their Helm values), runtime configuration overrides stored in the database, the per-tab API-key sign-in and `UNDERUTILIZED_NODE` recommendations.
- **Helm:** `values-production.yaml`, the standalone two-container pod, the production deployments, the migration Job, the credential-check and ServiceMonitor CRD hooks, and the `automation.providers[].tokenSecret` and `repositories` aliases (rendering fails with the replacement keys).

### Fixed

- **CPU and memory measurement:** no more doubled CPU and memory from pod-cgroup or duplicate series; consecutive windows no longer share, skip or shift samples; NaN/Inf samples are dropped; CPU of restarted containers adds up inside a window; memory is the working-set average over the step; restarts are counted inside the window, not over the lifetime.
- **Stored rows:** windows with no data, re-collections and missing pods no longer erase or degrade stored rows; failed Kubernetes or carbon-intensity reads fail the window instead of dropping rows; hourly compression no longer loses samples or closes an hour before its rows are collected.
- **Workload identity:** pod owners follow the controller chain (a ReplicaSet resolves to its Deployment or Argo Rollout, a Job to its CronJob); static control-plane pods no longer receive recommendations; the node cloud provider comes from `spec.providerID`, so hybrid clusters keep each node's provider.
- **OpenCost costs:** every request sends `OPENCOST_BEARER_TOKEN` or `OPENCOST_USERNAME`/`OPENCOST_PASSWORD` (an authenticated OpenCost yielded no cost); same-named pods in different namespaces or clusters no longer share a cost; NaN, missing or zero costs are no longer trusted; an outage no longer silently switches to list prices; orphaned PersistentVolume and LoadBalancer costs are read from the right fields.
- **Carbon data:** Boavizta values are used (every lookup failed and fell back to a flat 100 kg per node); stale or default intensities are no longer used as measured; the default zone gets live intensity; the bundled zone intensity defaults were repaired (2024 annual means from Ember, 186 zones); `cloudProvider: on-premise` maps to the default PUE.
- **Recommendations:** rightsizing prices CPU and memory separately and never claims more than the container costs; young workloads are no longer projected 168 times too low; OOM-killed containers get a decisive increase, never a decrease; failed OOM or restart queries are no longer read as zero; one failing source no longer blocks the others; ignoring survives the next scan; `config.recommendations.vpaEnabled: false` is honoured and a VPA without `updateMode` is no longer read as recommendation mode. LoadBalancers and PersistentVolumes in use (Gateway API, EndpointSlices, StatefulSets, snapshots) are no longer recommended for deletion, and bare-metal LoadBalancers no longer project a cloud bill.
- **Verification and ledger:** ledger compression no longer loses half of the savings; accrual no longer double counts on restart or failover; failed, inconclusive or reverted outcomes no longer accrue; traffic normalization no longer inflates savings (about 4x); multi-replica changes no longer always go to rollback review; CPU and memory records no longer each claim the whole workload delta.
- **GitOps delivery:** GitLab merge requests can be opened again (approved plans went `stale`); sync is confirmed when a later commit contains the merge; GitHub PRs closed unmerged and GitLab fast-forward or squash merges are handled; provider rate limits reschedule instead of failing plans; two plans for one workload can no longer be created or approved concurrently; Flux `targetNamespace`, namespace-less Argo CD overlays and Argo CD multi-source Helm applications plan correctly; Helm values files are no longer folded at 80 columns; one repository spelled two ways is recognized as one.
- **Operations and UI:** the bundled PostgreSQL runs without a root init container (Pod Security `restricted`); the API key is no longer asked after every reload; page loads no longer trigger full scans; amounts no longer hard-code `$`; `/report/export` no longer loses the sample at each 7-day chunk boundary.

### Security

- **CORS and rate limiting:** wildcard CORS is never combined with credentials and CORS is same-origin by default; rate limiting keys per route and on the rightmost untrusted `X-Forwarded-For` hop; sign-in lockout is per username and client IP; failed bearer authentications are throttled per client IP.
- **Git credentials:** the default Git token is never sent to a host it was not issued for; remote chart fetches never follow cross-host redirects with tokens or reach loopback, link-local or metadata addresses; only PR footers and comments written by GreenKube's own identity are trusted.
- **Secrets and keys:** the credentials Secret is read through a Role limited to its name and the API can no longer patch it; no full-admin key is generated in browser modes; HSTS on HTTPS; PostgreSQL NetworkPolicy on by default (`postgres.networkPolicy.*`); push clients are bound to their API key and cannot manufacture independent votes; release images and the chart are signed (`SECURITY.md`).
- **Code scanning:** the frontend no longer depends on vulnerable `@sveltejs/kit` 2.x (`npm audit` is clean); log lines quoting request data escape line breaks; oversized durations and steps are rejected; the SPA fallback serves only files listed from the frontend build.

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
