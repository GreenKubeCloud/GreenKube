### Added
- `greenkube_metrics_compression_lag_seconds{table}` and a `compression` block in `/api/v1/health` (watermarks, lag, `degraded` above two hours); a failed compression step marks the `metrics-compression` loop tick failed (ROL-01).

### Fixed
- Every reader that combines raw and hourly metric rows splits at the compression watermark instead of wall-clock `now - METRICS_COMPRESSION_AGE_HOURS`: the hour before that cutoff, hours the compressor has not reached and the partial first hour of a window are no longer dropped from summaries, time series, breakdowns, counts, pages and measurement reads (ROL-01, RECON-03, API-03, MEA-18). Stored rows are unchanged; cached summaries are rebuilt on the next refresh.
