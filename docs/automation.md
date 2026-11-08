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
| Controller runtime | Observes the Argo CD/Flux revision and readiness after merge (the landed commit or a descendant of it), then gates apply detection and verification. |
| Webhook endpoint | Accepts signed GitHub/Gitea/GitLab delivery events. Polling remains the recovery path. |

## Prerequisites

- Kubernetes cluster with GreenKube installed and healthy.
- Argo CD and/or Flux controllers installed if you want discovery-driven plans. GreenKube only plans workloads it can bind to a supported Git source.
- A CNI that enforces NetworkPolicy. The renderer sandbox relies on an egress-deny policy. If your CNI does not enforce NetworkPolicy:
  - keep automation disabled, or
  - set `rendererSandbox.egressFirewall.enabled=true` on clusters that permit `NET_ADMIN` and validate the resulting boundary, or
  - leave rendering disabled and validate plans only through the API without applying them.
- `rendererSandbox.egressFirewall.privilegedInitContainer` grants `NET_ADMIN` to a short-lived init container. Leave it disabled in production and enable it only after reviewing the cluster security model and validating the resulting egress boundary.
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
- Set `webBaseUrl` when GreenKube must reach Gitea through an in-cluster API URL but users open pull requests through a different browser-reachable URL. It must be an HTTP(S) origin (no repository path or `/api/v1`); it is used only for pull-request links.
- `pullRequests.draft`, `labels`, `reviewers`, and `teamReviewers` are preflighted against provider capabilities. Unsupported options fail the operation instead of being silently dropped.

## Create and validate a pull request

After configuring a Git provider, repository allowlist, and secure renderer boundary:

1. Check `GET /api/v1/automation/status`. Automation must be enabled and ready, the provider token must be configured, and the worker must be healthy.
2. In **Automation & GitOps**, confirm the target workload has a current, resolved binding to a repository allowed by the selected provider.
3. In **Recommendations**, choose a CPU or memory rightsizing recommendation that offers **Preview plan**. Cards that cannot become a pull request list their blockers and how to fix them (the recommendation's `eligibility`). Review its **Evidence** before previewing.
4. Select **Preview plan**. Review every policy check, source and rendered diff, validation result, and digest. Approve only the exact plan you intend to propose.
5. Follow the queued operation in the plan dialog, then use the **Pull requests** tab and **View PR** to review the provider PR. A PR does not change the cluster until a reviewer merges it and the GitOps controller syncs the merged revision.

### Pull-request eligibility and observability coverage

Automation does not create a PR for every visible recommendation. By default, each recommendation in a plan must have at least **36 samples**, **50% evidence coverage** (`automation.policy.minCoverage: 0.5`), **60% confidence** (`automation.policy.minConfidence: 0.6`), and risk at or below `automation.policy.maxRiskLevel: medium`. A plan that groups multiple recommendations must satisfy the configured gates for the affected recommendations. Other checks include a resolved GitOps binding, a supported CPU/memory rightsizing action, no conflicting workload evidence, and the per-workload open-PR and cooldown limits.

Evidence coverage is the share of expected CPU or memory observation points present in the recommendation's observation window, sampled at the configured Prometheus query step. It is not the GitOps binding coverage displayed on the **Automation & GitOps** page; that separate ratio reports resolved workloads among discovered workloads.

Inspect the recommendation's **Evidence** panel and its blockers, or read its `eligibility` through the API: `can_open_pr`, then each blocker's `code`, `message` and `fix` ([eligibility codes](reference/eligibility-codes.md)). Eligibility runs the same policy evaluator as plan creation, read-only. After opening the API port-forward and exporting the key as described in the [installation guide](installation.md#reach-the-frontend), run:

```bash
REC_ID=123 # replace with the recommendation ID shown in GreenKube
curl -fsS -H "Authorization: Bearer ${GREENKUBE_API_KEY}" \
  "http://localhost:8000/api/v1/recommendations/${REC_ID}" |
  jq '{id, confidence, risk_level, evidence: {sample_count: .evidence.sample_count, coverage_ratio: .evidence.coverage_ratio}, eligibility}'
```

While evidence is still being collected, the `warming_up` blocker gives an estimate in `eligible_at`. If a gate rejects a plan at preview time, inspect `policy_result.checks` for the observed value and configured limit. Prefer collecting representative metrics. You may adjust `automation.policy.minCoverage`, `minSamples`, `minConfidence`, and the other policy fields in your values file when your risk policy requires different thresholds; lowering them trades evidence quality for more eligible PRs. Keep any reduced thresholds local to a controlled test unless they have been explicitly approved for your environment.

## Signed webhooks (optional)

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
4. A successful preview contains a `sha256:` digest, changed files, source and rendered diffs, the renderer's warnings (credentials redacted), validation state, and an expiry. Previewing the same workload again supersedes its previous `draft`/`validated` plan instead of returning `409`. Closing a preview calls `DELETE /api/v1/automation/plans/{plan_id}`, which marks it `superseded`.
5. Approval must present that exact digest. Unrelated commits on the target branch do not stale a plan: when every file the plan touches is unchanged between the planned base revision and the current head, the plan is committed on the moved head. That head is recorded as `committed_on_revision`, next to the planned `base_revision`, and the pull-request body lists both. Rollback plans follow the same rule; their identity stays derived from the planned base revision. A change to a touched file or a stale digest returns `409` and requires a new preview.
6. The durable worker revalidates the source, creates one atomic multi-file commit on a deterministic branch, and opens or adopts a matching PR. Repeated requests reuse the open PR instead of creating duplicates. When a worker reclaims an operation whose previous worker lost its lease, it first adopts an open PR on the plan's deterministic branch (`pr_open`), before any staleness check. Opening a PR costs a bounded number of provider calls that does not depend on repository size (see [Provider API cost](#provider-api-cost)).
7. After merge, linked recommendation actions enter `sync_pending` and the plan moves to `merged`. GreenKube records the commit that landed on the base branch: the merge commit, or for GitLab merges without one the squash commit, or the merge request head for fast-forward projects (the plan's audit reason is `pull_request_merged`, `pull_request_merged_squash_commit` or `pull_request_merged_fast_forward_head`). A PR closed without merging moves the plan to `closed`; GitHub's leftover test-merge SHA is ignored.
8. The controller observes the Argo CD/Flux source revision and readiness. The plan moves to `synced` when the healthy controller runs the landed commit (`sync_confirmed`), or a later commit that contains it and still carries the planned values, for example after another PR or an image write-back landed first (`sync_confirmed_descendant`). For a later commit GreenKube asks the provider two questions, each cached per commit:
   - Does the observed commit contain the landed commit? This uses the provider's compare or merge-base API.
   - Does it still carry the planned file changes? GreenKube reads the blob IDs of the planned paths at the observed commit. When a later commit edited a planned file, it reads that file at the observed and base commits and checks structurally that every value the plan set is still set and every value it removed is still absent. Unrelated edits in the same file, such as an image tag, are accepted. Containers and other named list items are matched by name.

   A plan whose planned values were reverted, overwritten or deleted by a later commit stays pending with `planned_change_not_present` and is never confirmed. Other pending reasons are `revision_behind_merge` (the controller still runs an older commit), `revision_not_descendant` (a diverged commit), `ancestry_unavailable` and `planned_change_unverified` (the provider cannot answer yet). After the timeout the plan moves to `sync_failed`, which does not block new plans for the workload. A plan that failed only for the timeout is re-checked later and recovers if the controller reaches a confirming revision; once a later commit is seen to have reverted or overwritten it, its records keep `planned_change_not_present` and are no longer re-checked. A plan never jumps directly to `applied`.
9. Apply detection requires a synced plan and an exact per-container request match on the live workload. Verification then uses the normal post-apply health and savings gates.

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

Discovery also supplies live workload labels, rollout state, controller suspend/health, and HPA, VPA and PDB evidence. A configured freeze label blocks only matching workloads.

- **HPA.** An HPA blocks only the resource it scales on. A CPU-utilization target blocks CPU changes; a memory change on the same workload passes with a warning. A `ContainerResource` metric blocks only its own container. A plan that adds an HPA conflicts with an existing one.
- **VPA.** A VPA that targets the workload in `Auto`, `Recreate`, `InPlaceOrRecreate` or `InPlace` mode rewrites requests itself, so it blocks the plan. `Initial` mode passes with a warning; `Off` passes.
- **PDB.** A PDB that blocks the rolling restart rejects the plan.

`automation.policy.missingEvidence` sets what these live gates do when discovery cannot collect their evidence:

- `fail` rejects the plan with `policy_evidence_missing`.
- `warn` allows it and adds a warning to `policy_result.warnings`.

The chart defaults to `fail` for `profile: production` and `warn` for `standalone`.

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

## Provider API cost

Opening a PR never lists or downloads the whole repository. The worker compares the plan branch with the plan's immutable base commit and reads only the planned paths:

| Provider | Branch-state check | Commit |
|---|---|---|
| GitHub | One recursive tree read per ref (`GET /git/trees/{ref}?recursive=1`); the base tree is cached for the operation and blob IDs are compared. | Git Data API: one tree (new file contents inline) → one commit → one fast-forward-only ref update. |
| GitLab | One direct compare (`GET /repository/compare?straight=true`) plus the blob IDs of the planned paths. | One native multi-action commit pinned to `start_sha`. |
| Gitea | One commit read with its affected files (`GET /commits?sha=…&files=true`) plus the blob IDs of the planned paths; other branch shapes fall back to a paginated tree comparison. | One `POST /contents` on a throwaway branch, then a compare-and-swap branch update. |

A typical plan opens its PR in about 12–14 provider calls, whatever the repository size (a 5,000-file repository is covered by the test suite). Every plan operation also runs under a provider call budget of 200 HTTP requests, retries included: an operation that would exceed it stops before the next request and fails with `provider-call-budget-exceeded`, so a pathological repository cannot exhaust the provider rate limit. Sync observation has its own smaller budgets: 10 requests per ancestry check (`sync_ancestry`) and 20 per planned-change check (`sync_planned_change`, at most three reads per planned file). Calls are exported as `greenkube_git_provider_requests_total`, `greenkube_git_provider_operation_calls` and `greenkube_git_provider_budget_exhausted_total`.

When GitLab reports `compare_timeout` for the direct compare, the worker falls back to comparing two full repository trees. GitLab pages that tree listing 100 entries at a time, so on repositories with more than about 10,000 files the fallback exceeds the 200-request budget and the operation fails closed with `provider-call-budget-exceeded` instead of consuming the rate limit.

## Rollback

A synced plan whose recommendations remain in `rollback_review` can produce a validated inverse preview:

- The API re-reads the original plan's base revision through the authorized provider and rejects files that changed after the forward plan.
- The rollback preview uses the same digest approval and worker queue.
- A recommendation becomes `reverted` only after the rollback plan is `synced`, a post-baseline workload generation is observed, and live per-container requests match the original pre-apply baseline.
- RAW and Kustomize rollback are built in. Kustomize rollback renders the overlay in the same renderer sandbox as forward planning and rejects any change outside the exact target. The supported Argo CD `HELM_VALUES` subset and Flux `HELM_RELEASE` subset restore the same Git-managed inputs as their forward plans.
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

Worker loops are supervised:

- An iteration that raises is logged with an error code and retried with jittered backoff.
- A loop task that crashes or exits is restarted with backoff.
- In inline mode, `GET /api/v1/health` returns `503` (`degraded`) and `GET /api/v1/automation/status` reports `ready: false` while the in-process loop is down or its heartbeat is stale. The readiness probe no longer reports a dead queue as ready. The chart's liveness probe uses `/api/v1/health/heartbeat`, so a stalled loop takes the pod out of the Service but does not restart the API container.

## Intervals and operations

| Value | Default | Purpose |
|---|---|---|
| `automation.discoveryInterval` | `5m` | Periodic Argo CD/Flux discovery refresh (`1s`–`1h`). |
| `automation.bindingTtl` | `30m` | Binding and manual-resolution freshness (`1s`–`24h`). |
| `automation.prPollIntervalSeconds` | `120` | PR polling fallback (10–86400). |
| `automation.syncPollIntervalSeconds` | `30` | Controller sync observation interval (10–86400). |
| `automation.syncTimeoutSeconds` | `1800` | Time before an unsynced merged plan becomes `sync_failed` (60–86400). |

A periodic reconciler fails an `applying` plan when its operation is terminal and no PR exists. The expiry sweep reads only the plans whose TTL elapsed, in bounded batches.

A daily retention job removes rendered file contents and terminal operation/webhook rows older than 90 days:

- A pruned plan keeps a compact `file_plan_summary`: paths, original blob SHAs, content digests, diff statistics, validation and digest. `GET /api/v1/automation/plans/{plan_id}` and the workload's policy history keep working. A pruned plan can no longer be rolled back automatically, because its file contents are gone; the rollback preview explains this, and a new preview with the desired resources replaces it.
- Plan metadata, PR records and recommendation audit events (`recommendation_events`) are never deleted.
- Webhook deliveries for pull requests that GreenKube does not track are stored as `ignored` and answered with `202`. They are never retried, and retention prunes them.

## Observability

- `GET /api/v1/automation/status` reports worker heartbeat age, the inline loop state (`worker.inline_loop`: state, heartbeat age, restarts, last error code), queue depth, plan backlog, discovery freshness, renderer configuration, and provider preflight/capability state. `token_configured` is `true` only when an enabled provider's credential Secret key resolves to a non-empty value; each provider also reports its own `token_configured`.
- `GET /api/v1/automation/pull-requests` lists safe pending/open PR metadata.
- `GET /prometheus/metrics` exposes control-loop metrics. In production worker mode, a separate Service/ServiceMonitor scrapes the worker's process-local registry.
- Optional `monitoring.prometheusRule.enabled` installs alerts for stuck operations/plans, sync failures, webhook lag, and stale worker heartbeats.

SLOs and example alert rules are documented in [Observability and SLOs](observability-slos.md). The full metric inventory is in [Prometheus and Grafana](prometheus-grafana.md).

## Limitations

- Only CPU and memory rightsizing recommendations have GitOps planners.
- Remote Helm/Kustomize artifacts, Argo CD inline Helm values, Flux `valuesFrom.targetPath`, cross-repository/revision values, chart dependencies, and unmanaged post-renderers fail closed.
- Argo CD Applications that are not stored in Git have no PR target.
- Provider archive downloads must be available at the resolved full commit SHA. GreenKube does not fall back to a mutable branch or per-file reads.
- Descendant sync confirmation needs a provider ancestry API: GitHub compare, GitLab merge base, or a Gitea release that serves `GET /repos/{owner}/{repo}/compare/{basehead}`. Without it, a controller that already runs a later commit stays pending (`ancestry_unavailable`) until the sync timeout.
- The planned-change check for a later commit compares only the files the plan edited, parsed as YAML or JSON. A later commit that overrides the planned values from another file (for example a second Helm values file) is not detected at sync time; apply detection then never matches the live workload and the plan stays `synced`. Files that no longer parse, or whose base content no longer matches the plan, never confirm a later commit.
- Gitea does not support draft PRs or team reviewers; GitLab does not support team reviewers. Configure only supported options.
- If the CNI does not enforce NetworkPolicy, keep the renderer disabled unless you have independently validated an equivalent egress boundary.

See the [API reference](api.md) for plan, GitOps, and webhook endpoints.
