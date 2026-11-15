### Fixed
- An OpenCost outage, rejected request (HTTP 401, 503), invalid answer or an empty answer after OpenCost priced earlier windows no longer switches the window to a trusted list-price cost silently: the rows keep a modeled cost labelled `opencost_pending` (never trusted), the step records carry `cost_status = opencost_pending`, and the collection job re-prices the window from OpenCost once it answers again (COST-01, RECON-09).
- `OpenCostCollector.collect()` raises `OpenCostUnavailableError` instead of returning an empty list when the configured or discovered OpenCost fails, and reports `last_status` (`ok`, `empty`, `not_configured`).

### Added
- Migration 0155 adds `collection_windows.cost_status`; gauge `greenkube_collection_cost_pending_steps`.
