### Added

- **CO₂e components (B, B4 with C3).** Projections report `co2e_components_annual_g` (`operational_energy`, `idle_share`, `embodied`), `co2e_measurable_components` and `co2e_measurable_expected_annual_g`, the value a measurement over stored rows is compared with. `greenkube.impact.comparison.measurable_co2e_projection` derives it from any stored projection, including those written before Impact Model 2.0.0.
- Migration `0064`: `gpu_power_watts` on `combined_metrics_hourly` and `container_metrics_hourly` (SQLite and PostgreSQL), the duration-weighted average GPU power of each compressed hour (B5 with A3, E3).

### Changed

- **Impact Model `2.0.0`: projections are annualized over the target's observed coverage** (from its first observation to the end of the scan window, capped at the lookback) instead of the whole lookback. A target observed for less than one hour is annualized as if observed for one hour; coverage below one week is flagged (`short_coverage`, a note in `assumptions.notes`) and multiplies the projection's confidence by `max(coverage / 168 h, 0.25)`. Projections record `lookback_hours`, `coverage_hours`, `coverage_basis` and `first_observed_at`. Coverage comes from the target's usage summary (bounded scans) or raw row timestamps, never from rollup baseline rows; when unknown, the lookback is kept. Allocation of stored rows is unchanged.
- **Change measurement claims operational CO₂e (C3, component matching).** `co2e_g` (the operational CO₂e the Impact Model allocates to the workload, which follows its reservation) is claimed when the change is realized and its own interval shows a saving, split between the change's recommendations by projected measurable CO₂e (never twice), and compared with the projected operational components only: its `projected_annual` and `projection_accuracy` exclude embodied emissions, which the measured rows do not carry.

### Fixed

- Young workloads were projected at a fraction of their run-rate: a workload observed for 2 hours in a 14-day lookback was annualized over 336 hours (168 times too low; a workload allocated 0.05 per hour projected 2.61 per year instead of 438). E2E defect, `impact/projector.py` baseline window and `impact/model.py` annualization.
- Realized rightsizing recommendations stored a measured CO₂e of 0 while their projection included idle and embodied CO₂e: the measured value was hard-coded to 0 and compared with a projection containing a component (embodied) the measurement cannot see. E2E defect, `verify/service.py`.
- `node_idle_metrics` had no retention: its `__idle__` rows are now pruned with the pod rows of `combined_metrics` (`METRICS_RAW_RETENTION_DAYS`, same compression watermark).
- Hours re-rolled into `workload_usage_hourly` from the compressed hourly tables lost their DCGM GPU energy (the hourly tables had no GPU power); the rollup now reads it.
