# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Recommendation full lifecycle:** Recommendations now support a complete status lifecycle (`open`, `in_progress`, `resolved`, `dismissed`, `snoozed`). New API endpoints allow updating status, bulk-dismissing, and snoozing recommendations. DB migrations `0006` (lifecycle columns) and `0007` (upsert null-fix) applied for both PostgreSQL and SQLite.
- **Frontend recommendation lifecycle UI:** The recommendations page now exposes status filters, per-recommendation status controls (dismiss, snooze, mark in-progress/resolved), and a lifecycle summary on the dashboard.
- **Expanded test coverage:** Nine new test files covering `CollectionOrchestrator`, `MetricAssembler`, `MetricsCompressor`, `Scheduler`, recommender v2, factory, `SummaryRepository` (SQLite), `TimeseriesCacheRepository` (SQLite), and a full recommendation lifecycle end-to-end suite (2 366 lines of new tests).
- **Grafana dashboard overhaul (`scripts/build_grafana_dashboard.py`):** Complete rebuild of the Grafana JSON dashboard generation script with full PromQL aggregation correctness, instant-query bargauges for Top 3 panels, and a `reduce` transformation to fix bar-scale inflation from historical data.
- **Savings attribution system:** New `SavingsAttributor` service (`src/greenkube/core/savings_attributor.py`) prorates projected annual CO₂e and cost savings to the actual observation window. New `SavingsLedger` Pydantic model, abstract `BaseSavingsRepository`, and PostgreSQL/SQLite implementations. DB migrations `0008` applied for both engines. Two new Prometheus gauges: `greenkube_co2e_savings_attributed_grams_total` and `greenkube_cost_savings_attributed_dollars_total`.
- **Grafana dashboard: Sustainability Command Center row** with Sustainability Score gauge, CO₂e/Cost/Energy stats, attributed savings window panels, active-recommendation counters, and three sorted Top-3 bargauges (CO₂e by namespace, Cost by namespace, Recommendation Types).

### Fixed
- **Node recommendation memory usage:** The underutilised-node recommender now correctly factors in memory utilisation alongside CPU, preventing false positives on memory-heavy workloads.
- **Node-level recommendation persistence:** `pod_name` and `namespace` are now allowed to be `NULL` in the DB schema for node-scope recommendations, fixing an integrity error on save.

### Changed
- **CI: automated test-coverage badge update:** The README test-coverage shields are now refreshed automatically by CI on each push to `dev`.
- **All Grafana PromQL expressions deduplicated:** Every expression across all rows uses the appropriate aggregation (`sum(max by (cluster)(…))` for cluster-level scalars, `sum by (namespace)(…)` for namespace breakdowns, `max by (namespace, pod)(…)` for pod-level topk, `max by (node)(…)` for node metrics) to prevent value multiplication from multiple scrape instances.

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
- **Infinite aggregated retention by default:** `METRICS_AGGREGATED_RETENTION_DAYS` now defaults to `-1` (infinite retention), preserving all historical data by default. This is the correct default for CSRD/ESRS E1 compliance, which requires multi-year reporting. Set an explicit positive integer to enforce a rolling window.
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
