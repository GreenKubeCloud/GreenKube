### Added
- **Immutable savings measurements (C0.13, BUG-37):** every verification decision is stored once in the new `savings_measurements` table (migration `0041`; database triggers reject `UPDATE` and `DELETE` on SQLite and PostgreSQL) with its windows, signed per-quantity estimates, health gates, reasons and an evidence SHA-256. Re-measurements add a superseding row; a crashed verification replays its stored decision instead of deciding again.
- **Explicit re-measurement (C0.5, BUG-31):** `POST /api/v1/recommendations/{id}/remeasure` starts a new fixed-horizon measurement of a `not_realized` or `inconclusive` change (`202`; `409` otherwise).
- **Rollback-review dismissal (C0.11, BUG-37):** `POST /api/v1/recommendations/{id}/rollback-review/dismiss` returns a false-positive `rollback_review` to `applied` with a required reason and measures it again.
- **Workload health collector (C0.9, BUG-35):** the lifecycle job now gives the verifier OOM kills (`kube_pod_container_status_last_terminated_reason`), readiness (`kube_pod_status_ready`) and the CPU throttling ratio (`container_cpu_cfs_throttled_periods_total / container_cpu_cfs_periods_total`) from Prometheus. Pod presence is read over the whole window, so pods replaced before its end still count. A signal that cannot be read is reported `not_available` with its reason, never counted as a pass.
- **Lifecycle vocabulary (C0.11):** event types `not_realized`, `inconclusive`, `remeasure_requested`, `rollback_dismissed` and `regressed`; status `regressed` (reachable only from `verified`, reserved for drift detection).

### Changed
- **Fixed-horizon verification (C0.5, C0.12):** the after-window starts at rollout completion and lasts `VERIFICATION_WINDOW_HOURS`; it no longer grows with late evaluation. The baseline window has the same length and ends at the PR merge (or plan approval). `not_realized` and `inconclusive` are terminal for a measurement.
- **Apply detection waits for the rollout (C0.12, BUG-38):** a change counts as applied only once every replica is updated and available and `observedGeneration >= generation`; `applied_at` is the rollout completion time (Deployment `Progressing` condition, otherwise detection time). `freeze_baseline` records the evidence window it copies (`captured_at`, `evidence_window`), its `frozen_at` time and `baseline_window_end`.
- **Projection is reported, not gated:** a change is realized when the measured saving clears a 2% noise floor of the baseline rate. `VERIFICATION_MIN_SAVINGS_RATIO` now only flags a realized saving far below its projection.
- **Rightsizing never claims CO2e:** the usage-based CO2e delta of a request change is kept in the measurement as `not_claimed`; `measured_co2e_saved_grams` is 0.
- **Trusted rows by provenance (C0.3, BUG-29):** cost rows are selected by `cost_source` (`opencost`, `kubecost`, `pricing_catalog`, finite non-negative cost; delegated to the shared A11 provenance helpers when installed) when present, otherwise by `total_cost > 0` and `is_estimated = false`; rates divide by covered time. Container-scoped changes are measured on trusted container rows, or on their workload when container rows carry no trusted cost.
- **Attribution groups (C0.10, BUG-36):** CPU and memory records on the same container applied by the same plan are measured once; the cost delta is split by `cost_cpu`/`cost_ram`, or by projected share without components.
- **State machine (C0.11, BUG-37):** `PATCH /recommendations/{id}/apply` accepts only `active` and `pr_open` records (`409` otherwise); `active → verified` is no longer allowed; every transition is a compare-and-swap written in the same transaction as its event.
- **Verification metrics:** `greenkube_verification_health_gate_failures_total` is labelled by gate name instead of free-text reasons.
- The SQLite migration runner keeps trigger bodies (`BEGIN ...; END;`) in one statement.

### Fixed
- **Traffic normalization inflated savings ~4× (C0.1, BUG-27):** normalization is disabled until load-driver counterfactuals ship; the traffic ratio is a rate recorded for information only, and the unused `seasonality_factor` read is removed.
- **Multi-replica changes always went to rollback review (C0.2, BUG-28):** the usage gate compares the per-pod p95 with the applied per-pod request × headroom; restarts are per-pod deltas of the cumulative counter, new pods counted from their first observation.
- **Container-scoped (GitOps) changes were never measurable (C0.3, BUG-29).**
- **Retest-until-pass (C0.5, BUG-31):** `not_realized` changes are no longer re-evaluated every few hours with a growing window.
- **OOM, readiness and throttling gates were inert in production (C0.9, BUG-35).**
- **CPU and memory records each claimed the whole workload delta (C0.10, BUG-36).**
- **`mark_inconclusive` wrote a `verified` event and deleted the measured input (C0.11, C0.13, BUG-37).**
- **`applied_at` was the detection time and the baseline `captured_at` was "now" (C0.12, BUG-38).**
