### Added

- Scheduled, leader-gated recommendation scans: the controller's `recommendation_scan` job queues a cluster-wide run every `RECOMMENDATION_SCAN_INTERVAL` (default `1h`, Helm `config.recommendations.scanInterval`) and executes queued runs. Queueing and claiming are atomic in the database, so two API replicas plus one controller produce exactly one scan per interval (E1, BUG-21).
- Optimization run API: `POST /api/v1/optimization/runs` queues a scan and returns `202 {"run_id", "status": "queued"}` (deduplicated while a run is pending for the same scope), `GET /api/v1/optimization/runs/{run_id}` returns its status and outcome, and `GET /api/v1/optimization/runs?limit=` lists recent runs (E1, E10).
- Each optimization run persists per-source health (`succeeded`, `incomplete`, `failed`, `unavailable`), recommendation counts and the diff against the previous active set (new, changed, resolved, unchanged), with up to 100 fingerprints per list (E10).
- Migration `0045_optimization_run_queue` turns `optimization_runs` into the run request queue (queued state, trigger, requester, claimer, counts, source outcomes, diff) on SQLite and PostgreSQL; existing rows are kept and tagged `legacy`.

### Changed

- The API no longer runs a recommendation scan at startup (one per replica) and recommendation GET endpoints never run the optimizer or write: `refresh=true` on `/recommendations`, `/recommendations/active` and `/recommendations/top` is deprecated and only queues a run, returning its id in the `X-GreenKube-Run-Id` header (E1, BUG-21).
- `GET /api/v1/recommendations` no longer generates recommendations on an empty first read.
- Each recommendation source runs in isolation with a 30 s probe and 5 min collection bound; a hung connector is recorded as failed instead of stalling the scan (E8).
- A run with some failed sources is now a degraded success (failed sources are listed in `error` and `sources`); it fails only when every attempted source failed or persistence failed. Persistence errors now fail the run instead of being logged as a success.

### Fixed

- Active recommendation lists were sorted `ORDER BY priority DESC` on a text column (medium, low, high). They are now ordered by `ranking_score DESC NULLS LAST, potential_savings_cost DESC NULLS LAST, id` on both backends (E2, BUG-20).
- A single failed source blocked persistence and reconciliation for every source. Other sources now persist normally, and a recommendation becomes `stale` only when every source that supported it completed the scan; failed, unavailable or incomplete sources keep their last known recommendations, and a lower-priority source no longer overwrites the last known advice of an incomplete higher-priority one (E8, BUG-22).
