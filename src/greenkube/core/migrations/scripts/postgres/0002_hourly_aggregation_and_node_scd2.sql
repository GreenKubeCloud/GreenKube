-- 0002: Hourly aggregation table + Node SCD Type 2
-- Addresses OOM issues from loading raw 5-min metrics into RAM.
-- Adds a pre-aggregated hourly table for dashboard/report queries.
-- Converts node_snapshots to SCD Type 2 to avoid storing duplicate snapshots.

-- ── Hourly aggregated metrics table ──────────────────────────────────────────
-- Stores pre-computed hourly rollups per (namespace, pod).
-- The API reads from this table for any time range > METRICS_COMPRESSION_AGE_HOURS.
CREATE TABLE IF NOT EXISTS combined_metrics_hourly (
    id SERIAL PRIMARY KEY,
    pod_name TEXT NOT NULL,
    namespace TEXT NOT NULL,
    hour_bucket TIMESTAMP WITH TIME ZONE NOT NULL,
    sample_count INTEGER NOT NULL DEFAULT 1,
    total_cost REAL DEFAULT 0,
    co2e_grams REAL DEFAULT 0,
    embodied_co2e_grams REAL DEFAULT 0,
    pue REAL DEFAULT 1.0,
    grid_intensity REAL DEFAULT 0,
    joules REAL DEFAULT 0,
    cpu_request INTEGER DEFAULT 0,
    memory_request BIGINT DEFAULT 0,
    cpu_usage_avg INTEGER,
    cpu_usage_max INTEGER,
    memory_usage_avg BIGINT,
    memory_usage_max BIGINT,
    network_receive_bytes DOUBLE PRECISION,
    network_transmit_bytes DOUBLE PRECISION,
    disk_read_bytes DOUBLE PRECISION,
    disk_write_bytes DOUBLE PRECISION,
    storage_request_bytes BIGINT,
    storage_usage_bytes BIGINT,
    gpu_usage_millicores INTEGER,
    restart_count INTEGER DEFAULT 0,
    owner_kind TEXT,
    owner_name TEXT,
    duration_seconds INTEGER,
    node TEXT,
    node_instance_type TEXT,
    node_zone TEXT,
    emaps_zone TEXT,
    is_estimated BOOLEAN DEFAULT FALSE,
    estimation_reasons TEXT DEFAULT '[]',
    calculation_version TEXT,
    UNIQUE(pod_name, namespace, hour_bucket)
);
CREATE INDEX IF NOT EXISTS idx_hourly_metrics_bucket ON combined_metrics_hourly(hour_bucket);
CREATE INDEX IF NOT EXISTS idx_hourly_metrics_ns_bucket ON combined_metrics_hourly(namespace, hour_bucket);

-- ── Namespace cache table ────────────────────────────────────────────────────
-- Avoids scanning combined_metrics just to list namespaces.
CREATE TABLE IF NOT EXISTS namespace_cache (
    namespace TEXT PRIMARY KEY,
    last_seen TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- ── Node SCD Type 2 ─────────────────────────────────────────────────────────
-- Add valid_from / valid_to columns to track node configuration history
-- without storing duplicate snapshots every 5 minutes.
-- is_current allows fast lookup of the active node record.
CREATE TABLE IF NOT EXISTS node_snapshots_scd (
    id SERIAL PRIMARY KEY,
    node_name TEXT NOT NULL,
    instance_type TEXT,
    cpu_capacity_cores REAL,
    architecture TEXT,
    cloud_provider TEXT,
    region TEXT,
    zone TEXT,
    node_pool TEXT,
    memory_capacity_bytes BIGINT,
    embodied_emissions_kg REAL,
    valid_from TIMESTAMP WITH TIME ZONE NOT NULL,
    valid_to TIMESTAMP WITH TIME ZONE,
    is_current BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_node_scd_current ON node_snapshots_scd(node_name, is_current) WHERE is_current = TRUE;
CREATE INDEX IF NOT EXISTS idx_node_scd_range ON node_snapshots_scd(node_name, valid_from, valid_to);

-- ── Add index for compression queries on raw table ───────────────────────────
CREATE INDEX IF NOT EXISTS idx_combined_metrics_ns_pod_ts
    ON combined_metrics(namespace, pod_name, timestamp);
