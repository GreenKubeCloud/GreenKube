# GitOps automation

GreenKube can turn eligible CPU and memory rightsizing recommendations into reviewed Git pull requests. The workflow is deliberately conservative: GreenKube discovers the workload's Git owner, renders the change in isolation, requires an exact-digest approval, opens one atomic multi-file PR, and only records the recommendation as applied after the GitOps controller has synced the merged revision and the live workload matches the plan.

Automation is disabled by default. Everything below applies only after `automation.enabled=true`.

## How it works

```text
Recommendation evidence
        │
        ▼
Argo CD / Flux discovery ──► workload binding
        │
        ▼
Server-side plan (raw / Kustomize / Helm)
        │
        ▼
Digest-bound approval ──► durable operation queue
        │
        ▼
Git provider commit + pull request
        │
        ▼
Controller sync ──► apply detection ──► verification
```

Components:

| Component | Responsibility |
|---|---|
| Discovery runtime | Reads Argo CD/Flux resources and live workloads, then persists generation-bound workload bindings. |
| Renderer sandbox | Renders Helm/Kustomize plans from a bounded repository snapshot without database or provider credentials. |
| Plan service | Resolves recommendation IDs to server-side actions, validates the source and rendered diff, and stores an expiring preview. |
| Plan worker | Executes approved operations, creates commits and PRs, polls linked PRs, and reconciles terminal states. Runs inline in `standalone` or as a dedicated Deployment in `production`. |
| Controller runtime | Observes the exact Argo CD/Flux revision and readiness after merge, then gates apply detection and verification. |
| Webhook endpoint | Accepts signed GitHub/Gitea/GitLab delivery events. Polling remains the recovery path. |

## Prerequisites

- Kubernetes cluster with GreenKube installed and healthy.
- Argo CD and/or Flux controllers installed if you want discovery-driven plans. GreenKube only plans workloads it can bind to a supported Git source.
- A CNI that enforces NetworkPolicy. The renderer sandbox relies on an egress-deny policy. If your CNI does not enforce NetworkPolicy:
  - keep automation disabled, or
  - set `rendererSandbox.egressFirewall.enabled=true` on clusters that permit `NET_ADMIN` and validate the resulting boundary, or
  - leave rendering disabled and validate plans only through the API without applying them.
- Metrics Server or another source of per-container CPU/memory for evidence, apply detection, and verification.
- A GitHub, GitLab, or Gitea repository containing the workload manifests, plus a token with read/write repository and pull-request permissions.
- A repository allowlist. An empty allowlist denies every repository.

GreenKube needs read access to the GitOps APIs it discovers:

- Argo CD `applications.argoproj.io`, `applicationsets.argoproj.io`
- Flux `kustomizations.kustomize.toolkit.fluxcd.io`, `helmreleases.helm.toolkit.fluxcd.io`
- Flux sources: `gitrepositories`, `ocirepositories`, `helmrepositories`, `buckets` in `source.toolkit.fluxcd.io`

The chart grants the corresponding read-only RBAC by default.

## Configure a provider

Create the token Secret in the Helm release namespace. Do not put the token in `values.yaml`:

```bash
kubectl -n greenkube create secret generic greenkube-git \
  --from-file=github-token=./github-token
```

Configure one or more providers:

```yaml
automation:
  enabled: true
  mode: worker          # default for profile=production; use inline for standalone
  providers:
    - id: github-main
      type: github      # github | gitlab | gitea
      apiBaseUrl: https://api.github.com
      secretName: greenkube-git
      secretKey: github-token
      repositoryAllowlist:
        - "acme/gitops"
        - "acme/platform"
```

Rules:

- Provider `id` values must be unique.
- `repositoryAllowlist` is mandatory and uses segment-aware globs. `acme/*` matches `acme/repo`, not `acme/team/repo`.
- The Secret must be in the release namespace. Only the selected key is projected read-only into API/worker pods.
- Provider credentials are never rendered into a ConfigMap and are never returned by the API.
- For GitLab, `apiBaseUrl` can point at a self-managed instance. For Gitea, point it at the Gitea API root (for example `https://gitea.example.com/api/v1`).
- `pullRequests.draft`, `labels`, `reviewers`, and `teamReviewers` are preflighted against provider capabilities. Unsupported options fail the operation instead of being silently dropped.

### Signed webhooks (optional)

Polling works without webhooks. Webhooks reduce latency for merge/close events:

```yaml
automation:
  webhooks:
    enabled: true
  providers:
    - id: github-main
      type: github
      secretName: greenkube-git
      secretKey: github-token
      repositoryAllowlist:
        - "acme/gitops"
      webhookSecretName: greenkube-webhooks
      webhookSecretKey: github-hmac
```

A single-provider installation may instead use the top-level `automation.webhooks.secretName` / `secretKey` form. Each provider must use a distinct Secret key.

Only `POST /api/v1/webhooks/<provider_id>` is exempt from the GreenKube bearer key. The request still must carry the provider-native signature/token. Provider schemes do not include signed timestamps, so delivery IDs are deduplicated and provider state is re-fetched before lifecycle changes.

## Supported source layouts

Only CPU and memory rightsizing recommendations currently have GitOps action planners. Other recommendation types remain visible but show "Manual change required" and cannot open a PR.

| Source | Supported | Notes |
|---|---|---|
| Argo CD raw YAML | Yes | Uses the Application's resource inventory and a raw manifest planner. |
| Argo CD Kustomize | Yes | Plans the overlay and render-validates the result. |
| Argo CD Helm with Git-managed values files | Yes | Infers a unique values file/container mapping when possible; otherwise offers a server-validated one-time mapping. |
| Argo CD Helm with inline `valuesObject` / `parameters` | No | Fails closed; move values to a Git-managed values file. |
| Argo CD multi-source Helm | Yes | Values must resolve to a Git-managed source; source indices are handled server-side. |
| Flux Kustomization (raw/Kustomize) | Yes | Uses the Flux inventory and exact source revision. |
| Flux HelmRelease with local same-revision Git chart | Yes | Renders `valuesFiles`, `valuesFrom`, and inline values, then applies the managed post-renderer edit. |
| Flux HelmRelease `chartRef` to a Git-backed HelmChart | Yes | The HelmChart must exist in the same immutable snapshot. |
| Flux HelmRelease with remote HelmRepository/OCI chart | No | The chart artifact is not in the snapshot; fails closed. |
| Flux `valuesFrom` that is runtime-only, cross-repository, cross-revision, or uses `targetPath` | No | Fails closed with a typed reason. |
| Manual binding resolution | Yes | An operator can select one server-provided candidate for an ambiguous/unresolved binding. |

When discovery cannot choose a single owner, the binding is `ambiguous`, `unresolved`, or `blocked` and plans fail closed. The **Automation & GitOps** page lists actionable bindings and their safe candidate provenance. Manual resolutions are generation-bound, expire at `automation.bindingTtl` (default 30 minutes), and are written to the audit trail.

## Plan lifecycle

A plan request accepts recommendation IDs only. Repository URLs, file paths, provider IDs, source bindings, and credentials are never accepted from the browser.

1. GreenKube builds a typed, container-targeted action from the recommendation evidence. Recommendations without a safe per-container target remain non-actionable.
2. The server resolves the workload binding, snapshots the immutable provider revision, and renders the before/after state.
3. Raw YAML/JSON, Kustomize, and the supported Helm layouts are validated. Any unrelated rendered change fails the plan.
4. A successful preview contains a `sha256:` digest, changed files, source and rendered diffs, validation state, and an expiry.
5. Approval must present that exact digest. A changed source or stale digest returns `409` and requires a new preview.
6. The durable worker revalidates the source, creates one atomic multi-file commit on a deterministic branch, and opens or adopts a matching PR. Repeated requests reuse the open PR instead of creating duplicates.
7. After merge, linked recommendation actions enter `sync_pending` and the plan moves to `merged`. The controller observes the exact Argo CD/Flux source revision and readiness; a confirmed plan moves to `synced`, while a timeout moves it to `sync_failed`. A plan never jumps directly to `applied`.
8. Apply detection requires a synced plan and an exact per-container request match. Verification then uses the normal post-apply health and savings gates.

Plan statuses: `draft`, `validated`, `approved`, `applying`, `pr_open`, `merged`, `synced`, `applied`, `sync_failed`, `closed`, `stale`, `failed`, `needs_mapping`, `ambiguous`, `rejected`, `superseded`.

Plan operations are durable and idempotent: `queued`, `running`, `retrying`, `succeeded`, `failed`, `cancelled`. Poll an operation through `GET /api/v1/automation/operations/{operation_id}`.

## Approval and policy

Policy is evaluated when a preview is created and revalidated on approval:

```yaml
automation:
  policy:
    maxRiskLevel: medium
    minConfidence: 0.6
    minSamples: 36
    minCoverage: 0.5
    maxOpenPullRequestsPerWorkload: 1
    cooldownMinutes: 60
    changeFreezeLabels: []
    changeFreezeWindows: []
```

Discovery also supplies live workload labels, rollout state, controller suspend/health, and HPA/PDB evidence. A configured freeze label blocks only matching workloads. When rollout, controller-health, or HPA/PDB evidence cannot be collected, the gate fails closed instead of guessing.

## Pull-request options

```yaml
automation:
  pullRequests:
    draft: false
    labels: []
    reviewers: []
    teamReviewers: []
```

- Labels default to none. Configure only labels that already exist in every target repository.
- GitHub supports draft PRs, labels, reviewers, and team reviewers.
- GitLab supports draft MRs, labels, and numeric reviewer IDs; team reviewers are not supported.
- Gitea supports labels and reviewers; draft PRs and team reviewers are not supported.

Provider capabilities are checked before a branch or commit is created. Missing capabilities surface as a typed `provider_capability_missing`-style error.

## Rollback

A synced plan whose recommendations remain in `rollback_review` can produce a validated inverse preview:

- The API re-reads the original plan's base revision through the authorized provider and rejects files that changed after the forward plan.
- The rollback preview uses the same digest approval and worker queue.
- A recommendation becomes `reverted` only after the rollback plan is `synced`, a post-baseline workload generation is observed, and live per-container requests match the original pre-apply baseline.
- RAW rollback is built in. Kustomize rollback requires an explicitly configured exact-target rollback adapter. The supported Argo CD `HELM_VALUES` subset and Flux `HELM_RELEASE` subset restore the same Git-managed inputs as their forward plans.
- Unsupported layouts fail closed rather than producing a partial rollback.

The **Recommendations** page exposes rollback preview from the Pull requests tab when the original plan qualifies.

## Security model

| Risk | Control |
|---|---|
| Unauthorized repository change | Per-provider repository allowlists, server-side binding resolution, digest-bound approval. |
| Credential exposure | Secret references only; read-only projections; no credentials in ConfigMaps, logs, plan responses, or the browser. |
| Path traversal / archive escape | Bounded repository archives, path normalization, rejection of `..`, symlinks, device entries, and oversized files. |
| Renderer compromise | Separate Pod without database/provider Secrets or service-account token; read-only root filesystem; dropped capabilities; bounded CPU/memory/PIDs; renderer NetworkPolicy denies egress. |
| Malicious Helm/Kustomize templates | Pinned binaries, argument arrays, scrubbed environment, timeouts, output limits, and no cluster credentials. |
| Webhook spoofing | Provider-native HMAC/token verification, delivery deduplication, and provider-state re-fetch before lifecycle changes. |
| Duplicate PRs/operations | Deterministic plans, durable operation IDs, provider PR adoption, and database-backed leases. |

The default snapshot limits are 100 MiB compressed, 500 MiB extracted, 20,000 files, and 2 MiB per file.

## Worker modes

| Mode | When | Behaviour |
|---|---|---|
| `inline` | Default for `standalone` when automation is enabled. | The API process starts the worker loop. |
| `worker` | Default for `production` when automation is enabled. | A dedicated single-replica Deployment executes operations. Its authenticated metrics listener uses `automation.workerMetricsPort` (default 8001). |
| `disabled` | Automation off. | No discovery, planning, or worker execution. |

Do not run inline and worker mode against the same database. Database leases prevent duplicate execution, but only one mode should be configured.

## Intervals and operations

| Value | Default | Purpose |
|---|---|---|
| `automation.discoveryInterval` | `5m` | Periodic Argo CD/Flux discovery refresh (`1s`–`1h`). |
| `automation.bindingTtl` | `30m` | Binding and manual-resolution freshness (`1s`–`24h`). |
| `automation.prPollIntervalSeconds` | `120` | PR polling fallback (10–86400). |
| `automation.syncPollIntervalSeconds` | `30` | Controller sync observation interval (10–86400). |
| `automation.syncTimeoutSeconds` | `1800` | Time before an unsynced merged plan becomes `sync_failed` (60–86400). |

A periodic reconciler fails an `applying` plan when its operation is terminal and no PR exists. A daily retention job removes rendered file contents and terminal operation/webhook rows older than 90 days while retaining plan metadata, PR records, and recommendation audit events.

## Observability

- `GET /api/v1/automation/status` reports worker heartbeat age, queue depth, plan backlog, discovery freshness, renderer configuration, and provider preflight/capability state.
- `GET /api/v1/automation/pull-requests` lists safe pending/open PR metadata.
- `GET /prometheus/metrics` exposes control-loop metrics. In production worker mode, a separate Service/ServiceMonitor scrapes the worker's process-local registry.
- Optional `monitoring.prometheusRule.enabled` installs alerts for stuck operations/plans, sync failures, webhook lag, and stale worker heartbeats.

SLOs and example alert rules are documented in [Observability and SLOs](observability-slos.md). The full metric inventory is in [Prometheus and Grafana](prometheus-grafana.md).

## Limitations

- Only CPU and memory rightsizing recommendations have GitOps planners.
- Remote Helm/Kustomize artifacts, Argo CD inline Helm values, Flux `valuesFrom.targetPath`, cross-repository/revision values, chart dependencies, and unmanaged post-renderers fail closed.
- Argo CD Applications that are not stored in Git have no PR target.
- Provider archive downloads must be available at the resolved full commit SHA. GreenKube does not fall back to a mutable branch or per-file reads.
- Gitea does not support draft PRs or team reviewers; GitLab does not support team reviewers. Configure only supported options.
- Minikube does not enforce NetworkPolicy. Do not enable in-cluster rendering there unless you have validated an equivalent boundary.

See the [API reference](api.md) for plan, GitOps, and webhook endpoints.
