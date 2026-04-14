-- 0004: Pre-computed dashboard summary table (PostgreSQL)
-- Stores one row per (window_slug, namespace) pair.
-- Refreshed hourly by SummaryRefresher to avoid full-table scans on every
-- frontend request.

CREATE TABLE IF NOT EXISTS metrics_summary (
    id          SERIAL PRIMARY KEY,
    window_slug TEXT    NOT NULL,
    namespace   TEXT,
    total_co2e_grams          DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_embodied_co2e_grams DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_cost                DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_energy_joules       DOUBLE PRECISION NOT NULL DEFAULT 0,
    pod_count                 INTEGER NOT NULL DEFAULT 0,
    namespace_count           INTEGER NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL,
    UNIQUE(window_slug, namespace)
);

CREATE INDEX IF NOT EXISTS idx_metrics_summary_slug
    ON metrics_summary(window_slug);
