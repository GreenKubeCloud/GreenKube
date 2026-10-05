# API Reference

The GreenKube REST API is available under `/api/v1`. Interactive documentation and the OpenAPI schema are available at `/api/v1/docs` and `/api/v1/openapi.json`.

## Authentication

When an API key is configured, send it as `Authorization: Bearer <API_KEY>`. The default Helm values use the `development` environment with an empty key, which leaves protected API routes open; use that default only for local or otherwise protected installations. Production configuration requires an API key. The current API middleware supports API-key authentication; selecting another authentication mode does not enable an OIDC or session implementation.

## Endpoints

All paths below are relative to `/api/v1`.

### Health and configuration

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health status and application version. |
| `GET` | `/version` | Application version. |
| `GET` | `/config` | Current runtime configuration. |
| `GET` | `/health/services` | Health checks for configured data sources (`?force=true` bypasses the cache). |
| `GET` | `/health/services/{service_name}` | Health check for one data source. |
| `POST` | `/config/services` | Apply selected integration URLs or tokens at runtime; see [Configuration](configuration.md). |

The unauthenticated `/api/v1/health/heartbeat` liveness route is excluded from the OpenAPI schema.

### Metrics and dashboard data

| Method | Path | Description |
|---|---|---|
| `GET` | `/metrics` | Paginated combined per-pod metrics. `last`, `namespace`, `offset`, `limit`, and `cursor` are supported. |
| `GET` | `/metrics/summary` | Aggregated cluster or namespace totals (`last`, optional `namespace`). |
| `GET` | `/metrics/timeseries` | Aggregated time series (`last`, optional `namespace`, `granularity=hour\|day\|week\|month`; default `hour`). |
| `GET` | `/metrics/by-namespace` | Metrics grouped by namespace (`last`; optional `namespace` narrows the result). |
| `GET` | `/metrics/top-pods` | Highest-emitting pods (`last`, optional `namespace`, `limit`, 1–50; default 10). |
| `GET` | `/metrics/dashboard-summary` | Precomputed KPI values for dashboard windows (optional `namespace`). |
| `GET` | `/metrics/dashboard-timeseries/{window_slug}` | Precomputed dashboard time series for `1h`, `6h`, `24h`, `7d`, `30d`, `1y`, or `ytd` (optional `namespace`). |
| `POST` | `/metrics/dashboard-summary/refresh` | Schedule a dashboard-cache refresh for the optional `namespace`; returns `202 Accepted`. |
| `GET` | `/namespaces` | List active namespaces. |
| `GET` | `/nodes` | Cluster node inventory. |

Raw `/metrics` listing accepts at most 30 days by default (`config.metricsListMaxRangeDays`); wider exports should use the report endpoints.

### Recommendations and automation

| Method | Path | Description |
|---|---|---|
| `GET` | `/recommendations` | Read active records in an optional `namespace`; generates records on an empty first read. `refresh=true` schedules a background refresh. |
| `GET` | `/recommendations/active` | Read active records; `refresh=true` refreshes before returning. Supports `namespace`, `source`, `risk_level`, and `capability` filters. |
| `GET` | `/recommendations/top` | Rank active recommendations by projected savings (`metric=co2\|cost`, default `co2`) or a ranking `profile`; supports `namespace` and `refresh`; `limit` defaults to 5. |
| `GET` | `/recommendations/ignored` | List ignored records, optionally filtered by `namespace`. |
| `GET` | `/recommendations/applied` | List applied records, optionally filtered by `namespace`. |
| `GET` | `/recommendations/history` | List records by required ISO-8601 `start`/`end`, optional `type`, and optional `namespace`. |
| `GET` | `/recommendations/savings` | Read realized savings (`last`, `namespace`). |
| `GET` | `/recommendations/{rec_id}` | Read a recommendation record. |
| `GET` | `/recommendations/{rec_id}/events` | Read its lifecycle audit trail. |
| `PATCH` | `/recommendations/{rec_id}/apply` | Record an applied change and verification baseline. |
| `PATCH` | `/recommendations/{rec_id}/ignore` | Ignore a recommendation. |
| `DELETE` | `/recommendations/{rec_id}/ignore` | Restore an ignored recommendation. |
| `POST` | `/recommendations/{rec_id}/apply-pr` | Preview a manifest patch or enqueue a Git pull-request operation. |
| `GET` | `/recommendations/{rec_id}/apply-pr/eligibility` | Check whether a recommendation can be submitted through the PR bot. |
| `GET` | `/recommendations/{rec_id}/pull-requests` | List pull-request attempts for one recommendation. |
| `GET` | `/automation/pull-requests` | List pending or open pull requests. |
| `GET` | `/automation/status` | Report PR-bot provider and configuration readiness. |
| `GET` | `/automation/operations/{operation_id}` | Read durable PR-operation status and integrity metadata. |

For `POST /recommendations/{rec_id}/apply-pr`, pass `{"dry_run": true}` to receive a manifest diff without changing Git. A normal request is queued and returns `202 Accepted` with an `operation_id`; poll the operation endpoint for its state. A running automation worker is required to execute queued operations; the chart's `production` profile deploys one, while `standalone` does not. `Idempotency-Key` may be supplied to make client retries idempotent. The PR bot currently supports CPU and memory rightsizing recommendations on supported workload manifests.

### Reports

| Method | Path | Description |
|---|---|---|
| `GET` | `/report/summary` | Preview report row count and aggregate totals. |
| `GET` | `/report/years` | List calendar years with reportable data. |
| `GET` | `/report/export` | Stream a CSV or JSON report (`format=csv\|json`, default `csv`). |

Report endpoints accept one of:

- `last`: a duration such as `10min`, `2h`, `7d`, `3w`, `1m` (30 days), or `1y` (365 days); `ytd` means January 1 UTC through now. If omitted, the default window is 24 hours.
- `start` and `end`: both required for a custom ISO date or datetime range.
- Repeatable `years` values for calendar-year reports.
- Optional `namespace` to restrict the report or year list to one namespace.

With `aggregate=true`, report rows can be grouped using `granularity=hourly`, `daily`, `weekly`, `monthly`, or `yearly`, and `group_by=pod` (default) or `namespace`.

### Repository bindings

Repository bindings can be managed through this API, but the current PR bot resolves a workload's Git source from its Kubernetes annotations rather than from these bindings.

| Method | Path | Description |
|---|---|---|
| `GET` | `/repository-bindings` | List repository bindings; optional `cluster` filter. |
| `GET` | `/repository-bindings/{namespace}/{workload_kind}/{workload_name}` | Read a binding; optional `cluster` filter. |
| `POST` | `/repository-bindings` | Create or update a repository binding. |

### Prometheus scrape endpoint

GreenKube exposes Prometheus metrics at `GET /prometheus/metrics`. The endpoint is authenticated under the same API-key configuration as protected API routes. For the complete metric inventory and scrape setup, see [Prometheus & Grafana](prometheus-grafana.md).

## Examples

```bash
# Health check
curl http://localhost:8000/api/v1/health

# Per-pod metrics for the last 24 hours
curl "http://localhost:8000/api/v1/metrics?last=24h"

# Time series at daily granularity
curl "http://localhost:8000/api/v1/metrics/timeseries?last=7d&granularity=day"

# Namespace-aggregated monthly report for a custom date range
curl "http://localhost:8000/api/v1/report/export?start=2025-01-01&end=2025-12-31&aggregate=true&granularity=monthly&group_by=namespace"

# Download an unaggregated JSON report
curl -OJ "http://localhost:8000/api/v1/report/export?format=json&last=30d&namespace=production"

# Preview the GitOps patch without touching Git
curl -X POST "http://localhost:8000/api/v1/recommendations/42/apply-pr" \
  -H "Content-Type: application/json" \
  -d '{"dry_run": true}'
```
