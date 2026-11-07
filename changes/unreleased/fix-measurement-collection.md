### Added

- `PromQLCatalog` (`collectors/promql.py`) is the single definition of every PromQL expression; rate windows span `max(step, 4 × PROMETHEUS_SCRAPE_INTERVAL)` (A1).
- Gap-aware collection: half-open `CollectionWindow`, `collection_watermarks` table (migration 0038) and a catch-up job that collects every missing window oldest first, up to `COLLECTION_MAX_BACKFILL`, and advances the watermark only after a window is written (A2).
- Settings `PROMETHEUS_SCRAPE_INTERVAL` (`30s`), `COLLECTION_SAFETY_LAG` (`1m`) and `COLLECTION_MAX_BACKFILL` (`24h`), with Helm values `config.prometheus.scrapeInterval`, `config.collection.safetyLag` and `config.collection.maxBackfill` (A1, A2).
- Collection telemetry: `greenkube_collection_watermark_lag_seconds{source}`, `greenkube_collection_gap_windows_total{source}`, `greenkube_collection_windows_total{source}`, `greenkube_collection_window_errors_total{source,reason}`, `greenkube_collection_window_duration_seconds{source}`, `greenkube_collection_dropped_samples_total{reason}` and `greenkube_unknown_power_profile_nodes` (A2, A6, A10).
- `LeaderFence`: leader-gated controller jobs receive a fencing token (the Lease generation) and the collection job re-checks it before writing rows and before advancing the watermark (A14, BUG-13).

### Changed

- **Data note:** CPU, memory and energy stored before this release are inflated (the pod cgroup series was summed with the container series, ≈2× CPU). New rows carry `calculation_version = <version>+range.2`. On upgrade the first collection run re-collects the last 24 hours (`COLLECTION_MAX_BACKFILL`) and overwrites those rows with corrected values; older raw and hourly rows are not rewritten (A1, BUG-01).
- A stored row with `timestamp = T` now describes `[T, T + step)` (rate evaluated at `T + step`), and every repository range read uses `timestamp >= start AND timestamp < end` (A2, BUG-03).
- The metrics collection job runs on wall-clock step boundaries plus `COLLECTION_SAFETY_LAG` instead of a jittered interval, keeping the watermark lag below two steps (A2).
- `greenkube start --last` re-collects the given range with an idempotent upsert instead of doing nothing when any row exists (A2, BUG-03).
- Nodes with an unknown instance type get a power profile derived from their CPU capacity and the provider's CCF per-vCPU coefficients (cross-provider average when unknown) instead of `DEFAULT_INSTANCE_PROFILE`; the remaining fallback is flagged as estimated and counted (A10, BUG-08).
- `PrometheusCollector.collect_range` raises `PrometheusQueryError` when Prometheus is unconfigured, unreachable or returns an error, so an outage is retried rather than recorded as an empty window (A2).

### Fixed

- Range CPU, memory, disk I/O and ephemeral-storage queries exclude the pod cgroup (`container=""`) and pause (`container="POD"`) series; the CPU fallback reads the pod cgroup series only once (A1, BUG-01).
- `run_range` no longer returns stored rows when any row exists in the range, and inclusive `BETWEEN` reads no longer make consecutive windows share a sample, so no window is skipped, double counted or shifted (A2, BUG-03).
- Multi-step ranges (backfills, `--last`) store each step's own CPU, memory, network, disk and restart values instead of one chunk-wide value on every row (A2).
- Metrics-server usage is attached only to the latest sample of a current window and only when it was measured during or after that sample; container rows keep their sample timestamp and step duration (A5, BUG-05).
- Range parsing drops NaN/Inf samples and clamps negative rates to 0 (A6, BUG-09).
- Pods whose cAdvisor series lack a `node` label are placed on their node via `kube_pod_info` instead of a single 10 W pseudo-node (A10, BUG-08).
