# Web UI

The GreenKube UI is a SvelteKit single-page application served by the API at `/` (see [Architecture](architecture.md#sveltekit-dashboard)). Every page reads the API with your session; no page starts a scan or writes anything on load. Scans run on the controller schedule.

Status is always written as a word (`Measured`, `Projected`, `Action needed`, `Failing`…), never shown by colour alone. Pages fit phone widths with a 16 px gutter; wide tables scroll horizontally inside their card.

## Pages

| Route | Purpose | Reads |
|---|---|---|
| `/` **Overview** | Savings first: measured allocated savings (with the realization factor), savings pending verification, projected opportunity, changes in flight (linked to the changes board), the top 5 recommendations in server order, and a link to the footprint | `GET /recommendations/savings`, `GET /recommendations/active`, `GET /automation/pull-requests`, `GET /api/v2/changes` |
| `/recommendations` | The ranked list, filters and plan preview; each card links to its detail page. The **Pull requests** tab is a link to the changes board | `GET /recommendations/active` and related |
| `/recommendations/{id}` | One recommendation: proposal, expected value, delivery eligibility with blockers and fixes, projected impact by component, consensus across sources (every vote, agreement, contested reasons), source badges, revision and queued newer advice, evidence, pull requests, measurement and lifecycle timeline | `GET /recommendations/{id}`, `/events`, `/pull-requests`, `GET /api/v2/recommendations/{id}` |
| `/changes` | The changes board: one pipeline from approved plan to measured outcome (Plan → PR open → Merged → Synced → Applied → Measuring → Outcome), with filters | `GET /api/v2/changes`, `GET /automation/pull-requests`, `GET /api/v2/recommendations?status=pr_open`, `GET /recommendations/{id}/pull-requests`, `GET /automation/plans/{id}` |
| `/changes/{plan_id}` | One plan: its files, pull request and sync state, the applied change and its latest measurement | `GET /automation/plans/{id}`, `GET /api/v2/changes?plan_id=`, `/measurements` |
| `/ledger` | Savings totals per method and the applied changes, filterable by measurement state; `?change=` opens one change (a change id, or a plan id filtered on the server) | `GET /recommendations/savings`, `GET /api/v2/changes` (`?plan_id=` for a plan) |
| `/sources` | Every recommendation source: status, reason, last run, counts, measured track record, registry problems and recent runs on demand | `GET /sources`, `GET /sources/{id}/runs`, `GET /sources/track-record` |
| `/setup` | The setup checklist, capability states and repository suggestions | `GET /setup/status`, `GET /automation/repositories/suggestions`, `POST /automation/repositories/allowlist` |
| `/footprint` | Footprint KPIs and charts of the selected period (CO₂e, cost, energy, pods), with **Nodes** (`/footprint/nodes`) and **Report** (`/footprint/report`) sections | dashboard and metrics endpoints, `GET /nodes`, report endpoints |
| `/automation` | GitOps discovery (one card per controller family), workload binding coverage and manual resolution, plan automation readiness; pull requests are on the changes board | `GET /gitops/status`, `/gitops/coverage`, `/gitops/bindings`, `GET /automation/status` |
| `/settings` | Configuration, account and API keys | unchanged |

API paths are under `/api/v1` unless they say `/api/v2`.

The old `/report` and `/nodes` routes redirect to `/footprint/report` and `/footprint/nodes` (query string kept), so bookmarks keep working. The footprint is supporting context; the bundled Grafana dashboard stays the deep-analysis surface (see [Prometheus and Grafana](prometheus-grafana.md)).

## Changes board

`/changes` follows every change GreenKube delivered in one pipeline, newest first in each column:

| Column | A row is here when |
|---|---|
| **Plan** | The plan is approved (or being applied) and its pull request is not open yet. |
| **PR open** | The pull request is open, waiting for review and merge. |
| **Merged** | The pull request is merged and the GitOps controller has not synced it yet. A failed sync stays here with **Sync failed** and its reason. |
| **Synced** | The controller synced the merge; the workload has not rolled out yet. |
| **Applied** | The change rolled out; measurement starts after the warm-up. |
| **Measuring** | The measurement window is open. |
| **Outcome** | Saving measured, no saving measured, inconclusive, contaminated, regressed (rollback review) or closed; plans that ended without a change (closed without merge, superseded, rejected) too. |

Once an applied change exists, its measurement state decides the column; before that, the plan status (`GET /automation/plans/{id}`) decides, and the pull-request record when the plan is no longer stored. Merged pull requests leave the open list, so the board finds them through the recommendations still in delivery (`status=pr_open`). Per-row lookups are capped at 25 per load; the page says how many it skipped.

Each row names its workload and namespace, shows its status in words, flags what needs a human (**Sync failed**, **The controller synced another revision**, **Pull request failed**, **Regressed: review the rollback**) and links to the change page (`/changes/{plan_id}`), the pull request, its recommendations (`/recommendations/{id}`) and, once applied, its ledger entry (`/ledger?change=`). Filters (stage, namespace, needs attention, free text over workload, repository, plan, change and `#recommendation`) are kept in the URL, for example `/changes?stage=measuring&namespace=shop`. **Load older changes** pages through `GET /api/v2/changes`. The board replaces the pull-request list of the Automation page; it only reads.

## Projected, allocated and realized are never summed

Principle 3 of the [roadmap](roadmap.md): projected ≠ allocated ≠ realized. The Overview, the Ledger, the
Recommendations savings tab and the Changes board use one vocabulary (`frontend/src/lib/savingsVocabulary.js`) and show
each kind of saving in its own panel, with its method as a label:

- **Measured allocated savings** (`Measured`): what the changed workloads stop being charged, measured after the change and accrued in the savings ledger to date (**Allocated CO₂e** for carbon), with the **measured allocated run rate** of verified changes beside them. The cluster bill drops only when capacity is removed, so these are not called realized.
- **Realization factor**: the expected share of an allocated saving that reaches the bill (`realization.realization_factor` of the savings report), with the billed saving it implies, labeled as an expectation.
- **Realized (billed) savings**: shown only when the server reports savings measured on the bill (`realized_billed`). The word "Realized" is reserved for them; a measurement outcome reads **Saving measured** or **No saving measured**.
- **Pending verification** (`Projected`): the yearly projection of applied changes that have not been measured yet.
- **Opportunity** (`Projected`): the projected yearly savings of open recommendations and their risk-adjusted expected value. Reliability recommendations add resources on purpose and are counted apart, never as savings.
- **User declared** (Ledger only): savings declared on manual applies, never counted as measured.

No page adds these figures together. The deprecated flat `total_*` fields of the savings report are not read.

Amounts use the pricing currency (`impact.pricing.currency`, served as `currency` by `GET /api/v1/config`), read once
after sign-in; a value that carries its own currency (a recommendation's `value.currency`) keeps it.

When nothing is measured yet, the measured panel explains that a merged change is measured after its warm-up and measurement window. When every open recommendation is still collecting evidence, the Overview shows when the first one becomes eligible for a pull request.

## Roles in the UI

Pages show only the actions the signed-in principal may perform, so a viewer never meets a button that ends in
`403`. Mutations need the `write` scope: **Open PR**, **Ignore**, **Restore** and **Rescan** on the recommendations
page, **Refresh discovery** and **Resolve binding** on the Automation page (a viewer gets **Reload**), **Mark as
applied** in the manual panel, and the footprint cache refresh (a viewer only reloads). Approving a plan needs the
`approve` scope, and a plan that answers `may_approve: false` is not offered for approval. A short **Read-only access**
note says why the actions are missing. The server enforces the same scopes ([Authentication](authentication.md)).

## Pull-request deep links

With `config.externalUrl` set (see [Automation](automation.md#pull-request-content)), pull requests link back to the UI:

| Link | Opens |
|---|---|
| `/recommendations/{id}` | The recommendation detail page. An unknown or pruned id shows "Recommendation not found". |
| `/changes/{plan_id}` | The plan, its pull request and the applied change. A plan removed by retention still shows its applied change; a plan not applied yet says so. |
| `/ledger?change={change_id or plan_id}` | The change's measurement, its confidence interval, the original projection for comparison and the split between its recommendations. A plan id is matched against the 1,000 most recent changes. |

## Recommendation detail

- **Delivery** shows the server-side eligibility: whether the recommendation can become a pull request, each blocker with its code, message and fix, warnings, and how long until the evidence warm-up ends.
- **Projected impact** shows the Impact Model projection: allocated and expected billable cost, the realization factor, expected CO₂e (or "Not modeled"), the cost and CO₂e components (operational energy, idle share, embodied) and the model assumptions.
- **Sources and consensus** appears only for recommendations built by the consensus engine (Recommendation v2). It shows the policy, the chosen value, the sources that support it, each source's proposal, which signals were counted (two sources of the same engine count as one vote, a push client counts once), why a consensus is contested and the revision history. It reads `GET /api/v2/recommendations/{id}` and falls back to the v1 record on a server without it. The header shows the current revision, a **Newer advice: revision N** badge with its values when advice is queued behind an open plan (E7), and one badge per source id behind the recommendation (`goldilocks`, `krr`, `push:<client>`...). A recommendation without a consensus block hides the section.
- **Measurement** shows measured savings apart from the projection, with links to the plan's change and ledger entries.
- **Apply manually** (active recommendations, roadmap H9) loads on demand the `kubectl` steps of `GET /api/v1/recommendations/{id}/manual` (copy buttons, the diff and notes) and offers **Mark as applied** after a confirmation; the change is then measured like a merged pull request. The **Apply manually** link of each active card on the recommendations page opens this panel (`/recommendations/{id}#manual`).

## Setup checklist and banner

`/setup` renders `GET /api/v1/setup/status`: the four capabilities (discovery, delivery, sync tracking, measurement) with their state and reason, and the checks grouped by category (data sources, recommendation sources, GitOps controllers, repositories, Git credentials, renderer, plan executor, evidence warm-up). A check that is not `ok` or `not_applicable` shows its fix: a sentence, a copyable Helm or kubectl snippet, and a link to the page that performs a UI action (`allowlist_repositories` opens the repository suggestions, `open_sources` the Sources page). Capability reasons read as text (for example `credentials_host_ambiguous`: "The Git token is not bound to a host"). A capability is active only when the server says `active`; delivery paused by `automation.policy.pauseDelivery` reads **paused**, with a notice that discovery, previews, tracking and measurement keep running, and the banner says so. Documentation links open only `https://` or in-app URLs. An unknown check status reads as `Pending`, never as `OK`.

Until the server reports the setup as complete, a banner on every page counts the checks that need attention and links the checklist. **Dismiss** hides it for the browser tab session. A server without the setup endpoint shows no banner.

**Repositories found by discovery** lists the repositories that discovery inferred, with their provider and where they were found. An administrator selects them and clicks **Allow these N repositories** (`POST /api/v1/automation/repositories/allowlist`). The server allows them in its database and answers with the equivalent `automation.repositoryAllowlist` values snippet (`values_snippet`), which the page shows to copy and commit on GitOps-managed installs.

The UI tests run on JSON the FastAPI app generates (`frontend/tests/fixtures/api`, written by `tests/api/test_ui_contract_fixtures.py` with `GREENKUBE_UPDATE_SNAPSHOTS=1`; it covers the setup status, repository suggestions, recommendation v2 detail, `GET /sources` and `GET /sources/{id}/runs`): a response change fails the backend test until the fixtures, and so the UI tests, follow it.

## Sources

`/sources` lists every registered source, problem sources first (`Failing`, `Misconfigured`, then `Degraded`), with its origin, tier, version, enablement, capabilities, status reason, last run, last success, signal and recommendation counts, and its measured track record (for example "14/16 realized"). **Show recent runs** loads the last 20 runs with status, duration, signal count, error code and diagnostics. Registry problems (a plugin that failed to load, invalid settings) are listed at the top. See [Recommendation sources](sources.md).
