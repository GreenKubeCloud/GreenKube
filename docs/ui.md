# Web UI

The GreenKube UI is a SvelteKit single-page application served by the API at `/` (see [Architecture](architecture.md#sveltekit-dashboard)). Every page reads the API with your session; no page starts a scan or writes anything on load. Scans run on the controller schedule.

Status is always written as a word (`Measured`, `Projected`, `Action needed`, `Failing`…), never shown by colour alone. Pages fit phone widths with a 16 px gutter; wide tables scroll horizontally inside their card.

## Pages

| Route | Purpose | Reads |
|---|---|---|
| `/` **Overview** | Savings first: realized savings, savings pending verification, projected opportunity, changes in flight, the top 5 recommendations in server order, then the footprint charts | `GET /recommendations/savings`, `GET /recommendations/active`, `GET /automation/pull-requests`, `GET /api/v2/changes`, footprint endpoints |
| `/recommendations` | The ranked list, filters, plan preview and pull requests; each card links to its detail page | `GET /recommendations/active` and related |
| `/recommendations/{id}` | One recommendation: proposal, expected value, delivery eligibility with blockers and fixes, projected impact by component, consensus across sources (every vote, agreement, contested reasons), source badges, revision and queued newer advice, evidence, pull requests, measurement and lifecycle timeline | `GET /recommendations/{id}`, `/events`, `/pull-requests`, `GET /api/v2/recommendations/{id}` |
| `/changes/{plan_id}` | One plan: its files, pull request and sync state, the applied change and its latest measurement | `GET /automation/plans/{id}`, `GET /api/v2/changes?plan_id=`, `/measurements` |
| `/ledger` | Savings totals per method and the applied changes, filterable by measurement state; `?change=` opens one change (a change id, or a plan id filtered on the server) | `GET /recommendations/savings`, `GET /api/v2/changes` (`?plan_id=` for a plan) |
| `/sources` | Every recommendation source: status, reason, last run, counts, measured track record, registry problems and recent runs on demand | `GET /sources`, `GET /sources/{id}/runs`, `GET /sources/track-record` |
| `/setup` | The setup checklist, capability states and repository suggestions | `GET /setup/status`, `GET /automation/repositories/suggestions`, `POST /automation/repositories/allowlist` |
| `/automation`, `/report`, `/nodes`, `/settings` | GitOps console, report builder, node inventory, configuration, account and API keys | unchanged |

API paths are under `/api/v1` unless they say `/api/v2`.

## Projected, measured and realized are never summed

The Overview and the Ledger show each kind of saving in its own panel, with its method as a label:

- **Realized savings** (`Measured`): savings accrued in the savings ledger to date, with the measured yearly run rate of verified changes beside them.
- **Pending verification** (`Projected`): the yearly projection of applied changes that have not been measured yet.
- **Opportunity** (`Projected`): the projected yearly savings of open recommendations and their risk-adjusted expected value. Reliability recommendations add resources on purpose and are counted apart, never as savings.
- **User declared** (Ledger only): savings declared on manual applies, never counted as measured.

No page adds these figures together. The deprecated flat `total_*` fields of the savings report are not read.

When nothing is measured yet, the realized panel explains that a merged change is measured after its warm-up and measurement window. When every open recommendation is still collecting evidence, the Overview shows when the first one becomes eligible for a pull request.

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

The UI tests run on JSON the FastAPI app generates (`frontend/tests/fixtures/api`, written by `tests/api/test_ui_contract_fixtures.py` with `GREENKUBE_UPDATE_SNAPSHOTS=1`): a response change fails the backend test until the fixtures, and so the UI tests, follow it.

## Sources

`/sources` lists every registered source, problem sources first (`Failing`, `Misconfigured`, then `Degraded`), with its origin, tier, version, enablement, capabilities, status reason, last run, last success, signal and recommendation counts, and its measured track record (for example "14/16 realized"). **Show recent runs** loads the last 20 runs with status, duration, signal count, error code and diagnostics. Registry problems (a plugin that failed to load, invalid settings) are listed at the top. See [Recommendation sources](sources.md).
