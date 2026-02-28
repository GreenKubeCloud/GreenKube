# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

### Added
- Namespace input validation on all API endpoints (Kubernetes naming rules)
- Contributing guide (`CONTRIBUTING.md`)
- This changelog
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

[Unreleased]: https://github.com/GreenKubeCloud/GreenKube/compare/v0.2.2...HEAD
[0.2.2]: https://github.com/GreenKubeCloud/GreenKube/compare/v0.1.0...v0.2.2
[0.1.0]: https://github.com/GreenKubeCloud/GreenKube/releases/tag/v0.1.0
