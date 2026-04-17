-- 0003: Optimize indexes and add recommendation scope for SQLite
-- Reduces query times for report/export endpoints.

-- Ensure recommendation_history exists before altering it
CREATE TABLE IF NOT EXISTS recommendation_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pod_name TEXT,
    namespace TEXT,
    type TEXT NOT NULL,
    description TEXT NOT NULL,
    reason TEXT DEFAULT '',
    priority TEXT DEFAULT 'medium',
    potential_savings_cost REAL,
    potential_savings_co2e_grams REAL,
    current_cpu_request_millicores INTEGER,
    recommended_cpu_request_millicores INTEGER,
    current_memory_request_bytes INTEGER,
    recommended_memory_request_bytes INTEGER,
    cron_schedule TEXT,
    target_node TEXT,
    created_at TEXT NOT NULL
);

ALTER TABLE recommendation_history ADD COLUMN scope TEXT DEFAULT 'pod';

-- Ensure carbon_intensity_history exists before creating index
CREATE TABLE IF NOT EXISTS carbon_intensity_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    datetime TEXT NOT NULL,
    zone TEXT NOT NULL,
    carbon_intensity REAL NOT NULL,
    source TEXT,
    UNIQUE(zone, datetime)
);

CREATE INDEX IF NOT EXISTS idx_combined_metrics_ns_ts
    ON combined_metrics(namespace, "timestamp");

CREATE INDEX IF NOT EXISTS idx_namespace_cache_last_seen
    ON namespace_cache(last_seen);

CREATE INDEX IF NOT EXISTS idx_carbon_intensity_datetime
    ON carbon_intensity_history(datetime);
