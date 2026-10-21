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

### GitOps discovery

| Method | Path | Description |
|---|---|---|
| `GET` | `/gitops/status` | Check controller/source CRD availability with bounded probes and return freshness-aware coverage. |
| `POST` | `/gitops/refresh` | Run and persist one explicit discovery refresh. |
| `GET` | `/gitops/bindings` | Read one cursor-paginated binding page (`limit` defaults to 50, maximum 100; optional repeated `status` filters and opaque `cursor`). |
| `GET` | `/gitops/bindings/{namespace}/{workload_kind}/{workload_name}` | Read one binding and bounded candidate provenance. |
| `POST` | `/gitops/bindings/{namespace}/{workload_kind}/{workload_name}/resolve` | Select one current candidate using its server-issued generation token. |
| `GET` | `/gitops/coverage` | Return freshness-aware binding counts without loading the binding table into the API process. |

When a binding page has more items, `has_more` is true and `next_cursor` can be sent as the next request's `cursor`. A resolved or ambiguous binding/candidate whose observation is at least 30 minutes old or whose expiry has passed is reported as `stale` on read; `blocked` remains a distinct status. `POST /gitops/refresh` remains the explicit discovery trigger.

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
| `POST` | `/recommendations/{rec_id}/apply-pr` | Compatibility endpoint that previews or queues a validated GitOps plan. |
| `GET` | `/recommendations/{rec_id}/apply-pr/eligibility` | Check eligibility with a read-only plan preview. |
| `GET` | `/recommendations/{rec_id}/pull-requests` | List pull-request attempts for one recommendation. |
| `GET` | `/automation/pull-requests` | List pending or open pull requests. |
| `GET` | `/automation/status` | Report secret-free automation readiness: worker heartbeat, queue depth, plan backlog, discovery freshness, renderer configuration, and provider capability/preflight state. |
| `GET` | `/automation/operations/{operation_id}` | Read durable plan-operation state through the compatibility route. |
| `POST` | `/automation/plans` | Create a render-validated preview from persisted recommendation IDs and the server-resolved GitOps binding. |
| `GET` | `/automation/plans/{plan_id}` | Read a secret-safe plan preview, including source and rendered diffs. |
| `POST` | `/automation/plans/{plan_id}/rollback-preview` | Create a validated inverse preview from a synced plan whose linked recommendations remain in `rollback_review`. |
| `POST` | `/automation/plans/{plan_id}/approve` | Approve the exact `sha256:` digest returned by preview and enqueue the plan. |
| `POST` | `/automation/plans/{plan_id}/cancel` | Mark a draft, validated, or approved plan stale before application. |

For `POST /automation/plans`, send recommendation IDs; when the server reports `needs_chart_mapping`, the one-time Argo Helm flow may also send `values_mapping` with `file_path`, `container`, and a string-key `path` ending in `resources`. The error's `mapping_options` contains only Git-managed files and containers resolved by the server. Repository URLs, paths outside those choices, providers, source bindings, source indices, and credentials are never accepted from the UI. The API re-resolves the binding and render-validates the mapping against the immutable repository snapshot; it is recorded only in that plan. A successful preview includes a digest, changed files, source diff, rendered diff, validation result, and expiry. Approval must include that exact digest; a stale source or digest returns `409` and a successful approval returns the durable `operation_id` and `operation_status`. A read-only eligibility request or legacy `dry_run` does not persist or supersede plans. The plan API supports validated raw YAML/JSON manifests, Kustomize overlays, auto-inferred Argo CD local-chart values mappings (with a one-time mapping flow and the Application annotation as an explicit override), and a fail-closed Flux HelmRelease subset. Flux supports same-snapshot GitRepository charts, `chartRef` to a Git-backed `HelmChart`, safe `chart.spec.valuesFiles`, and Git-inventoried same-repository ConfigMap/Secret `valuesFrom` inputs; the plan continues to update only the HelmRelease's managed post-renderer. Direct remote chart artifacts, cross-repository/revision values, runtime-only or ambiguous references, `valuesFrom.targetPath`, unsupported dependencies, and inline Argo overrides fail with typed reasons.

Example one-time mapping request body: `{"recommendation_ids":[42],"values_mapping":{"file_path":"apps/payments/values.yaml","container":"api","path":["app","resources"]}}`.

Resource recommendations for multiple containers in one workload are composed into one action and plan with one explicit operation per container. If the source renderer cannot safely apply the complete set, the request fails closed rather than silently dropping a container.

Rollback preview takes only the original plan ID. GreenKube snapshots the current immutable ref, retrieves updated files at the original plan's base revision through the authorized provider resolver, and rejects changed paths or any rendered effect outside the exact target. Approval uses the same exact-digest endpoint and worker queue as other plans. Recommendations remain in `rollback_review` through merge and controller sync; they become `reverted` (and their actions `reverted`) only after the rollback plan is `synced`, a post-baseline workload generation is observed, and live per-container requests match the original pre-apply baseline. RAW rollback is built in; Kustomize requires an explicitly configured exact-target rollback adapter; Argo CD `HELM_VALUES` rollback supports only the explicitly mapped local-chart subset. Flux `HELM_RELEASE` rollback restores the original Git-owned HelmRelease file and uses the same local same-revision chart, snapshot-resolvable `valuesFiles`/`valuesFrom`, and GreenKube-managed post-renderer subset as forward planning. It fails closed on stale refs, changed forward files or render output, missing baselines, unrelated rendered changes, remote/cross-revision charts, chart dependencies, runtime-only or cross-repository `valuesFrom`, unsupported `targetPath` merging, unmanaged post-renderers, or unsupported operations.

`POST /recommendations/{rec_id}/apply-pr` remains as a compatibility endpoint, but it uses the same server-resolved, render-validated plan path; workload annotations and the retired single-provider queue are not used. Pass `{"dry_run": true}` to receive the plan's source diff without changing Git. A normal request approves the exact validated digest, returns `202 Accepted` with the durable plan `operation_id`, and can be polled through `/automation/operations/{operation_id}`. Repeated requests reuse the deterministic plan operation, so they do not open duplicate PRs; closed or failed attempts can be re-planned without deleting their audit history. The standalone Helm profile runs plan processing inline; the production profile deploys a dedicated single-replica plan worker. Git provider credentials are configured through `automation.providers[].tokenSecret` Secret references and repository allowlists in Helm values; credential bytes are never put in the ConfigMap. Recommendation event history records plan creation, plan/action transitions, and authenticated actor identifiers.

### Provider webhooks

| Method | Path | Description |
|---|---|---|
| `POST` | `/webhooks/{provider_id}` | Verify, deduplicate, persist, and reconcile a signed GitHub/Gitea/GitLab pull-request event for an enabled configured provider. |

When `GREENKUBE_API_KEY` is configured, only this `POST` route with a single provider ID segment is exempt from bearer authentication. The provider-native signature/token check is still mandatory. Close events are re-fetched through the allowlisted provider adapter before plan and action state can change; delivery status and safe correlation fields are returned without echoing request payloads or credentials. Merged actions enter `sync_pending`; the controller observes the exact Argo CD/Flux source revision and readiness before moving the plan to `synced`. A sync timeout moves the plan to `sync_failed`. Apply detection requires a synced plan and exact per-container request match. Configure the read-only API Secret projection described in [Configuration](configuration.md#signed-git-provider-webhooks). Provider schemes do not include signed timestamps; polling is the recovery path for missed or unreconciled events.

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

GitOps bindings are discovered from Argo CD and Flux controller state. Discovery is read-only except for the operator-approved, expiring resolution of an ambiguous or unresolved binding. All GitOps endpoints use the existing API-key middleware. See the [GitOps automation specification](specs/gitops-automation.md) for supported source layouts and safety rules.

| Method | Path | Description |
|---|---|---|
| `GET` | `/gitops/status` | Controller CRD availability, discovery freshness, and binding coverage for an optional `cluster`. |
| `GET` | `/gitops/bindings` | Bounded binding list; repeat `status=ambiguous&status=unresolved` for actionable bindings and optionally set `limit` (maximum 100). |
| `GET` | `/gitops/bindings/{namespace}/{workload_kind}/{workload_name}` | Read the resolved binding, candidate sources, and safe provenance; optional `cluster`. |
| `POST` | `/gitops/bindings/{namespace}/{workload_kind}/{workload_name}/resolve` | Approve one server-provided current candidate; optional `cluster`. Writes an expiring manual resolution and a durable audit event. |
| `GET` | `/gitops/coverage` | Count resolved, ambiguous, unresolved, blocked, and stale workload bindings; optional `cluster`. |

The resolution request accepts only the candidate ID, its opaque generation token, and explicit confirmation:

```json
{
  "candidate_id": "argocd-...",
  "generation_token": "sha256:<64 lowercase hexadecimal characters>",
  "confirmed": true
}
```

Repository URLs, provider IDs, paths, and renderer configuration are never accepted from the client. The candidate must still belong to an unresolved or ambiguous binding and its controller/source generation must match the token returned by discovery; otherwise the API returns `409` and the operator must refresh candidates. Manual resolutions expire no later than the binding TTL (30 minutes by default) and are re-evaluated by discovery when controller or source identity changes.

| Method | Path | Description |
|---|---|---|
| `GET` | `/repository-bindings` | Legacy binding view; optional `cluster` filter. |
| `GET` | `/repository-bindings/{namespace}/{workload_kind}/{workload_name}` | Legacy binding lookup; optional `cluster` filter. |
| `POST` | `/repository-bindings` | Legacy manual binding endpoint. |

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

# Create a digest-addressed GitOps plan preview
curl -X POST "http://localhost:8000/api/v1/automation/plans" \
  -H "Authorization: Bearer $GREENKUBE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"recommendation_ids":[42]}'

# Approve the exact digest returned by the preview
curl -X POST "http://localhost:8000/api/v1/automation/plans/<plan_id>/approve" \
  -H "Authorization: Bearer $GREENKUBE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"digest":"sha256:<preview-digest>"}'
```
