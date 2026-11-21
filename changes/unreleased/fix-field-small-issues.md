### Added
- `python scripts/sync_version.py --check` fails when the API version (shown by the UI banner), the chart `version`/`appVersion`, the chart image tag or the frontend package diverge from `pyproject.toml`, the single source of truth; `tests/scripts/test_sync_version.py` runs it (F-10).
- Plan status `cancelled` and plan field `status_reason` (reason code of the last status change) in the API and storage. Migration `0220` adds the column and the status to both dialects; SQLite rebuilds `automation_plans` with every row and index kept. Existing plans keep their status and an empty `status_reason` (F-10).

### Fixed
- No false `All Prometheus query endpoints failed` ERROR at controller start: without `PROMETHEUS_URL`, discovery now runs before the first instant query, as for range queries, and relative URLs are never queried (F-06).
- `POST /automation/plans/{id}/cancel` ends the plan in `cancelled` with `status_reason` `cancelled_by_user`, visible on the plan's GET, instead of `stale` with the reason lost (F-10).
- The install NOTES list the Secrets of the providers in `automation.providers` instead of suggesting the default `greenkube-git-credentials` Secret when providers are configured (F-10).
- The initial pass of a controller loop run by the startup work (`metrics-compression`, `carbon-intensity`, `node-analysis`, `savings-attribution`, `recommendation-lifecycle`, `dashboard-summary`) now counts as a run of that loop, so the loop status no longer shows `runs: 0` after it (F-10). These passes now go through the loop registry, with its timeout and failure accounting.
- `cloudProvider: on-premise` (also `on-prem`, `on-premises`, `onprem`, `on_prem`) maps explicitly to `config.defaults.pue` with an INFO line, instead of warning about an unknown provider (F-10).
- A configured `defaultZone` used for nodes without a mappable zone is logged once per node and process at INFO (WARNING once per node when `defaultZone` is unset), then at DEBUG, instead of a WARNING per node at every collection. Each row still records the fallback in its estimation reasons (F-10).
