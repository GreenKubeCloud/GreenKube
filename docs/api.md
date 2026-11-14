# API Reference

The GreenKube REST API is available under `/api/v1`. Interactive documentation and the OpenAPI schema are available at `/api/v1/docs` and `/api/v1/openapi.json`.

## Authentication

See [Authentication](authentication.md) for the full model. In short:

- **Machines** send a scoped API key as `Authorization: Bearer gk_<id>_<secret>`. Administrators create keys in
  **Settings > API keys** or with `POST /api/v1/auth/api-keys`; the key is shown once. Scopes: `read`, `write`,
  `approve`, `plans:deliver`, `signals:write`, `metrics:read`, `admin`. A request outside its key's scopes answers `403` with code
  `INSUFFICIENT_SCOPE`.
- **The dashboard** signs in with `POST /api/v1/auth/login` and then uses an `HttpOnly` session cookie. Mutations
  authenticated by that cookie need a same-origin `Origin` and the `X-CSRF-Token` header returned by
  `GET /api/v1/auth/me`.
- **The bootstrap key** `GREENKUBE_API_KEY` (generated once by the chart in the Secret `<release>`) is still accepted
  as an admin Bearer key for one more minor release (deprecated):

  ```bash
  kubectl -n <namespace> get secret <release-secret> \
    -o jsonpath='{.data.GREENKUBE_API_KEY}' | base64 -d
  ```

Unauthenticated requests answer `401`. A production instance that cannot authenticate anybody answers `503`; a
development instance without any account and without a bootstrap key stays open.

### Authentication endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/auth/login` | Sign in with `{username, password, remember}`; sets the session cookie. Rate-limited (`429` with `Retry-After`). |
| `POST` | `/auth/logout` | Revoke the current session (`204`). |
| `GET` | `/auth/me` | Current principal, scopes, `must_change_password` and, for sessions, the CSRF token. |
| `POST` | `/auth/password` | Change the password; revokes every session and sets a new cookie. |
| `GET` | `/auth/sessions` | Active sessions of the signed-in user. |
| `DELETE` | `/auth/sessions/{id}` | Sign out one session. |
| `GET` | `/auth/api-keys` | List API keys (`admin`); secrets are never returned. |
| `POST` | `/auth/api-keys` | Create a key `{name, scopes, expires_in_days?}` (`admin`); the response holds the key once. |
| `DELETE` | `/auth/api-keys/{id}` | Revoke a key (`admin`). |

## Endpoints

All paths below are relative to `/api/v1`.

### Health and configuration

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Readiness and application version. Returns `503` with `status: migrating` until the controller has applied every database migration (cached once current), `database_unavailable` when the schema cannot be checked, and `degraded` while a supervised in-process loop is down or stale. |
| `GET` | `/setup/status` | Guided setup checklist: `complete`, `checks[]` (`id`, `category`, `title`, `status` ok/action_needed/blocked/not_applicable/pending, `reason`, `fix` {`text`, `snippet`, `ui_action`}, `docs_url`) and `capabilities` (`discovery`, `delivery`, `sync_tracking`, `measurement` with `state` and `reason`). |
| `GET` | `/version` | Application version. |
| `GET` | `/config` | Current runtime configuration. |
| `GET` | `/health/services` | Health checks for configured data sources (`?force=true` bypasses the cache). |
| `GET` | `/health/services/{service_name}` | Health check for one data source. |
| `POST` | `/config/services` | Removed: answers `410 Gone`. Data sources are configuration as code; see [Configuration](configuration.md#data-sources-are-configuration-as-code). |

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

Recommendation GET endpoints never run analyzers and never write. Scans run in the leader controller every `RECOMMENDATION_SCAN_INTERVAL`, or on demand through `POST /optimization/runs`. The `refresh=true` query parameter on `/recommendations`, `/recommendations/active` and `/recommendations/top` is deprecated: it returns the current data with a run id in the `X-GreenKube-Run-Id` response header (exposed to CORS clients). A run covering the scope that is queued, running or finished within the last 5 minutes is reused (a failed run only for 1 minute, so a later refresh retries); otherwise a run is queued. Poll `GET /optimization/runs/{run_id}` and reload when the run is terminal. Runs are kept for 30 days.

| Method | Path | Description |
|---|---|---|
| `GET` | `/recommendations` | Read persisted active records (and `pr_open` ones, whose pull request is in flight) in an optional `namespace`. Never runs the optimizer. |
| `GET` | `/recommendations/active` | Read persisted active and `pr_open` records with their live expected value (`value`) and delivery `eligibility`, ordered by expected value (`ranking_score`, unscored last), then value, projected cost savings and identity. Supports `namespace`, `source`, `risk_level`, `capability`, `profile` and `carbon_price`. |
| `GET` | `/recommendations/top` | Rank active recommendations by projected savings (`metric=co2\|cost`, default `co2`) or by expected value (`metric=value`, a `profile` or a `carbon_price`); supports `namespace`; `limit` defaults to 5. |
| `GET` | `/sources/track-record` | Measured realization rate per source family and capability, as Beta(α, β) entries. |
| `GET` | `/sources` | Every recommendation source ([Source SDK](sources.md)): `id`, `display_name`, `tier`, `origin` (`builtin`, `plugin`, `unregistered`), `capabilities`, `legacy_source`, `enabled` (`true`, `false`, `auto`), `status` (`healthy`, `degraded`, `failing`, `unavailable`, `disabled`, `misconfigured`, `pending`, `unregistered`) with `status_reason`, `health`, `last_run`, `last_success_at`, `consecutive_failures`, signal and recommendation counts of the last run, and `limits`. `problems` lists plugins that failed to load and invalid `SOURCES_CONFIG` entries. `503` when the run history cannot be read. |
| `GET` | `/sources/{source_id}/runs` | A source's runs, newest first (`limit` 1–500, default 20): status, health, counts, consecutive failures, `error_code` and `diagnostics`. `404` for an unknown source. |
| `GET` | `/sources/{source_id}/signals` | A source's v2 signals, most recently seen first (`limit` 1–500, default 100, optional `target_key`), with `first_seen_at`, `last_seen_at`, `last_run_id` and the `recommendation_fingerprint` they support. Kept 30 days after the last sighting. `404` for an unknown source. |
| `POST` | `/signals` | Push a batch of signals (scope `signals:write`; schema [`signal-batch.v1alpha1`](../schemas/signal-batch.v1alpha1.json)): `source_id` `push:<client>`, `complete` (true replaces the client's earlier signals) and up to 5,000 `signals`. Idempotent on `signal_id`. Answers `batch_id`, `accepted`, `signal_ids`, `withdrawn` and `diagnostics`; `422` lists schema errors as JSON pointers; `403 push_client_mismatch` when an API key pushes for another client than `push:<key name>`. Pushed signals are used by the next scans through the `push` source and become recommendations only through consensus. See [Push API](sources/push.md). |
| `POST` | `/sources/krr/reports` | Push a Robusta KRR JSON report (`krr simple -f json`; scope `signals:write`; `?complete=false` for a partial report; `?cluster=<context>` keeps one kubeconfig context of a multi-cluster report). Answers like `POST /signals`; `422` when the body is not a KRR report, or when it covers several clusters without `cluster` (`multiple_clusters`) or lacks the given one (`cluster_not_in_report`). See [KRR](sources/krr.md). |
| `POST` | `/sources/polaris/reports` | Push a Fairwinds Polaris audit (`polaris audit --format=json`; scope `signals:write`; `?complete=false` for a partial audit). Answers like `POST /signals`; `422` when the body is not a Polaris audit (`polaris_report`). Findings are hygiene advice; missing requests get a request computed from usage. See [Polaris](sources/polaris.md). |
| `POST` | `/optimization/runs` | Queue a recommendation scan for the controller (optional body `{"namespace": "..."}`). Returns `202 {"run_id", "status": "queued"}` and a `Location` header; while a run is queued for the same scope (or a cluster-wide run is queued) its id is returned instead of a duplicate. |
| `GET` | `/optimization/runs` | List recent runs, newest first (`limit` 1–100, default 20), with per-source health and new/changed/resolved counts. |
| `GET` | `/optimization/runs/{run_id}` | Read one run: `run_id`, `status` (`queued`, `running`, `succeeded`, `failed`), `trigger`, `requested_at`, `started_at`, `completed_at`, `recommendation_count`, `sources`, diff counts, `diff`, `error`. `404` when unknown. |
| `GET` | `/recommendations/ignored` | List ignored records, optionally filtered by `namespace`. |
| `GET` | `/recommendations/applied` | List applied records, optionally filtered by `namespace`. |
| `GET` | `/recommendations/history` | List records by required ISO-8601 `start`/`end`, optional `type`, and optional `namespace`. |
| `GET` | `/recommendations/savings` | Read `projected_annual`, `run_rate_annual` and `realized_to_date` savings blocks (`last`, `namespace`); `503` when the ledger is unavailable. |
| `GET` | `/recommendations/{rec_id}` | Read a recommendation record. |
| `GET` | `/recommendations/{rec_id}/events` | Read its lifecycle audit trail. |
| `PATCH` | `/recommendations/{rec_id}/apply` | Record an applied change and verification baseline. Only `active` or `pr_open` records can be applied; any other status returns `409`. Optional user-declared savings are validated (`422`) and accrued as `user_declared`; the projection stays server-side (`503` if the declaration cannot be stored after the apply). |
| `GET` | `/recommendations/{rec_id}/manual` | Manual mode (H9): the `kubectl` steps (`patch` for a typed change, `delete` for an orphaned LoadBalancer or volume, `advisory` otherwise), the patch document, a readable diff, notes and `can_mark_applied`. Never touches the cluster. |
| `POST` | `/recommendations/{rec_id}/manual/applied` | "Mark as applied": records an `active` recommendation as applied by hand with a change id, so it is measured like a merged pull request (`application_method: manual`). `409` for any other status, while a plan is approved or being delivered, or while a resource to delete still exists; `503` when the resource or the plan state cannot be read; `404` for an unknown id. |
| `POST` | `/recommendations/{rec_id}/remeasure` | Start a new fixed-horizon measurement of an applied change whose last measurement was `not_realized` (`verification_status=failed`) or `inconclusive`. Optional body `{"reason": "..."}`. The other members of its attribution group (CPU and memory of one change) in the same state are re-measured with it over the same window. Returns `202` with the record (`verifying`); `409` for any other state (including a sibling already re-measured with its group), `404` when unknown. A recommendation of a recorded change that was `realized` (e.g. one that joined it late) is not measured again: it receives the existing decision with the reason `re-measurement not started` (see [savings measurement](methodology/savings-measurement.md#8-one-measurement-per-change-c5)). |
| `POST` | `/recommendations/{rec_id}/rollback-review/dismiss` | Dismiss a false-positive `rollback_review` with a required, non-blank `{"reason": "..."}`: the record, and the other members of its attribution group still in review, return to `applied` and are measured again. `422` for a missing or blank reason, `409` outside `rollback_review`, `404` when unknown. |
| `PATCH` | `/recommendations/{rec_id}/ignore` | Ignore an `active`, `stale` or `expired` recommendation. `409` for an open PR or an applied change. |
| `DELETE` | `/recommendations/{rec_id}/ignore` | Restore an ignored recommendation. `409` when it is not `ignored`. |
| `GET` | `/recommendations/{rec_id}/pull-requests` | List pull-request attempts for one recommendation. |
| `GET` | `/automation/pull-requests` | List pending or open pull requests. |
| `GET` | `/automation/status` | Report secret-free automation readiness: worker heartbeat, queue depth, plan backlog, discovery freshness, renderer configuration, and provider capability/preflight state. `mode` is `controller`, or `paused` with `automation.policy.pauseDelivery`. |
| `GET` | `/automation/repositories/suggestions` | Discovered repositories with their inferred provider (`provider_type`, `api_base_url`, `inferred_from`: `host`, `probe`, `configured` or `unknown`) and `allowlisted`. |
| `POST` | `/automation/repositories/allowlist` | `admin` scope. Allow discovered repositories (`{"repo_urls": [...]}`); applied by the controller within a minute. Returns `added`, `allowlisted` and the equivalent `values_snippet`. Unknown repositories are rejected with `422`. |
| `POST` | `/automation/plans` | Create a render-validated preview from persisted recommendation IDs and the server-resolved GitOps binding. With `automation.autoApprove`, an eligible preview is approved in the same request (`auto_approved`, else `auto_approve_blockers`). |
| `GET` | `/automation/plans?status=approved&delivery=ci` | Plans by status (default `approved`) and delivery mode (`direct`, `patch`, `ci`), with the plan branch, base branch and revision, PR title and body, and the patch and delivery URLs. A CI job polls it. |
| `GET` | `/automation/plans/{plan_id}/patch` | The approved plan as a `git format-patch` (`text/x-patch`), with `X-GreenKube-Branch`, `X-GreenKube-Base-Branch` and `X-GreenKube-Base-Revision`. `409 plan_not_approved` before approval, `409 patch_unavailable` when the stored diff does not reproduce the original blobs. |
| `POST` | `/automation/plans/{plan_id}/delivery` | `plans:deliver` scope. Report the PR opened outside GreenKube for a plan approved in `patch` or `ci` mode: `{"pr_url": "...", "head_sha": "..."}` (`head_sha` required in `ci` mode). Verified through the provider before the plan moves to `pr_open`; `201` when recorded, `200` with `created: false` for the same PR again, `409 delivery_not_verified`, `delivery_conflict` or `delivery_mode_mismatch`, `503 delivery_not_recorded` when recording failed midway (the plan is approved again; retry). See [Delivery modes](automation.md#delivery-modes). |
| `GET` | `/automation/plans/{plan_id}` | Read a secret-safe plan preview, including source and rendered diffs. |
| `POST` | `/automation/plans/{plan_id}/rollback-preview` | Create a validated inverse preview from a synced plan whose linked recommendations remain in `rollback_review`. |
| `POST` | `/automation/plans/{plan_id}/approve` | Approve the exact `sha256:` digest returned by preview and enqueue the plan. |
| `POST` | `/automation/plans/{plan_id}/cancel` | Mark a draft, validated, or approved plan stale before application. |
| `POST` | `/automation/plans/{plan_id}/abandon` | `approve` scope (operator or admin). Fail a plan stuck in `applying`: cancels its operation, closes its pending pull-request records and releases its recommendations; audited with the caller and the optional `{"reason": "..."}` (at most 500 characters). `409 plan_stale` for another status, while a worker holds a live lease, or when a pull request was already recorded. See [A plan stuck in `applying`](automation.md#a-plan-stuck-in-applying). |
| `DELETE` | `/automation/plans/{plan_id}` | Cancel an unapproved preview: a draft or validated plan becomes `superseded` (`204`). Unknown plan: `404`. Any other status: `409`. |

Recommendation records include an `impact` object: the projection computed by the [Impact Model](methodology/impact-model.md) (model version, annual cost and CO2e breakdown, capacity released, realization factor, `value_annual`, confidence, and the unit prices and their source). It is `null` for records written before the Impact Model or for types without a projection. `potential_savings_cost` and `potential_savings_co2e_grams` are its `expected_billable_cost_annual` and `co2e_expected_annual_g`; both are projections, never measured values.

For `POST /automation/plans`, send recommendation IDs; when the server reports `needs_chart_mapping`, the one-time Argo Helm flow may also send `values_mapping` with `file_path`, `container`, and a string-key `path` ending in `resources`. The error's `mapping_options` contains only Git-managed files and containers resolved by the server. Repository URLs, paths outside those choices, providers, source bindings, source indices, and credentials are never accepted from the UI. The API re-resolves the binding and render-validates the mapping against the immutable repository snapshot; it is recorded only in that plan. A successful preview includes a digest, changed files, source diff, rendered diff, validation result, and expiry. Approval must include that exact digest; a stale source or digest returns `409` and a successful approval returns the durable `operation_id` and `operation_status`. A new preview for the same workload supersedes its existing draft or validated plans instead of returning `409`. A recommendation's read-only `eligibility` field never persists or supersedes plans. Preview `warnings` contain the renderer's warnings (at most 20, each bounded, with URL credentials, query strings and secret assignments redacted); `warnings_truncated` reports whether more exist. A plan whose rendered file contents were pruned by retention still returns its file list, base revision and validation from its stored summary. The plan API supports validated raw YAML/JSON manifests, Kustomize overlays, auto-inferred Argo CD local-chart values mappings (with a one-time mapping flow and the Application annotation as an explicit override), and a fail-closed Flux HelmRelease subset. Flux supports same-snapshot GitRepository charts, `chartRef` to a Git-backed `HelmChart`, safe `chart.spec.valuesFiles`, and Git-inventoried same-repository ConfigMap/Secret `valuesFrom` inputs; the plan continues to update only the HelmRelease's managed post-renderer. Direct remote chart artifacts, cross-repository/revision values, runtime-only or ambiguous references, `valuesFrom.targetPath`, unsupported dependencies, and inline Argo overrides fail with typed reasons.

Example one-time mapping request body: `{"recommendation_ids":[42],"values_mapping":{"file_path":"apps/payments/values.yaml","container":"api","path":["app","resources"]}}`.

Resource recommendations for multiple containers in one workload are composed into one action and plan with one explicit operation per container. If the source renderer cannot safely apply the complete set, the request fails closed rather than silently dropping a container.

Rollback preview takes only the original plan ID. GreenKube snapshots the current immutable ref, retrieves updated files at the original plan's base revision through the authorized provider resolver, and rejects changed paths or any rendered effect outside the exact target. Approval uses the same exact-digest endpoint and worker queue as other plans. Recommendations remain in `rollback_review` through merge and controller sync; they become `reverted` (and their actions `reverted`) only after the rollback plan is `synced`, a post-baseline workload generation is observed, and live per-container requests match the original pre-apply baseline. RAW rollback is built in; Kustomize requires an explicitly configured exact-target rollback adapter; Argo CD `HELM_VALUES` rollback supports only the explicitly mapped local-chart subset. Flux `HELM_RELEASE` rollback restores the original Git-owned HelmRelease file and uses the same local same-revision chart, snapshot-resolvable `valuesFiles`/`valuesFrom`, and GreenKube-managed post-renderer subset as forward planning. It fails closed on stale refs, changed forward files or render output, missing baselines, unrelated rendered changes, remote/cross-revision charts, chart dependencies, runtime-only or cross-repository `valuesFrom`, unsupported `targetPath` merging, unmanaged post-renderers, or unsupported operations.

The compatibility routes `POST /recommendations/{rec_id}/apply-pr`, `GET /recommendations/{rec_id}/apply-pr/eligibility` and `GET /automation/operations/{operation_id}` were removed after their deprecation release (roadmap F5): they answer `404`. Use `POST /automation/plans` to preview, `POST /automation/plans/{plan_id}/approve` to open the pull request, `GET /automation/plans/{plan_id}` (`operation_id`, `operation_status`) to follow it, and each recommendation's `eligibility` field to know whether a pull request can be opened. Git provider credentials are configured through `automation.providers[].secretName` / `secretKey` Secret references and repository allowlists in Helm values; credential bytes are never put in the ConfigMap. Recommendation event history records plan creation, plan/action transitions, and authenticated actor identifiers.

### Changes and measurements (`/api/v2`)

These routes live under `/api/v2` (not `/api/v1`) and use the same authentication: a session, the bootstrap key, or an API key with the `read` scope ([Authentication](authentication.md)). A change is one workload revision applied by apply detection; see [Savings measurement](methodology/savings-measurement.md).

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v2/changes` | Changes, newest first: target, recommendation IDs, plan and PR links (`pr_url`, `merge_sha`, `gitops_revision`), generations and spec digests before/after, rollout start and completion, `state` (`applied`, `measuring`, `realized`, `not_realized`, `inconclusive`, `contaminated`, `regressed`, `closed`), `post_window`. Filters: `namespace`, `state` (repeatable), `plan_id` (the changes of one GitOps plan, used by the `/ledger?change=<plan_id>` link of a pull request); `limit` 1–200 (default 50); `cursor` from the previous page's `next_cursor`. |
| `GET` | `/api/v2/changes/{change_id}` | One change with its `baseline` snapshot metadata (window, covered hours, load drivers, price card, `content_sha256` and `content_verified`) and its lifecycle `events`. `404` when unknown. |
| `GET` | `/api/v2/changes/{change_id}/measurements` | Decided measurements, oldest first: windows, `load_driver`, `counterfactual_params`, signed `estimates` per quantity (`rate_per_hour`, 90% `ci_low`/`ci_high`, `annualized` and its interval, `coverage`, `claimed`, `projection_accuracy`), `confounders`, `confidence`, `health`, `allocations`, `outcome`, `reasons`, `evidence_sha256`; and `recommendations`, the per-recommendation rows of that measurement. `404` when unknown. |

Storage failures return `503`. Only `allocated_cost` is claimed for request changes; the other quantities are reported. Measured values are never added to projected ones.

### Savings ledger (`/api/v2`)

The append-only, hash-chained savings ledger v2 ([Savings ledger v2](methodology/savings-measurement.md#12-savings-ledger-v2-c6)). Same authentication as the other `/api/v2` routes. Reads only; the controller writes the ledger. Every entry is one signed amount of one `quantity` (`cost` in the pricing currency, `co2e_g`) under one `method` (`projected`, `measured_allocated`, `realized_billed`, `user_declared`): methods are never summed together.

Filters shared by the three routes: `namespace`, `method`, `quantity`, `entry_kind` (`accrual`, `reversal`, `restatement`), `book` (`accrued`, `restatement`), `reason` (`accrual`, `drift`, `supersession`, `restatement`), `recommendation_id`, `change_id`, `measurement_id`, and a window: `last` (`24h`, `7d`, `30d`, `ytd`...) or both `start` and `end` (ISO-8601; one bound alone returns `400`, as the savings report would ignore it). An entry is selected when its period overlaps the window. Unknown filter values return `422`, a bad window or cursor `400`. CSV string cells are guarded against formula injection (a leading `=`, `+`, `-` or `@` gets a `'` prefix) and `pr_url` is the web URL of the pull request, as in the JSON entries.

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v2/ledger/entries` | `{"cluster_uid", "cluster_name", "items": [...], "next_cursor"}`, newest first (highest chain position). Each item carries every column: `seq`, `entry_id`, `entry_key`, `group_key`, `entry_kind`, `book`, `reason`, `method`, `quantity`, `value`, `unit`, `currency`, `period_start`, `period_end`, `slot_start`, `recommendation_id`, `recommendation_ids`, `namespace`, `recommendation_type`, `change_id`, `measurement_id`, `pr_url`, `method_version`, `confidence`, `reverses_entry_id`, `restates_entry_id`, `source_table`, `source_id`, `created_at`, `created_by`, `prev_hash`, `hash`. `limit` 1–500 (default 100); `cursor` from the previous page's `next_cursor`. Empty (`cluster_uid: null`) until the controller resolved the cluster identity. |
| `GET` | `/api/v2/ledger/summary` | `totals`: one row per method and quantity with `accrued` (equals `GET /api/v1/recommendations/savings`: `measured_allocated` ↔ `realized_to_date`, `projected` ↔ `projected_to_date`, `user_declared` ↔ `user_declared_to_date`), `restatement` and `restated` (both books: projections replaced by measured amounts after a measurement). With a window, entries crossing a boundary count for their overlapping share, as in the savings report. Also `entries_by_kind`, `cluster_uid` and its `cluster_uid_source` (`kube-system` or `cluster_name`), and `chain`: `head_seq`, `head_hash`, `verified_at`, `verified_entries`, `verified_head_seq`, `ok`, `failure`. Filters: `namespace` and the window. |
| `GET` | `/api/v2/ledger/export` | Streams `text/csv`, oldest entry first. `format=csv` (default): one row per entry with every column, plus `window_share` and `window_value` (the prorated value inside the window). `format=focus`: one FOCUS-aligned row per accrual group (the cost and CO₂e entries of one period and method): `BillingAccountId` (cluster UID), `SubAccountId` (namespace), `ChargePeriodStart`/`End`, `BillingPeriodStart`/`End` (the calendar month), `ChargeCategory` (`Credit`, or `Adjustment` with `ChargeClass` `Correction` for reversals and restatements), `EffectiveCost` (−saving, avoided spend), `BilledCost` (0 except `realized_billed`), `BillingCurrency`, `ResourceId` (`recommendation/<id>`), `Tags`, and the extension columns `x-greenkube-method`, `-book`, `-reason`, `-entry-kind`, `-method-version`, `-saving`, `-co2e-g`, `-window-share`, `-measurement-id`, `-change-id`, `-recommendation-ids`, `-pr-url`, `-reverses-entry-id`, `-restates-entry-id`, `-entry-id`, `-seq`, `-hash`. |

Reconciliation: over the same window, the sum of `window_value` (CSV) or `x-greenkube-saving` (FOCUS) of the `accrued` rows of one method and quantity equals the matching block of `GET /api/v1/recommendations/savings`.

### Recommendations v2 (`/api/v2`)

The v2 shape of the same persisted recommendations ([Consensus across sources](recommendation.md#consensus-across-sources), [Identity and revisions](recommendation.md#identity-and-revisions)). Same authentication as the other `/api/v2` routes. Reads only.

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v2/recommendations` | `{"items": [...], "total": n}`, ordered by expected value. Each item: `id`, `fingerprint` (the cluster-qualified v4 identity), `legacy_fingerprint` (v1), `revision`, `queued_revision` (newer advice waiting behind an open plan, else `null`), `target` (`cluster_uid`, `api_version`, `kind`, `namespace`, `name`, `container`), `capability` (`container_requests`, `horizontal_autoscaling`, `schedule`, `cleanup`, `node_pool`, `placement`, `hygiene`), `kind` (the v1 type), `proposal` (typed: `container_resources`, `hpa`, `schedule`, `deletion`, `advisory`...), `consensus` (`policy`, `value`, `chosen_from`, `agreement`, `contested`, `reasons`, `spread`, `independent_votes`, `source_ids`), `signals` (one vote per supporting signal: `source_id`, `family`, `signal_id`, `value`, `counted`, `supports_chosen`), `impact`, `value`, `risk`, `confidence`, `eligibility`, `status`, `lifecycle`, `source`, `sources` (reconciliation units), `source_ids` (every source id behind the recommendation, the chosen one first: `goldilocks`, `krr`, `push:<client>`...), `description`, `reason`, `priority`, `evidence` and `legacy_fields` (v1 fields the target and proposal cannot express). Filters: `namespace`; `status` (repeatable or comma-separated, default `active` and `pr_open`, `422` when unknown); `capability` (v2 or v1 name); `source` (supporting source or source id, such as `goldilocks`); `profile` and `carbon_price` as in v1; `limit` 1–500 (default 100). |
| `GET` | `/api/v2/recommendations/{id}` | One recommendation in the v2 shape plus `revisions`, newest first: each with `revision`, `state` (`superseded` or `queued`), `proposal`, `consensus`, a v1 `snapshot` and timestamps. `404` when unknown. |

The v1 routes keep their flat fields (`recommended_cpu_request_millicores`, `cron_schedule`...): they are the v2 proposal in flat form, and `flat_fields(to_v2(record))` reproduces them exactly. v1 records also gain the additive fields `fingerprint_v4`, `revision`, `target`, `proposal` and `consensus`. `source` may be the id of a signal-only source (`krr`, `push:<client>`) when its proposal carried the consensus.

### Provider webhooks

| Method | Path | Description |
|---|---|---|
| `POST` | `/webhooks/{provider_id}` | Verify, deduplicate, persist, and reconcile a signed GitHub/Gitea/GitLab pull-request event for an enabled configured provider. |

When `GREENKUBE_API_KEY` is configured, only this `POST` route with a single provider ID segment is exempt from bearer authentication. The provider-native signature/token check is still mandatory. Close events are re-fetched through the allowlisted provider adapter before plan and action state can change; delivery status and safe correlation fields are returned without echoing request payloads or credentials. Merged actions enter `sync_pending`; the controller must report the landed commit, or a later commit that contains it (checked through the provider's ancestry API) and still carries the planned values in the planned files, with healthy readiness before the plan moves to `synced`. A PR closed without merging closes its plan even when GitHub still reports a test-merge SHA. A sync timeout moves the plan to `sync_failed`. Apply detection requires a synced plan and exact per-container request match. Configure the read-only API Secret projection described in [Configuration](configuration.md#signed-git-provider-webhooks). Provider schemes do not include signed timestamps; polling is the recovery path for missed or unreconciled events. A verified event for a pull request that GreenKube does not track returns `202` with status `ignored`: it is terminal, never retried, and removed by retention.

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

GitOps bindings are discovered from Argo CD and Flux controller state. Discovery is read-only except for the operator-approved, expiring resolution of an ambiguous or unresolved binding. All GitOps endpoints use the standard API authentication (a session cookie or a scoped API key, see [Authentication](authentication.md)). See [GitOps automation](automation.md) for supported source layouts and safety rules.

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
