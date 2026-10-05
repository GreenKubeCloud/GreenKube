# Development Plan: "Apply Recommendation" → Automated PR

> **Implementation status:** implemented (Phase 4 of the [optimization engine specification](specs/optimization-engine.md)). This document is kept as the design reference and now describes the shipped behavior, including the deviations listed at the end of this section.
>
> Deviations from the original draft:
> - `owner_kind`/`owner_name` shipped with migration `0010` (engine phase 1);
> the pull-request table is migration `0013`.
> - A third provider, **Gitea**, is supported alongside GitHub and GitLab
> (used by the self-contained local GitOps demo).
> - `POST /recommendations/{id}/apply-pr` handles `dry_run=true` synchronously.
> A normal request persists a durable automation operation and returns `202 Accepted`; the automation worker performs the provider mutation and clients poll `/api/v1/automation/operations/{operation_id}` for its state. In the Helm chart, the `production` profile deploys this worker; the default `standalone` profile does not.
> - Merge detection uses the Kubernetes API (apply detector) rather than Git
> webhooks/polling, per the phase 3 decision.

## 1. Goal & Scope

Turn GreenKube recommendations from passive suggestions into actionable fixes. A user requests a pull request for an eligible recommendation; GreenKube queues the operation, and the automation worker opens a pull request against the workload's Git repository.

**Decided scope:**

- Source discovery via **workload annotations** (primary).
- Git platforms: **GitHub**, **GitLab** and **Gitea**, both SaaS and self-hosted.
- Recommendation types: **CPU/memory rightsizing only** (v1).
- The API accepts and persists automation operations; a worker executes provider mutations.
- `ruamel.yaml` is a **production dependency** (preserves comments/formatting).
- A dedicated **`POST /api/v1/recommendations/{id}/apply-pr`** endpoint (distinct from the existing "record applied" endpoint).

The existing `PATCH /recommendations/{id}/apply` only *records* that a change was made (it does not touch Git). This feature is additive and does not replace it.

---

## 2. Architecture Overview

The request path and provider worker use pluggable layers following the existing clean/hexagonal conventions:

```
Frontend "Create PR" button
        │ POST /api/v1/recommendations/{id}/apply-pr
        ▼
AutomationService enqueues durable operation
        │ 202 Accepted + operation_id
        ▼
Automation worker
  │ 1. Load RecommendationRecord (owner_kind + namespace + name + evidence)
  │ 2. SourceResolver → ManifestSource (workload annotations)
  │ 3. Read manifest → RightsizingPatcher (ruamel.yaml round-trip)
  │ 4. GitProvider creates branch, commit and pull request
  │ 5. Persist result and recommendation lifecycle event
  ▼
Client polls operation status; user reviews and merges the PR
```

Key principle: **no knowledge of ArgoCD/Flux anywhere**. The bot only needs (a) a way to find the file (annotations), (b) a way to talk to the Git platform (adapters), (c) a way to edit YAML (patchers).

---

## 3. Persistence

### 3.1 `owner_kind` on `recommendation_history`

Shipped with migration `0010_recommendation_sources.sql` (see the optimization engine spec). The DTOs carry `owner_kind`, `owner_name`, `source_ref` and the machine-readable `patch` plan.

### 3.2 Table `recommendation_pull_requests` (migration `0013`)

| column | type |
|---|---|
| id | PK auto |
| recommendation_id | FK → recommendation_history.id |
| provider | text (`github` / `gitlab` / `gitea`) |
| repo | text (`owner/name`) |
| base_branch | text |
| head_branch | text (nullable) |
| pr_number | integer (nullable) |
| pr_url | text (nullable) |
| status | text (`pending` / `open` / `merged` / `closed` / `error`) |
| error | text (nullable) |
| created_at / updated_at | timestamptz / text |

- `PullRequestRepository` ABC (`storage/base_pull_request_repository.py`) with SQLite and PostgreSQL implementations sharing `storage/pull_request_mapper.py`.
- Registered in `core/factory.py` + `api/dependencies.py`.

---

## 4. Configuration

Secrets (read via `/etc/greenkube/secrets/` or env):

- `GIT_TOKEN` — PAT (GitHub/Gitea) or personal access token (GitLab).

Config (`config.recommendations.git.*` in Helm):

- `GIT_PROVIDER` — `github` | `gitlab` | `gitea` (default `github`)
- `GIT_API_BASE_URL` — override for GitHub Enterprise / self-hosted GitLab / Gitea
- `GIT_DEFAULT_BRANCH` — fallback base branch (default `main`)
- `GIT_COMMIT_AUTHOR_NAME` / `GIT_COMMIT_AUTHOR_EMAIL` — bot commit identity

The token is never logged. When `GIT_TOKEN` is empty the endpoint returns a clear "not configured" error and `GET /api/v1/automation/status` reports `token_configured: false`.

---

## 5. Backend Components

### 5.1 `src/greenkube/automation/source_resolver.py`

- `AnnotationSourceResolver` reads the workload object (Deployment, StatefulSet or DaemonSet) through the Kubernetes apps API.
- Annotation contract:
  - `greenkube.cloud/git-repo` (required) — repo URL
  - `greenkube.cloud/git-path` (optional) — manifest path; when omitted the bot
    lists the repository and searches for a matching `kind` + `metadata.name`
  - `greenkube.cloud/git-branch` (optional) — base branch override
- `SourceResolutionError` messages are surfaced verbatim to the UI.

### 5.2 `src/greenkube/automation/git/`

- `base.py` — `GitProvider` ABC plus `parse_repo_url`, `GitFile`, `GitRepository`.
- `github.py` — GitHub REST API (SaaS + Enterprise Server).
- `gitlab.py` — GitLab REST API (SaaS + self-managed), nested groups supported.
- `gitea.py` — Gitea/Forgejo REST API (self-hosted), used by the local demo.
- All use `httpx.AsyncClient`; `factory.py` selects the adapter from config.

### 5.3 `src/greenkube/automation/manifests/patcher.py`

- `RightsizingPatcher.patch_content(record, content, path)`:
  - Parses multi-document YAML with `ruamel.yaml` round-trip mode.
  - Finds the document with `kind == owner_kind` and `metadata.name == owner_name`.
  - Applies `set_container_resources` operations (CPU/memory requests) from the
    recommendation `patch`; falls back to the DTO values when no patch is set.
  - Returns the patched content plus a unified diff.
- `find_path(record, files)` powers repository discovery when no path annotation is present.

### 5.4 `src/greenkube/automation/service.py`

`AutomationService.apply_recommendation_pr(rec_id, request)` resolves and previews a patch for dry runs. Normal requests go through `enqueue_recommendation_pr`: the operation is persisted before provider mutations, then the worker performs discovery → patch → branch → commit → PR → persistence → lifecycle transition. Failures and retries are recorded on the durable operation and pull-request attempt.

### 5.5 `src/greenkube/automation/pr_body.py`

Renders the PR title and body from the evidence block (see the contract in the optimization engine spec §6.13): summary, impact, risk, evidence tables, proposed diff, verification plan, provenance and reviewer checklist.

---

## 6. API

- `POST /api/v1/recommendations/{id}/apply-pr`
  - Body: optional `{ base_branch?, dry_run? }`.
  - Validates the recommendation is rightsizing, resolves the source and builds
    the patch. `dry_run=true` returns `{status: "dry_run", diff, path, ...}`.
  - A normal request returns `202 Accepted` with an `operation_id` and
    idempotency metadata. Poll
    `GET /api/v1/automation/operations/{operation_id}` for the result; a
    successful provider operation records the pull request and moves the
    recommendation to `pr_open`.
- `GET /api/v1/recommendations/{id}/pull-requests` — PR attempts/status.
- `GET /api/v1/automation/status` — provider + token readiness.
- `GET /api/v1/recommendations/{id}/events` — lifecycle audit trail.

---

## 7. Frontend

`frontend/src/routes/recommendations/+page.svelte`:

- **Create PR** button on active CPU/memory rightsizing cards that have a workload owner.
- Preview modal: dry-run diff, base branch input, confirm, PR URL on success, actionable error on failure.
- Applied cards show the verification state (`applied`, `verifying`, `verified`, `rollback_review`), measured vs projected savings and a lifecycle event trail.
- Realized savings split into measured (verified) and projected (prorated).

---

## 8. Helm Chart

- `config.recommendations.git.*` values wired into the ConfigMap.
- `secrets.gitToken` (or `secrets.existingSecret`) wired into the Secret.
- ClusterRole grants read access to `apps` workloads (apply detection), plus VPA and Karpenter CRDs.

---

## 9. Testing

- Unit: `SourceResolver` (fake reader), `RightsizingPatcher` (golden YAML), GitHub/GitLab/Gitea providers (respx-mocked HTTP), `AutomationService` orchestration (fake provider + real SQLite repositories).
- Storage: `PullRequestRepository` SQLite tests and PR table migration tests.
- API: `apply-pr` dry run, pull-request listing, automation status and events.
- Frontend build (`npm run build`) is part of verification.

---

## 10. Risks / Notes / Future Work

### Durable execution safety

Non-preview `apply-pr` requests are persisted before any provider mutation and return `202 Accepted`. The idempotency key is unique and its fingerprint binds the recommendation, patch and request; reusing a key with different input is rejected. Workers claim operations transactionally and use a deterministic branch per recommendation. A retry reuses an unfinished pull-request attempt and provider transient failures are requeued with exponential backoff (up to five attempts); permanent failures are terminal. Stale worker leases are reconciled before claiming work. Operators can inspect operation status, attempts, digests and the last error through `/automation/operations/{id}`.

- Provider webhooks/polling for merge detection and rollback PRs remain deferred. Merge/apply detection uses the Kubernetes API, and post-apply verification is handled by the recommendation lifecycle.
- Keep `Idempotency-Key` stable across client/network retries. Do not retry a request with a changed recommendation or patch under the same key.

- **YAML round-trip fidelity** — solved by `ruamel.yaml`; covered by golden tests.
- **Helm/Kustomize/values-based workloads** — v1 targets raw manifests; Helm values patching (via annotation JSON path) remains a follow-up.
- **Multi-container** — the patcher applies the new request to all containers by default; per-container targeting is a follow-up.
- **Merge detection** — Kubernetes API detection; provider webhooks/polling are explicitly deferred.
- **Non-rightsizing types** — zombie deletion / HPA addition / off-peak cron are follow-ups using the same patcher interface.
- **Security** — the token stays in the secret mechanism and is never logged; PRs are only created from explicit user action (no autonomous mutation).
