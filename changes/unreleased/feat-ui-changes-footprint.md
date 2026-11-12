### Added

- **Changes board (K4):** `/changes` follows every delivered change in one pipeline — Plan → PR open → Merged → Synced → Applied → Measuring → Outcome (realized, not realized, inconclusive, regressed for rollback review, closed). Rows merge `GET /api/v2/changes`, the open pull requests, the plans API (`GET /automation/plans/{id}`) and, for merged pull requests that left the open list, the pull-request records of recommendations still in delivery (`GET /api/v2/recommendations?status=pr_open`). Each row shows its status in words, flags what needs a human (failed sync, revision mismatch, failed pull request, regression) and links to `/changes/{plan_id}`, the pull request, `/recommendations/{id}` and `/ledger?change=`. Filters (stage, namespace, needs attention, free text) live in the URL; older changes page with the API cursor.
- **Footprint section (K7):** `/footprint` holds the footprint KPIs and charts, with `/footprint/nodes` and `/footprint/report` sections behind a labelled section navigation and a pointer to the Grafana dashboard for deep analysis.
- **UI contract fixtures for the Sources page:** `tests/api/test_ui_contract_fixtures.py` now also generates `GET /api/v1/sources` and `GET /api/v1/sources/{id}/runs` into `frontend/tests/fixtures/api` (`sources_list.json`, `source_runs.json`); the Sources page tests run on them.

### Changed

- **Navigation (K4, K7):** the sidebar shows Overview, Recommendations, Changes, Ledger, Footprint, Automation, Sources, Setup and Settings; Report and Nodes moved under Footprint. `/report` and `/nodes` redirect to `/footprint/report` and `/footprint/nodes`, keeping the query string.
- **Overview (K7):** the footprint charts moved to `/footprint`; the Overview links there, its "Changes in flight" strip links to the changes board, and it no longer shows the period selector (only the footprint reads a period).
- **Automation page (K4):** the pending and open pull-request list is gone; a card links to the changes board, which shows the same pull requests with their merge, sync, rollout and measurement state. The page no longer reads `GET /automation/pull-requests`.
- `/changes/{plan_id}` breadcrumbs lead to the changes board.
