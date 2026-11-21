### Added
- `GET /api/v1/recommendations/savings` returns `opportunity`: the projected opportunity of the active recommendations, de-duplicated across overlapping recommendations (namespace deletion and the rightsizing of its workloads, off-peak schedule and rightsizing of one workload, node removal and its pods), with request increases reported apart in `reliability_cost_annual`. The dashboard and the recommendations page show this one figure, labelled "Projected (not additive across overlapping recommendations)" (IMP-07).
- `interim_run_rate_annual` and `interim_realized_to_date` in the savings report: savings of recommendations decided by the interim verifier (`CHANGE_MEASUREMENT_ENABLED=false`), reported apart (MEA-16).
- `value_basis` on every annual block of the savings report (`allocated`, `billable_expected`, `declared` or `mixed`, IMP-06).
- Projections record `coverage_span_hours`, `collected_hours` and `collection_gap_hours`, and lose confidence when less than 90% of their coverage was collected (IMP-04).
- The recommendations page shows a verified reliability fix as "Fix confirmed, costs +X/yr" and the reliability fixes' cost to date on the savings tab (MEA-14).

### Changed
- Change measurement `request_rightsizing` 2.6, deletion 1.2, interim `c0.3`: hour buckets are rated per collected time of the namespace, so a workload present part of each hour (batch, scale-to-zero) counts its absent steps as zero cost instead of being extrapolated to the full hour, and a collected hour without the workload is a zero bucket. Every annualisation uses 8760 hours, the Impact Model constant (was 8766). Decided measurements keep their values (MEA-13).
- Impact Model 4.0.0 (unreleased, same version): projections are annualized over the hours the collector observed the cluster since the target's first sample, not over the wall-clock span from its first rolled hour, and the minimum coverage is half an hour. Projections of targets whose coverage had collection gaps, and of young targets, are higher (up to 2x after a 50% collection gap). Recommendations are re-projected at the next scan (IMP-04).
- Ledger writer `ledger/3`: a rightsizing applied more conservatively than recommended accrues the realized share of its allocated projection (IMP-06).

### Fixed
- Interim-verifier savings are no longer summed with change-measured savings in `run_rate_annual`, `realized_to_date`, `verified_count` and the realization summary (MEA-16).
- A user-declared saving on `PATCH /apply` is bounded by the allocated projection, so a rightsizing on fixed nodes (billable projection 0) accepts a declaration (IMP-06).
- The recommendations page no longer sums overlapping recommendations and nets request increases into its "Potential savings" totals (IMP-07).
