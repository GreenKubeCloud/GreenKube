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
| Plan executor | Executes approved operations, creates commits and PRs. Runs in the controller (`plan-executor` loop) and, in `worker` mode, also in dedicated worker pods. |
| Automation controller loop | Runs in the controller, under one database lease: webhook outbox, PR polling, branch cleanup, plan expiry and retention. |
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

The **Automation & GitOps** page shows one card per controller: Argo CD and Flux. Each card reports `Detected` (every resource kind is readable), `Not detected` (no CRD found), `Degraded` (only some kinds are readable; the card names the missing ones, and workloads reconciled through them cannot be bound) or `Unknown` (the status could not be read). The Flux card lists its reconcilers (Kustomizations, HelmReleases) and sources (GitRepositories, OCIRepositories, HelmRepositories, Buckets) with the status of each.

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
- `pullRequests.draft`, `labels`, `reviewers`, and `teamReviewers` are preflighted against provider capabilities. Unsupported options fail the operation instead of being silently dropped. Derived labels, drafts and CODEOWNERS reviewers are best effort (see [Pull-request content](#pull-request-content)).

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

While evidence is still being collected, the `warming_up` blocker gives an estimate in `eligible_at`. **Preview plan** runs every gate and returns the plan or a typed rejection; it does not open a PR. The former `GET /recommendations/{id}/apply-pr/eligibility` endpoint is deprecated (see [API](api.md)). If a gate rejects a plan at preview time, inspect `policy_result.checks` for the observed value and configured limit. Prefer collecting representative metrics. You may adjust `automation.policy.minCoverage`, `minSamples`, `minConfidence`, and the other policy fields in your values file when your risk policy requires different thresholds; lowering them trades evidence quality for more eligible PRs. Keep any reduced thresholds local to a controlled test unless they have been explicitly approved for your environment.

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

## Supported action kinds

What a pull request can change is defined by a registry of action kinds (see [Architecture: action framework](architecture.md#action-framework)). Each kind defines its operations, the resources it touches, its invariants, its pre-apply baseline, how apply detection reads it, its rollback and its PR section. A recommendation becomes a PR only when a registered kind can express it.

| Kind | Operation | What it changes | Renderers | Rollback | Apply detection |
|---|---|---|---|---|---|
| `container_resources` | `set_container_resources` | CPU/memory requests (and, on raw manifests and Helm, limits) of named containers of a `Deployment`, `StatefulSet`, `DaemonSet`, `Job`, `CronJob`, `ReplicaSet`, `ReplicationController` or `Pod` | Raw, Kustomize (requests only), Helm values, Flux HelmRelease post-renderer | Restores the recorded pre-apply requests (request-only changes) | Live per-container request within tolerance of the planned value |

Every other recommendation type remains visible but shows "Manual change required" and cannot open a PR. Every plan, whatever its kind, is render-validated the same way: the rendered resources after the edit must differ from the rendered resources before it by exactly the kind's expected change (the fields it sets, plus the resources it creates or deletes), and the kind's invariants must hold (for container resources, every request stays within its limit). Anything else fails closed.

## Supported source layouts

Only CPU and memory rightsizing recommendations (the `container_resources` kind) currently have GitOps action planners. Other recommendation types remain visible but show "Manual change required" and cannot open a PR.

| Source | Supported | Notes |
|---|---|---|
| Argo CD raw YAML | Yes | Uses the Application's resource inventory and a raw manifest planner. |
| Argo CD Kustomize | Yes | Plans the overlay and render-validates the result. |
| Argo CD Helm with a local chart and Git-managed values files | Yes | The chart must live in the same repository and revision as the values. Infers a unique values file/container mapping when possible; otherwise offers a server-validated one-time mapping. |
| Argo CD Helm with a remote chart (Helm repository, OCI, or another Git repository) | No | The chart is not in the snapshot; fails closed. |
| Argo CD Helm with inline `valuesObject` / `parameters` | No | Fails closed; move values to a Git-managed values file. |
| Argo CD multi-source Helm | Partial | Only when the chart is local to the values repository; source indices are handled server-side. A remote chart source fails closed. |
| Flux Kustomization (raw/Kustomize) | Yes | Uses the Flux inventory and exact source revision. The overlay is rendered as Flux builds it: `spec.targetNamespace`, `namePrefix` and `nameSuffix` are applied to the root Kustomization for the render only (see below). |
| Flux HelmRelease with local same-revision Git chart | Yes | Renders `valuesFiles`, `valuesFrom`, and inline values, then applies the managed post-renderer edit. |
| Flux HelmRelease `chartRef` to a Git-backed HelmChart | Yes | The HelmChart must exist in the same immutable snapshot. |
| Flux HelmRelease with remote HelmRepository/OCI chart | No | The chart artifact is not in the snapshot; fails closed. |
| Flux `valuesFrom` that is runtime-only, cross-repository, cross-revision, or uses `targetPath` | No | Fails closed with a typed reason. |
| Manual binding resolution | Yes | An operator can select one server-provided candidate for an ambiguous/unresolved binding. |

When discovery cannot choose a single owner, the binding is `ambiguous`, `unresolved`, or `blocked` and plans fail closed. The **Automation & GitOps** page lists actionable bindings and their safe candidate provenance. Manual resolutions are generation-bound, expire at `automation.bindingTtl` (default 30 minutes), and are written to the audit trail.

### Flux Kustomizations

Flux writes `spec.targetNamespace` (and `spec.namePrefix`/`spec.nameSuffix`) into the root Kustomization before `kustomize build`, so the deployed workload rarely has the namespace written in the manifests. GreenKube plans these overlays as follows:

- It renders the overlay with the same overrides, for validation only (they are never written to Git), and requires exactly one rendered workload with the live identity. `targetNamespace` must equal the workload's namespace.
- Kustomize applies an overlay's patches before its own namespace and name transformers, so the patch selector names the workload as it enters the overlay: GreenKube renders the overlay once more without `namespace`, `namePrefix` and `nameSuffix` and requires exactly one match. A namespace-less manifest is selected as namespace `default`, which is how Kustomize matches it.
- It fails closed when the result is ambiguous or cannot be reproduced: `namePrefix` or `nameSuffix` set both on the Flux Kustomization and in the overlay; Flux `spec.patches`, `spec.images` or `spec.components` (applied after GreenKube's patch); `spec.postBuild` substitution variables in the workload's name, namespace, container names or container resources (values from `substituteFrom` are not visible to GreenKube; other fields are substituted the same way before and after the patch and are not a blocker); a namespace-less workload without `targetNamespace`.
- Kustomize rollback renders the overlay the same way.

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

Plan operations are durable and idempotent: `queued`, `running`, `retrying`, `succeeded`, `failed`, `cancelled`. Follow an operation through `GET /api/v1/automation/plans/{plan_id}` (`operation_id`, `operation_status`); `GET /api/v1/automation/operations/{operation_id}` is deprecated.

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

- `labels` are added to every PR on top of the derived labels below. Labels no longer have to exist beforehand: GitHub and GitLab create missing labels implicitly, and GreenKube creates them through the Gitea label API.
- `reviewers` and `teamReviewers` are always requested; they are also the fallback when a repository has no CODEOWNERS entry for the touched paths.
- GitHub supports draft PRs, labels, reviewers, and team reviewers.
- GitLab supports draft MRs, labels, and numeric reviewer IDs; team reviewers are not supported.
- Gitea supports labels and reviewers; draft PRs and team reviewers are not supported.

Provider capabilities are checked before a branch or commit is created. Missing capabilities surface as a typed `provider_capability_missing`-style error.

## Pull-request content

The PR is where most reviewers meet GreenKube, so it is written to be decided on in 30 seconds. Configure it under `automation.pr`:

```yaml
config:
  externalUrl: https://greenkube.example.com # deep links; empty means no links
automation:
  pr:
    titlePrefix: greenkube
    template: '' # empty uses the built-in template
    defaultLabels: true
    draftHighRisk: true
    codeowners: true
    commitStatus: true
    lifecycleComments: true
    lifecycleLookbackDays: 30
```

**Title.** `greenkube: rightsize payments/api (cpu 500m→200m, mem 1Gi→512Mi) · −$412/yr · −18 kgCO₂e/yr`. The verb follows the action (`rightsize`, `raise resources of`, `autoscale`, `delete`, or `revert …` for rollbacks). Titles are at most 72 characters: the resource details, then the CO₂e figure, are dropped before the title is cut, and the full summary is always in the body. `titlePrefix` sets the conventional-commit prefix; an empty prefix drops it.

**Body.** The built-in template renders these sections:

1. **Summary**: one sentence and a totals table per month and per year, for cost and CO₂e. The values are *projected* by the Impact Model, with the realization factor; they are never mixed with measured savings.
2. **Changes**: per container, only the resources that change, as current → proposed with the delta.
3. **Why**: per recommendation, the finding, the consensus line (sources and proposed value), the evidence (window, samples, coverage, p95, p99, max, restarts and OOM events) and a link to the recommendation.
4. **Risk & what to watch**: the risk level and factors, headroom over p95 and max, and the signals to watch for 24 hours. An *Autoscalers and disruption budgets* list explains the HPA, VPA and PDB interactions: the plan's policy checks (`hpa_interaction`, `vpa_interaction`, `pdb_interaction`, with the autoscalers they found, or "not checked" when the evidence was missing), the policy warnings about them, the risk factors `hpa_scales_on_resource`, `hpa_present`, `vpa_conflicting_writer` and `pdb_blocks_disruption`, and, for a plan that adds an HPA, how the new autoscaler interacts with requests, other autoscalers and PDBs. When the policy result has no HPA, VPA or PDB evidence, the list says so.
5. **Assumptions**: unit prices and their source, observed cost sources, carbon intensity and its source, the CO₂e basis (what the projection counts), PUE, realization factor, carbon price and Impact Model version. The intensity is the projection window's average; its source lists the intensity sources the Impact Model recorded and, when it records none (Impact Model 1.1), states how the value was derived: the stored grid intensity of the observed samples, or the node profile's intensity where a sample has none.
6. **Rollback**: revert the PR, or the GreenKube rollback pull request from the change page. Layouts without automated rollback say so.
7. **Verification plan**: when measurement starts and ends, the strategy, and where results are posted.
8. Plan provenance and collapsible source and rendered diffs.
9. A machine-readable footer on the body's last line: `<!-- greenkube:v1 {"plan_id":…,"recommendation_ids":[…],"revision":…,"change_set_id":…,"digest":…} -->`. Readers (`greenkube.automation.pr_content.parse_footer`) trust only the footer that ends the body, so a look-alike inside a diff, a recommendation reason or a custom template is ignored. Text appended after the footer by a manual edit makes the footer unreadable rather than replacing it.

The body is at most 60,000 characters. When it would be longer, diffs are truncated first, then dropped; sections 1–7 and the footer are always kept. The sections share the space the template's own text leaves (each at most 6,000 characters; short sections keep their full text), so a long custom template shortens sections instead of cutting them off. Blank lines are collapsed only where an empty section leaves a gap; diffs are shown exactly. A recommendation whose Impact Model projection mixes currencies is left out of the cost totals and the title, with a note.

**Template.** `automation.pr.template` (`GITOPS_PR_TEMPLATE`) replaces the built-in layout. It is Markdown with Jinja2-style placeholders: the sections `{{ summary }}`, `{{ changes }}`, `{{ why }}`, `{{ risk }}`, `{{ assumptions }}`, `{{ rollback }}`, `{{ verification }}`, `{{ provenance }}`, `{{ diffs }}`, and the values `{{ title }}`, `{{ plan_id }}`, `{{ digest }}`, `{{ workload }}`, `{{ namespace }}`, `{{ repository }}`, `{{ branch }}`, `{{ recommendation_ids }}`, `{{ risk_level }}`, `{{ plan_url }}`. Templates are logic-free: no loops, filters or code, and rendered values are never expanded again. An unknown placeholder or an unterminated `{{` fails at startup instead of leaking into PRs. The `greenkube:v1` footer is always appended, whatever the template.

```yaml
automation:
  pr:
    template: |
      {{ summary }}

      Owner checklist: confirm the change window with the on-call team.

      {{ changes }}

      {{ risk }}

      {{ rollback }}

      {{ verification }}

      {{ diffs }}
```

**Commit.** The commit GreenKube creates in the GitOps repository uses the PR title as its subject and carries `GreenKube-Plan:` and `GreenKube-Recommendations:` trailers (plus `GreenKube-Rollback-Of:` for rollbacks), so the history of the GitOps repository points back to the plan.

**Deep links.** With `config.externalUrl` (`GREENKUBE_EXTERNAL_URL`) set, PRs, commit statuses and status comments link to `/recommendations/{id}`, `/changes/{plan_id}` and `/ledger?change=…`. Without it they carry no links.

### Labels, drafts and reviewers

- **Labels** (`defaultLabels`): `greenkube`, `optimization/<capability>` (for example `optimization/cpu-rightsizing`), `risk/<level>`, and `reliability` when the plan raises a resource. Missing labels are created where the provider allows it (see above).
- **Drafts** (`draftHighRisk`): a plan whose riskiest recommendation is `high` opens as a draft PR on GitHub, a `Draft:` merge request on GitLab, and with a `WIP:` title on Gitea. If the provider refuses the draft (for example a GitHub plan without draft PRs), GreenKube opens the PR ready for review instead of failing.
- **CODEOWNERS** (`codeowners`): GreenKube reads `CODEOWNERS` at the plan's base revision from the provider's documented locations (GitHub: `.github/`, root, `docs/`; GitLab: root, `docs/`, `.gitlab/`; Gitea: root, `docs/`, `.gitea/`). GitHub and GitLab syntax are supported, including GitLab sections and default owners, and so are Gitea's regular-expression rules. The owners of the touched paths are requested as reviewers: `@user` as a user, `@org/team` as a team of the repository's organization (GitHub). E-mail owners and malformed lines are ignored, and so are Gitea rules that a backtracking engine could take exponential time on (a repeated group that itself repeats or alternates, lookarounds, rules over 512 characters). GitHub is never asked to review its own PR author, and when GitHub refuses a batch of reviewers (one unknown or non-collaborator login fails the whole request), each reviewer is requested alone so the valid ones are still asked. GitLab username-to-ID lookups are cached for an hour. Configured `reviewers` and `teamReviewers` are requested as well.

Labels, reviewers and the validation status are added after the PR is open and recorded. A provider refusal is logged and never fails or retries the plan operation.

## Lifecycle feedback on the PR

**Validation status** (`commitStatus`). GreenKube sets the commit status `greenkube/validation` on the PR head: `success` when the rendered diff validated and policy allowed the plan, `failure` otherwise. The description says which (for example `render diff valid; policy passed with 1 warning(s)`), and the status links to the change page when `externalUrl` is set.

**Status comment** (`lifecycleComments`). After the merge, GreenKube keeps one comment on the PR up to date with a timeline:

| Step | Shows |
|---|---|
| Merged | The landed commit and time. |
| Synced | The controller (Argo CD Application or Flux object) and the revision it runs, or the sync failure. |
| Applied | When the rollout completed. |
| Measuring | The measurement end (ETA). |
| Outcome | `realized` with the measured annual savings and their 90% confidence interval, or `not_realized`, `inconclusive`, `contaminated`, or rollback review; with a link to the ledger. |

The comment is posted once and then edited, never duplicated: it starts with the marker `<!-- greenkube:lifecycle v1 -->`, and GreenKube edits the marked comment that its own identity wrote. That identity is the token's user (`GET /user`) or, when the token may not read its own profile, the author of the PR, which GreenKube opened. A marked comment written by anyone else is never adopted or edited, so nobody can have GreenKube's status written into a comment they control, and a human comment is never overwritten. If the token changes to another user, the new user posts one new status comment and keeps it up to date. A body identical to the one last published costs no provider call. The sync step is posted as soon as the sync observer records it; applied, measuring and outcome are refreshed by the leader controller every 5 minutes for PRs merged within `lifecycleLookbackDays` (at most 25 comment writes per cycle). Outcomes come from change measurement; with change measurement off, from the interim verifier.

Comments and statuses need a token that can write PR comments and commit statuses (GitHub: `Pull requests` and `Commit statuses` write; GitLab: `api`; Gitea: `write:repository` and `write:issue`, plus `read:user` so GreenKube can read its own identity; without it, the PR author is used).

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

## Where automation runs

All automation runs in the controller process (roadmap M8, F8). The API only handles requests: it previews and approves plans, and it writes provider webhooks to a durable outbox. It runs no worker and no expiry sweep, so API replicas are safe to scale. See [Process topology](architecture.md#process-topology).

The controller runs two leader-gated loops:

- `plan-executor` claims and executes queued plan operations (at most 10 per 5-second tick).
- `automation-controller` runs one tick every 5 seconds under a single database lease. The tick drains the webhook outbox, polls open pull requests every `automation.prPollIntervalSeconds`, then cleans up branches, reconciles exhausted operations, expires plans (every 60 s) and runs retention (daily).

| Mode | When | Behaviour |
|---|---|---|
| `inline` | Default for `standalone` when automation is enabled. | Only the controller executes plan operations. |
| `worker` | Default for `production` when automation is enabled. | The controller executes operations, and dedicated worker pods execute them too (`production.components.automation.replicas`, or the standalone `automation-worker`). Workers do nothing else. Operation leases make any number of executors safe. The workers' authenticated metrics listener uses `automation.workerMetricsPort` (default 8001). |
| `disabled` | Automation off. | No plan execution, webhook outbox or PR polling. Plan expiry and retention still run while Git providers are configured. |

Loops are supervised by the controller's loop registry:

- A tick that raises or times out is logged with an error code, counted in `greenkube_loop_runs_total{outcome="failure"|"timeout"}`, and retried with exponential backoff. The other loops keep running.
- A loop task that crashes is restarted.
- The controller serves `/readyz` and `/loops` on its metrics port, and the `greenkube_loop_*` metrics (see [Observability](observability-slos.md#controller-loops)). `GET /api/v1/automation/status` reports executor health through the worker heartbeats that every executor writes to the database.

## Intervals and operations

| Value | Default | Purpose |
|---|---|---|
| `automation.discoveryInterval` | `5m` | Periodic Argo CD/Flux discovery refresh (`1s`–`1h`). |
| `automation.bindingTtl` | `30m` | Binding and manual-resolution freshness (`1s`–`24h`). |
| `automation.prPollIntervalSeconds` | `120` | PR polling fallback (10–86400). |
| `automation.syncPollIntervalSeconds` | `30` | Controller sync observation interval (10–86400). |
| `automation.syncTimeoutSeconds` | `1800` | Time before an unsynced merged plan becomes `sync_failed` (60–86400). |

The automation controller loop fails an `applying` plan when its operation is terminal and no PR exists. Its expiry step reads only the plans whose TTL elapsed, in bounded batches. It runs once per cluster, on the controller that holds the Lease, instead of in every API replica.

A daily retention job removes rendered file contents and terminal operation/webhook rows older than 90 days:

- A pruned plan keeps a compact `file_plan_summary`: paths, original blob SHAs, content digests, diff statistics, validation and digest. `GET /api/v1/automation/plans/{plan_id}` and the workload's policy history keep working. A pruned plan can no longer be rolled back automatically, because its file contents are gone; the rollback preview explains this, and a new preview with the desired resources replaces it.
- Plan metadata, PR records and recommendation audit events (`recommendation_events`) are never deleted.
- Webhook deliveries for pull requests that GreenKube does not track are stored as `ignored` and answered with `202`. They are never retried, and retention prunes them.

## Observability

- `GET /api/v1/automation/status` reports executor heartbeat age (`worker.inline_loop` is always `null` since the API runs no loop; loop state is on the controller's `/loops`), queue depth, plan backlog, discovery freshness, renderer configuration, and provider preflight/capability state. `token_configured` is `true` only when an enabled provider's credential Secret key resolves to a non-empty value; each provider also reports its own `token_configured`. A provider's `status` is `unverified` until a real call: the plan executor sets `verified` (with `last_checked_at`) once it opened or adopted a pull request, and `error` with `last_error_code` `auth_failed` (HTTP 401) or `permission_denied` (403) when the provider refuses the token. Changing a provider's endpoint or credential reference resets it to `unverified`; other configuration changes keep it.
- `GET /api/v1/automation/pull-requests` lists safe pending/open PR metadata.
- `GET /prometheus/metrics` on the controller metrics port exposes the automation loop, PR polling and plan-execution metrics. In production worker mode, a separate Service/ServiceMonitor scrapes the dedicated executors' process-local registry.
- Optional `monitoring.prometheusRule.enabled` installs alerts for stuck operations/plans, sync failures, webhook lag, and stale worker heartbeats.

SLOs and example alert rules are documented in [Observability and SLOs](observability-slos.md). The full metric inventory is in [Prometheus and Grafana](prometheus-grafana.md).

## Limitations

- Only CPU and memory rightsizing recommendations have GitOps planners.
- Remote Helm/Kustomize artifacts, Argo CD inline Helm values, Flux `valuesFrom.targetPath`, cross-repository/revision values, chart dependencies, and unmanaged post-renderers fail closed.
- Argo CD Applications that are not stored in Git have no PR target.
- Provider archive downloads must be available at the resolved full commit SHA. GreenKube does not fall back to a mutable branch or per-file reads.
- Descendant sync confirmation needs a provider ancestry API: GitHub compare, GitLab merge base, or a Gitea release that serves `GET /repos/{owner}/{repo}/compare/{basehead}`. Without it, a controller that already runs a later commit stays pending (`ancestry_unavailable`) until the sync timeout.
- The planned-change check for a later commit compares only the files the plan edited, parsed as YAML or JSON. A later commit that overrides the planned values from another file (for example a second Helm values file) is not detected at sync time; apply detection then never matches the live workload and the plan stays `synced`. Files that no longer parse, or whose base content no longer matches the plan, never confirm a later commit.
- Gitea does not support draft PRs or team reviewers; GitLab does not support team reviewers. Configure only supported options. High-risk plans on Gitea are marked with a `WIP:` title instead of a draft.
- CODEOWNERS team owners are requested only on GitHub; on GitLab and Gitea only user owners become reviewers (GitLab resolves usernames to numeric user IDs through its user API).
- The "newer advice available" note of the status comment is a publisher hook; it shows only once recommendation revisions (E7) are wired to it.
- If the CNI does not enforce NetworkPolicy, keep the renderer disabled unless you have independently validated an equivalent egress boundary.

See the [API reference](api.md) for plan, GitOps, and webhook endpoints.
