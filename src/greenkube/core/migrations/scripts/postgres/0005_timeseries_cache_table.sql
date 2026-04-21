-- 0005: Pre-computed timeseries chart cache (PostgreSQL)
-- One row per (window_slug, namespace, bucket_ts).
-- Refreshed hourly alongside metrics_summary to serve dashboard charts
-- without scanning raw metric tables.

CREATE TABLE IF NOT EXISTS metrics_timeseries_cache (
    id          SERIAL PRIMARY KEY,
    window_slug TEXT NOT NULL,
    namespace   TEXT,
    bucket_ts   TIMESTAMPTZ NOT NULL,
    co2e_grams          DOUBLE PRECISION NOT NULL DEFAULT 0,
    embodied_co2e_grams DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_cost          DOUBLE PRECISION NOT NULL DEFAULT 0,
    joules              DOUBLE PRECISION NOT NULL DEFAULT 0,
    UNIQUE(window_slug, namespace, bucket_ts)
);

CREATE INDEX IF NOT EXISTS idx_ts_cache_lookup
    ON metrics_timeseries_cache(window_slug, namespace);
