# Development Plan: "Apply Recommendation" → Automated PR

## 1. Goal & Scope

Turn GreenKube recommendations from passive suggestions into actionable fixes. A user clicks **"Apply"** on a recommendation, and GreenKube opens a pull request against the workload's Git repository that implements the change.

**Decided scope:**

- Source discovery via **workload annotations** (primary).
- Git platforms: **GitHub** and **GitLab**, both SaaS and self-hosted.
- Recommendation types: **CPU/memory rightsizing only** (v1).
- Bot runs **in-process** inside the existing FastAPI app.
- `ruamel.yaml` as a **production dependency** (preserves comments/formatting).
- A dedicated **`POST /api/v1/recommendations/{id}/apply-pr`** endpoint (distinct from the existing "record applied" endpoint).

The existing `PATCH /recommendations/{id}/apply` only *records* that a change was made (it does not touch Git). This feature is additive and does not replace it.

---

## 2. Architecture Overview

Three pluggable layers, all in-process, following the existing clean/hexagonal conventions:

```
Frontend "Apply" button
        │ POST /api/v1/recommendations/{id}/apply-pr
        ▼
AutomationService (orchestrator)                src/greenkube/automation/service.py
  │ 1. Load RecommendationRecord (needs owner_kind + namespace + name)
  │ 2. SourceResolver → Source{repo_url, path?, branch?}   (reads workload annotations via K8s)
  │ 3. GitProvider.read_file(repo, path, ref)             (GitHub / GitLab adapters)
  │ 4. ManifestPatcher.build_patch(rec, content)          (rightsizing edits)
  │ 5. GitProvider.create_pr(branch, commit, title, body)
  │ 6. PullRequestRepository.record_pr(rec.id, pr info)
  ▼
PR URL returned + persisted; user reviews & merges
```

Key principle: **no knowledge of ArgoCD/Flux anywhere**. The bot only needs (a) a way to find the file (annotations), (b) a way to talk to the Git platform (adapters), (c) a way to edit YAML (patchers).

---

## 3. Persistence Changes

### 3.1 New column on `recommendation_history`: `owner_kind`

Currently the recommender groups by `(namespace, owner_kind, owner_name)` but the `Recommendation` DTO drops `owner_kind` (it only stores `pod_name = target_name`, `scope`). The bot must know the Kubernetes **kind** (Deployment/StatefulSet/CronJob) to locate and patch the right manifest.

- Add `owner_kind: Optional[str]` to `Recommendation` and `RecommendationRecord` in `src/greenkube/models/metrics.py`.
- Populate it in `src/greenkube/core/recommender.py` (every `Recommendation(...)` already has `target_kind` in scope; pass it through).
- Migration `0010_*` for both `postgres/` and `sqlite/` (see `0006`/`0007` as templates):

  ```sql
  ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS owner_kind TEXT;
  ```

- Update `_row_to_record`, `INSERT`/`UPDATE` statements in both `sqlite/recommendation_repository.py` and `postgres/recommendation_repository.py`.

### 3.2 New table `recommendation_pull_requests`

Tracks PR lifecycle independently of recommendation status (a recommendation can have multiple attempts; the recommendation stays `active` until actually merged/applied).

| column | type |
|---|---|
| id | PK auto |
| recommendation_id | FK → recommendation_history.id |
| provider | text (`github` / `gitlab`) |
| repo | text |
| base_branch | text |
| head_branch | text |
| pr_number | integer (nullable) |
| pr_url | text |
| status | text (`pending` / `open` / `merged` / `closed` / `error`) |
| error | text (nullable) |
| created_at / updated_at | timestamptz |

- New `PullRequestRepository` abstract + SQLite/Postgres implementations, registered in `core/factory.py` + `api/dependencies.py`.
- Migration `0010_*` creates the table.

---

## 4. Configuration

Add to `src/greenkube/core/config.py` (and `helm-chart/values.yaml` + configmap/secret templates):

**Secrets (read via `/etc/greenkube/secrets/` or env):**

- `GIT_TOKEN` — PAT (GitHub) or personal access token (GitLab). Required to push/open PRs.

**Config:**

- `GIT_PROVIDER` — `github` | `gitlab` (default `github`)
- `GIT_API_BASE_URL` — override for GitHub Enterprise Server / self-hosted GitLab (e.g. `https://git.example.com`)
- `GIT_DEFAULT_BRANCH` — fallback base branch when the workload annotation omits one (default `main`)
- `GIT_COMMIT_AUTHOR_NAME` / `GIT_COMMIT_AUTHOR_EMAIL` — identity for bot commits (e.g. `GreenKube Bot` / `bot@greenkube.cloud`)

Helm: add `config.git.*` and `secrets.gitToken` (mirroring the existing `secrets.electricityMapsToken` pattern).

---

## 5. Backend Components (new files)

### 5.1 `src/greenkube/automation/source_resolver.py`

- `SourceResolver` with a single `AnnotationSourceResolver` (v1), returning `Source(repo_url, path=None, branch=None)`.
- Reads the workload object (kind from `owner_kind`, name from `pod_name`, namespace) via `get_core_v1_api()` (or AppsV1 for Deployments).
- Annotation contract (documented in `docs/`):
  - `greenkube.cloud/git-repo` (required) — repo URL
  - `greenkube.cloud/git-path` (optional) — file path; if omitted, the bot lists the repo and searches for a manifest with matching `kind` + `metadata.name`
  - `greenkube.cloud/git-branch` (optional) — base branch override
- Graceful failure with a clear error message surfaced to the UI when annotations are missing.

### 5.2 `src/greenkube/automation/git/`

- `base.py` — `GitProvider` ABC: `get_default_branch`, `get_file`, `create_branch`, `update_file`, `create_pull_request`, `get_pr_url`, `test_connection`.
- `github.py` — GitHub REST API via `httpx.AsyncClient`; supports `GIT_API_BASE_URL` override for Enterprise Server.
- `gitlab.py` — GitLab REST API; supports `GIT_API_BASE_URL` for self-hosted.
- Both use `GIT_TOKEN` bearer auth; factory `get_git_provider()` selects based on `GIT_PROVIDER`.

### 5.3 `src/greenkube/automation/manifests/`

- `patcher.py` — `RightsizingPatcher.build_patch(rec, content, kind, name)`:
  - Parse YAML (multi-doc), find doc with `kind == owner_kind` and `metadata.name == target name`.
  - Set `spec.template.spec.containers[*].resources.requests.cpu` = `f"{recommended_cpu}m"` and `memory` = humanized bytes (e.g. `512Mi`), creating `resources`/`requests` maps when absent.
  - CPU and memory handled independently (a rec may set only one).
  - Uses `ruamel.yaml` to preserve comments/formatting (promoted to production dependencies).
- `helm_values.py` (Phase 2) — patch `resources.requests.*` in `values.yaml` via an annotation-provided JSON path.

### 5.4 `src/greenkube/automation/service.py`

- `AutomationService.apply_rightsizing(recommendation_id)` orchestrates steps 1–6 above.
- Produces a conventional commit message + PR title/body (include before→after request values and projected savings).
- Wraps everything in try/except; on error, records `status='error'` + message and returns it to the API.

### 5.5 `src/greenkube/storage/` (new `pull_request_repository.py` files + abstract)

- SQLite + Postgres `PullRequestRepository` implementations, mirroring the existing recommendation repository patterns.

---

## 6. API

In `src/greenkube/api/routers/recommendations.py`:

- `POST /api/v1/recommendations/{id}/apply-pr`
  - Body: optional `{ base_branch?, dry_run? }`.
  - Validates the recommendation is a rightsizing type, resolves source, generates the patch.
  - Runs the PR creation as a background task (`asyncio.create_task`, matching the existing startup-scan pattern) and returns `202` with an initial status.
  - `dry_run=true` returns the computed diff (path + proposed content) without touching Git — useful for preview in the UI.
- `GET /api/v1/recommendations/{id}/pull-requests` — list PR attempts/status for a recommendation.
- `GET /api/v1/automation/status` (optional) — global feature readiness (git token configured? provider?).

Register a new router or extend the recommendations router. Add `PullRequestRepository` dependency in `api/dependencies.py` and factory in `core/factory.py`.

---

## 7. Frontend (`frontend/src/`)

- `src/lib/api.js`: add `applyRecommendationPr(id, body)` and `getRecommendationPullRequests(id)` helpers (the client already has `applyRecommendation` for the old "mark applied" flow).
- `src/routes/recommendations/+page.svelte`:
  - Add an **"Apply"** button (visible only for `RIGHTSIZING_CPU` / `RIGHTSIZING_MEMORY` cards in the Active tab).
  - Clicking opens a modal: preview (dry-run diff), confirm base branch, then submit.
  - On success, show the PR URL + status; poll for updates or refresh on next load.
  - If source discovery fails (missing annotations), show the specific, actionable error.
- Match existing Tailwind/button patterns (`btn-primary`, `card`, existing Ignore modal).

---

## 8. Helm Chart

- `helm-chart/values.yaml`: `config.git.*` and `secrets.gitToken`.
- `helm-chart/templates/configmap.yaml` / `secret.yaml`: wire the new env vars/secrets (follow existing patterns).
- Document the annotation convention in `docs/automation.md`.

---

## 9. Testing

Follow existing TDD + `pytest-asyncio` + `respx` patterns:

- Unit: `SourceResolver` (mock K8s), `RightsizingPatcher` (golden YAML fixtures), GitHub/GitLab providers (respx-mocked HTTP), `AutomationService` orchestration (mocks).
- Integration: new `PullRequestRepository` against SQLite + Postgres (mirroring `test_recommendation_lifecycle_e2e.py`).
- API: full request/response cycle for `apply-pr` incl. `dry_run`.
- Frontend: Svelte component test for the Apply button/modal.

---

## 10. Implementation Phases

1. **Data model** — add `owner_kind` to models + repositories + migration `0010` (both backends); populate from recommender. Update `RecommendationRecord.from_recommendation`.
2. **PR persistence** — `PullRequestRepository` abstract + SQLite/Postgres + factory/deps + migration.
3. **Config & Helm** — new `GIT_*` settings and secrets.
4. **Git providers** — `GitProvider` ABC + GitHub + GitLab + factory.
5. **Source resolver + patcher** — annotation resolver + rightsizing YAML patcher (+ `ruamel.yaml` to prod deps).
6. **Orchestration + API** — `AutomationService` + `apply-pr`/`pull-requests` endpoints.
7. **Frontend** — Apply button, preview modal, PR status.
8. **Tests + docs** — unit/integration/e2e + `docs/automation.md`.

---

## 11. Risks / Notes / Future Work

- **YAML round-trip fidelity** — solved by `ruamel.yaml` (promote to prod dep).
- **Helm/Kustomize/values-based workloads** — v1 targets raw manifests; Helm values patching (via annotation JSON path) is the natural Phase 2.
- **Multi-container** — patcher applies the new request to all containers by default; can be refined later to target a specific container via annotation.
- **Merge detection** — v1 stops at "PR created"; marking the recommendation `applied` on merge can come later via GitHub/GitLab webhooks or a manual "Mark merged" action. The existing `apply` endpoint already handles realized-savings once merged.
- **Non-rightsizing types** — zombie deletion / HPA addition / off-peak cron are follow-ups using the same `ManifestPatcher` interface.
- **Security** — token stays in the existing secret mechanism, never logged; PRs only created from explicit user action (no autonomous mutation).
