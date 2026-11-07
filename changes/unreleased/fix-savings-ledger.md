### Added
- **Idempotent savings accrual (C0.7, BUG-33):** savings accrue once per recommendation, method and UTC hour slot. A new `accrual_watermarks` table records the last accrued slot per cluster and is advanced by compare-and-swap in the same transaction as the inserts, so restarts, retries and a second controller cannot double count. Slots missed during downtime are backfilled (7 days on the first run after upgrade, at most 168 slots per run). The slot in which a change was applied is accrued pro rata.
- **Read-side ledger views (C0.6):** `savings_ledger_entries` (raw accruals plus legacy compressed rows) and `savings_ledger_hourly_rollup` replace the destructive hourly compression.
- **Savings ledger counters (C0.14):** `greenkube_co2e_savings_reversed_grams_total` and `greenkube_cost_savings_reversed_dollars_total` report superseded accruals and negative declarations; `greenkube_savings_run_rate_annual_co2e_grams` / `..._cost_dollars` expose current annual rates by method; `greenkube_dashboard_savings_accrued_co2e_grams` / `..._cost_dollars` expose window accruals by method.
- **`greenkube_attribution_errors_total{reason}` (C0.8, BUG-34):** counts recommendations skipped by attribution and accrual batches that could not be stored.

### Changed
- **`GET /recommendations/savings` (C0.14, BUG-40):** returns separate, never-summed blocks: `projected_annual`, `run_rate_annual`, `realized_to_date`, plus `projected_to_date`, `user_declared_annual` and `user_declared_to_date`, with `diagnostics`. The flat `total_*`, `measured_*` and `prorated_*` fields remain as deprecated aliases; `total_*` now equals realized (measured) savings only.
- **Ledger method `prorated` is now `projected` (C0.4):** migration 0043 relabels existing rows, and projected accruals are excluded from every realized figure (API, gauges).
- **`greenkube_co2e_savings_attributed_grams_total` and `greenkube_cost_savings_attributed_dollars_total` (C0.14):** now Prometheus counters with a `method` label; they never decrease. `greenkube_dashboard_savings_*_total` now carry realized (measured) savings only, and `greenkube_cluster_*_saved_*_total` only count recommendations still accruing as projected.
- **User-declared savings on `PATCH /recommendations/{id}/apply` (C0.8):** values must be finite and at most 10× the projection (`422` otherwise); negative values are accepted and flagged. They are stored under `baseline.user_declared_savings` and accrue with method `user_declared`, never as measured.
- **PostgreSQL ledger values** are stored as `DOUBLE PRECISION` instead of `REAL`.

### Fixed
- **Failed outcomes kept accruing savings (C0.4, BUG-30):** recommendations that were not realized, are inconclusive, failed, are in rollback review or were reverted no longer accrue anything.
- **Ledger compression lost about half of the savings (C0.6, BUG-32):** ledger rows are no longer compressed or deleted. Hourly rows left by earlier releases are kept, flagged `legacy_compressed`, and reported in a `legacy_compressed` diagnostic.
- **Double counting on restart and failover (C0.7, BUG-33):** accrual no longer uses wall-clock periods.
- **One invalid record stopped all attribution (C0.8, BUG-34):** each recommendation is attributed in isolation and signed values are accepted.
- **Savings windows dropped their partial first hour (C0.14, BUG-40):** rows that overlap a window boundary contribute their overlapping share.
- **Silent fallback when the ledger is unreadable (C0.14, BUG-42):** `GET /recommendations/savings` now answers `503`.
