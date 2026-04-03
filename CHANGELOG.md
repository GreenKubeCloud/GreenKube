# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/GreenKubeCloud/GreenKube/compare/v0.2.4...HEAD
[0.2.4]: https://github.com/GreenKubeCloud/GreenKube/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/GreenKubeCloud/GreenKube/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/GreenKubeCloud/GreenKube/compare/v0.1.0...v0.2.2
[0.1.0]: https://github.com/GreenKubeCloud/GreenKube/releases/tag/v0.1.0
