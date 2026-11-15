### Added
- Change measurement: a moderate `constant_prices_unavailable` confounder when the cost has no CPU/RAM components and no baseline unit prices (`constant_price_basis = observed`), so a price move that cannot be removed is no longer silent.
- Change measurement: hour buckets record `carbon_factor` (grid intensity x PUE of the hour's trusted-carbon rows, independent of energy; absent from older snapshots and their digest), so an hour with zero energy keeps its own carbon factor. `counterfactual_params` records `co2e_factor_fallback` and `co2e_factor_fallback_share`, and a mild `co2e_factor_fallback` confounder reports more than 5% of the baseline energy valued at the fallback factor.

### Changed
- Change measurement (`request_rightsizing` 2.4, MEA-03): each post-window hour's CO2e is valued at its own carbon factor and each baseline hour at the post factor of the same hour of week (the plain time average of the post factors as fallback, for hours of the week without one), `co2e_basis = energy_delta_at_post_hourly_intensity`. A single energy-weighted window factor over-valued savings when load followed the grid intensity (3.5 g/h claimed for a true 3.0 g/h). Measurements stored with `energy_delta_at_post_intensity` are kept unchanged.
- Change measurement: the `intensity_shift` confounder is informative and no longer lowers the measurement confidence.
- Change drift monitor: the baseline, post-window and monitored days are valued at the constant baseline prices of the snapshot price card, so a later price rise no longer reverses a saving claimed at constant prices; the `regressed` payload records `constant_price_basis`.

### Fixed
- Change measurement (MEA-02): baseline snapshots frozen without charged quantities get them derived per hour (`max(core_hours, cpu_usage_cores)`, `gib_hours`; `constant_price_basis = charged_derived`) instead of valuing the request, which booked a saving the allocator never gave when a request was cut below the usage (0.0200/h claimed for a true 0.0110/h).
- Methodology: the verdict step of the deterministic-baseline exemption at a relative noise of 1e-4 is documented.
