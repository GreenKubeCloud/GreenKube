### Added
- Workload spec observations and change records store a resources digest (requests and limits of every container, init container and native sidecar) and an image digest next to the pod-template digest (migration 0136, MEA-08).

### Changed
- `request_rightsizing` strategy version 2.5: a pod-template change during a measurement with the change's requests and limits is a `template_change` confounder (mild, moderate when the image changed or is unknown) instead of a severe `spec_change`, so an image deployment, an env change or a rollout restart no longer contaminates the measurement (MEA-08). Measurements stored with earlier versions are kept as they were; a contaminated change can be re-measured.
- Accelerated demo windows (`CHANGE_MEASUREMENT_ACCELERATED`, quickstart only) bucket rows into 5-minute slots, use the baseline mean as counterfactual (`flat_mean_accelerated`) and demo thresholds (6 post slots), so the quickstart windows reach a decided verdict; such measurements record `representative: false` (MEA-10).
- `CHANGE_MEASUREMENT_POST_WINDOW_DAYS` must be at least 5 days (a verdict needs 5 complete days of aligned hours), at least 30 minutes with `CHANGE_MEASUREMENT_ACCELERATED`; shorter values are rejected at startup and by the values schema (MEA-10).

### Fixed
- The reason of an undecided quantity names the true cause: no metric rows, rows without trusted cost or carbon, or hours below the 50% coverage threshold (with count and best coverage); request quantities are no longer called "trusted" (MEA-10).
- The `co2e_factor_fallback` confounder no longer lowers the confidence of the cost verdict: it only touches the CO2e valuation.
- The drift monitor closes a realized change only when the requests or limits changed (or the workload was recreated with other resources); a template change with the same resources keeps the change realized and accruing and records an informational `template_change` event. For changes recorded before the resources digest, the live container requests are read before closing (MEA-08).
