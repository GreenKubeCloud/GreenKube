### Added
- Workload spec observations and change records store a resources digest (requests and limits of every container, init container and native sidecar) and an image digest next to the pod-template digest (migration 0136, MEA-08).

### Changed
- `request_rightsizing` strategy version 2.5: a pod-template change during a measurement with the change's requests and limits is a `template_change` confounder (mild, moderate when the image changed or is unknown) instead of a severe `spec_change`, so an image deployment, an env change or a rollout restart no longer contaminates the measurement (MEA-08). Measurements stored with earlier versions are kept as they were; a contaminated change can be re-measured.

### Fixed
- The `co2e_factor_fallback` confounder no longer lowers the confidence of the cost verdict: it only touches the CO2e valuation.
- The drift monitor closes a realized change only when the requests or limits changed (or the workload was recreated with other resources); a template change with the same resources keeps the change realized and accruing and records an informational `template_change` event. For changes recorded before the resources digest, the live container requests are read before closing (MEA-08).
