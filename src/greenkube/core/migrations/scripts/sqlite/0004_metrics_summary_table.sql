-- 0004: Pre-computed dashboard summary table (SQLite)
-- Stores one row per (window_slug, namespace) pair.
-- Refreshed hourly by SummaryRefresher to avoid full-table scans on every
-- frontend request.

CREATE TABLE IF NOT EXISTS metrics_summary (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    window_slug TEXT    NOT NULL,
    namespace   TEXT,
    total_co2e_grams          REAL NOT NULL DEFAULT 0,
    total_embodied_co2e_grams REAL NOT NULL DEFAULT 0,
    total_cost                REAL NOT NULL DEFAULT 0,
    total_energy_joules       REAL NOT NULL DEFAULT 0,
    pod_count                 INTEGER NOT NULL DEFAULT 0,
    namespace_count           INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    UNIQUE(window_slug, namespace)
);

CREATE INDEX IF NOT EXISTS idx_metrics_summary_slug
    ON metrics_summary(window_slug);
