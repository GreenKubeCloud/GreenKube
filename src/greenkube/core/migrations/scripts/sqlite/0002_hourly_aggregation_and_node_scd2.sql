-- 0002: Hourly aggregation table + Node SCD Type 2
-- Addresses OOM issues from loading raw 5-min metrics into RAM.

-- ── Hourly aggregated metrics table ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS combined_metrics_hourly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pod_name TEXT NOT NULL,
    namespace TEXT NOT NULL,
    hour_bucket TEXT NOT NULL,
    sample_count INTEGER NOT NULL DEFAULT 1,
    total_cost REAL DEFAULT 0,
    co2e_grams REAL DEFAULT 0,
    embodied_co2e_grams REAL DEFAULT 0,
    pue REAL DEFAULT 1.0,
    grid_intensity REAL DEFAULT 0,
    joules REAL DEFAULT 0,
    cpu_request INTEGER DEFAULT 0,
    memory_request INTEGER DEFAULT 0,
    cpu_usage_avg INTEGER,
    cpu_usage_max INTEGER,
    memory_usage_avg INTEGER,
    memory_usage_max INTEGER,
    network_receive_bytes REAL,
    network_transmit_bytes REAL,
    disk_read_bytes REAL,
    disk_write_bytes REAL,
    storage_request_bytes INTEGER,
    storage_usage_bytes INTEGER,
    gpu_usage_millicores INTEGER,
    restart_count INTEGER DEFAULT 0,
    owner_kind TEXT,
    owner_name TEXT,
    duration_seconds INTEGER,
    node TEXT,
    node_instance_type TEXT,
    node_zone TEXT,
    emaps_zone TEXT,
    is_estimated BOOLEAN DEFAULT 0,
    estimation_reasons TEXT DEFAULT '[]',
    calculation_version TEXT,
    UNIQUE(pod_name, namespace, hour_bucket)
);

CREATE INDEX IF NOT EXISTS idx_hourly_metrics_bucket ON combined_metrics_hourly(hour_bucket);
CREATE INDEX IF NOT EXISTS idx_hourly_metrics_ns_bucket ON combined_metrics_hourly(namespace, hour_bucket);

-- ── Namespace cache table ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS namespace_cache (
    namespace TEXT PRIMARY KEY,
    last_seen TEXT NOT NULL
);

-- ── Node SCD Type 2 ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS node_snapshots_scd (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_name TEXT NOT NULL,
    instance_type TEXT,
    cpu_capacity_cores REAL,
    architecture TEXT,
    cloud_provider TEXT,
    region TEXT,
    zone TEXT,
    node_pool TEXT,
    memory_capacity_bytes INTEGER,
    embodied_emissions_kg REAL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    is_current BOOLEAN NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_node_scd_current ON node_snapshots_scd(node_name, is_current);
CREATE INDEX IF NOT EXISTS idx_node_scd_range ON node_snapshots_scd(node_name, valid_from, valid_to);

-- ── Add index for compression queries on raw table ───────────────────────────
CREATE INDEX IF NOT EXISTS idx_combined_metrics_ns_pod_ts ON combined_metrics(namespace, pod_name, "timestamp");
