### Added

- Per-dimension provenance on pod and container metric rows and their hourly aggregates: `cost_source`, `usage_source`, `power_source`, `intensity_source` (migration 0039, `greenkube.models.provenance` with `is_cost_trusted()` / `is_usage_trusted()` helpers). Legacy rows are backfilled (`cost_source=opencost` when a positive cost was stored, `intensity_source=unknown`; container rows inherit their pod row's `cost_source` and `emaps_zone`). (A11)
- OpenCost cost components `cost_cpu`, `cost_ram`, `cost_storage`, `cost_network`, `cost_lb` on pod and container rows; container rows split CPU cost by CPU share and RAM cost by memory share. (A4)
- Container `cpu_limit` / `memory_limit` collection and persistence; native sidecars (init containers with `restartPolicy: Always`) are collected as steady-state containers (`kind="sidecar"`), classic init containers only on request (`kind="init"`). (A7, BUG-10)
- `INTENSITY_MAX_AGE` setting (Helm `config.intensityMaxAge`, default `6h`). (A8)
- Controller Prometheus endpoint `/prometheus/metrics` (alias `/metrics`) on `CONTROLLER_METRICS_PORT` (Helm `collector.metricsPort`, default `9102`), with a `<release>-controller-metrics` Service, a controller ServiceMonitor and NetworkPolicy ingress. New metrics: `greenkube_collection_source_requests_total`, `greenkube_collection_source_duration_seconds`, `greenkube_collection_run_duration_seconds`, `greenkube_metric_rows_written_total` (provenance mix). Verification telemetry recorded by the controller is now scrapeable. (A12, BUG-14)
- `metrics_compression_watermarks` table (migration 0040). (A3)

### Changed

- OpenCost is queried on `/allocation/compute` with `aggregate=namespace,pod`, `accumulate=true`, `includeIdle=false` over the exact collection window; per-step cost uses the minutes the pod ran in the window instead of a trailing total divided by steps. (A4, BUG-41)
- Container rows inherit their pod's provenance and `is_estimated` instead of being blanket-flagged as estimated; `is_estimated` is deprecated in favour of provenance. (A11, BUG-29)
- Hourly container and pod readers keep `sample_count`. (BUG-29)
- Demo metrics carry provenance (`opencost`, `prometheus`, `instance_profile`, `consolidated`/`realtime`) so measurement gates trust them like an instrumented cluster. (A11)
- Electricity Maps and Wattnet collectors return no record when no measurement is available; zone and global defaults are applied at read time only. (A8)
- Raw metrics pruning removes whole hours below the compression watermark only, whatever `METRICS_RAW_RETENTION_DAYS`. (A3)
- The compressor logs a warning when `METRICS_COMPRESSION_AGE_HOURS` is below the 24-hour carbon-intensity recompute window, because compressed hours are never re-read. (A3)
- Rows priced with the flat `DEFAULT_COST` placeholder carry `cost_source=none` (as the 0039 backfill does), keeping `custom_rate` for configured per-unit rates. (A11)
- The Wattnet collector records every request outcome (`skipped`, `error`, `empty`, `success`) and its duration in `greenkube_collection_source_*`. (A12)

### Fixed

- Hourly compression lost samples in every hour crossing the raw-retention boundary (non-aligned cutoff + overwrite upsert); it now compresses closed hours once, from their complete raw set, behind a watermark, and never re-reads compressed hours. (A3, BUG-02)
- Same-named pods in different namespaces (e.g. `redis-0`) shared one OpenCost cost. (A4, BUG-04)
- Stale carbon intensities were used without age limit and static defaults were written into the intensity history as if measured. (A8, BUG-07)
- Zone defaults that earlier versions stored in `carbon_intensity_history` (`estimation_method=default_fallback`) are deleted by migration 0039 and ignored by every intensity lookup and by the recompute, so an upgraded install no longer labels default-priced rows `realtime`. (A8, BUG-07)
- When the recompute moves a row from a default to a measured intensity, it drops the "Carbon intensity uses the ..." entry of the deprecated `estimation_reasons` and clears `is_estimated` when that was the only reason; on PostgreSQL the pod and container updates run in one transaction. (A9)
- Intensities prefetched at the raw sample time were labelled `realtime` and could differ from the value the hourly recompute applies; the calculation now uses the classified point at the normalized lookup time. (A8)
- The intensity recompute updated `combined_metrics` only; container rows now follow (set-based on SQLite and PostgreSQL), and PostgreSQL now refreshes `is_estimated` when a provider consolidates a value. (A9, BUG-06)
