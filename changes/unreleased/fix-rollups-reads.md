### Added
- `greenkube_metrics_compression_lag_seconds{table}` and a `compression` block in `/api/v1/health` (watermarks, lag, `degraded` above two hours); a failed compression step marks the `metrics-compression` loop tick failed (ROL-01).

### Changed
- `COLLECTION_MAX_BACKFILL` may not exceed `METRICS_COMPRESSION_AGE_HOURS`, which must be at least 1: startup fails with a clear message otherwise (ROL-02).

### Fixed
- Hourly compression is bounded by the collection watermark and stops before the first failed or cost-pending step, so an hour is never closed before its rows are collected (STO-02).
- Raw rows written, re-written or deleted in an already compressed hour (`--last` re-collection, retried or re-priced steps, recompute) mark the hour dirty and the next compression run rebuilds its hourly rows from the raw rows while they are kept; late rows in an hour whose raw rows are already pruned never replace its complete aggregate, in the hourly tables or in the usage rollup. Migration 0170 adds the triggers and queues every compressed hour still in raw retention for one rebuild, which repairs divergences of the last `METRICS_RAW_RETENTION_DAYS`; older losses cannot be recovered (ROL-02, RECON-02).
- Every reader that combines raw and hourly metric rows splits at the compression watermark instead of wall-clock `now - METRICS_COMPRESSION_AGE_HOURS`: the hour before that cutoff, hours the compressor has not reached and the partial first hour of a window are no longer dropped from summaries, time series, breakdowns, counts, pages and measurement reads (ROL-01, RECON-03, API-03, MEA-18). Stored rows are unchanged; cached summaries are rebuilt on the next refresh.
