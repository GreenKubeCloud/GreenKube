# Web UI

The GreenKube UI is a SvelteKit single-page application served by the API at `/` (see [Architecture](architecture.md#sveltekit-dashboard)). Every page reads the API with your session; no page starts a scan or writes anything on load. Scans run on the controller schedule.

Status is always written as a word (`Measured`, `Projected`, `Action needed`, `Failing`…), never shown by colour alone. Pages fit phone widths with a 16 px gutter; wide tables scroll horizontally inside their card.

## Pages

| Route | Purpose | Reads |
|---|---|---|
| `/` **Overview** | Savings first: realized savings, savings pending verification, projected opportunity, changes in flight, the top 5 recommendations in server order, then the footprint charts | `GET /recommendations/savings`, `GET /recommendations/active`, `GET /automation/pull-requests`, `GET /api/v2/changes`, footprint endpoints |
| `/recommendations` | The ranked list, filters, plan preview and pull requests; each card links to its detail page | `GET /recommendations/active` and related |
| `/recommendations/{id}` | One recommendation: proposal, expected value, delivery eligibility with blockers and fixes, projected impact by component, consensus across sources, evidence, pull requests, measurement and lifecycle timeline | `GET /recommendations/{id}`, `/events`, `/pull-requests` |
| `/changes/{plan_id}` | One plan: its files, pull request and sync state, the applied change and its latest measurement | `GET /automation/plans/{id}`, `GET /api/v2/changes?plan_id=`, `/measurements` |
| `/ledger` | Savings totals per method and the applied changes, filterable by measurement state; `?change=` opens one change (a change id, or a plan id filtered on the server) | `GET /recommendations/savings`, `GET /api/v2/changes` (`?plan_id=` for a plan) |
| `/sources` | Every recommendation source: status, reason, last run, counts, measured track record, registry problems and recent runs on demand | `GET /sources`, `GET /sources/{id}/runs`, `GET /sources/track-record` |
| `/setup` | The setup checklist, capability states and repository suggestions | `GET /setup/status`, `GET /automation/repositories/suggestions` |
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
- **Sources and consensus** appears only for recommendations built by the consensus engine (Recommendation v2). It shows the policy, the chosen value, the sources that support it, each source's proposal, which signals were counted (two sources of the same engine count as one vote), and why a consensus is contested. A recommendation without a consensus block hides the section.
- **Measurement** shows measured savings apart from the projection, with links to the plan's change and ledger entries.

## Setup checklist and banner

`/setup` renders `GET /api/v1/setup/status`: the four capabilities (discovery, delivery, sync tracking, measurement) with their state and reason, and the checks grouped by category (data sources, recommendation sources, GitOps controllers, repositories, Git credentials, renderer, plan executor, evidence warm-up). A check that is not `ok` or `not_applicable` shows its fix: a sentence, a copyable Helm or kubectl snippet, and a link to the page that performs a UI action. Documentation links open only `https://` or in-app URLs. An unknown check status reads as `Pending`, never as `OK`.

Until the server reports the setup as complete, a banner on every page counts the checks that need attention and links the checklist. **Dismiss** hides it for the browser tab session. A server without the setup endpoint shows no banner.

**Repositories found by discovery** lists the repositories that discovery inferred, with their provider and where they were found. An administrator selects them and clicks **Allow these N repositories** (`POST /api/v1/automation/repositories/allowlist`). When the server answers with a Helm values snippet (GitOps-managed installs keep the allowlist in values), the page shows it to copy and commit.

## Sources

`/sources` lists every registered source, problem sources first (`Failing`, `Misconfigured`, then `Degraded`), with its origin, tier, version, enablement, capabilities, status reason, last run, last success, signal and recommendation counts, and its measured track record (for example "14/16 realized"). **Show recent runs** loads the last 20 runs with status, duration, signal count, error code and diagnostics. Registry problems (a plugin that failed to load, invalid settings) are listed at the top. See [Recommendation sources](sources.md).
