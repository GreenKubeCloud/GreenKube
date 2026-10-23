-- 0001: Baseline migrations
-- Extracted from the inline ALTER TABLE statements in db.py.
-- These add columns that were missing from early schema versions.
-- SQLite does not support ADD COLUMN IF NOT EXISTS, but the migration
-- runner ensures this script runs only once.

CREATE TABLE IF NOT EXISTS combined_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pod_name TEXT NOT NULL,
    namespace TEXT NOT NULL,
    total_cost REAL,
    co2e_grams REAL,
    pue REAL,
    grid_intensity REAL,
    joules REAL,
    cpu_request INTEGER,
    memory_request INTEGER,
    period TEXT,
    "timestamp" TEXT,
    duration_seconds INTEGER,
    grid_intensity_timestamp TEXT,
    UNIQUE(pod_name, namespace, "timestamp")
);

CREATE TABLE IF NOT EXISTS node_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    node_name TEXT NOT NULL,
    instance_type TEXT,
    cpu_capacity_cores REAL,
    architecture TEXT,
    cloud_provider TEXT,
    region TEXT,
    zone TEXT,
    node_pool TEXT,
    memory_capacity_bytes INTEGER,
    UNIQUE(node_name, timestamp)
);

CREATE TABLE IF NOT EXISTS carbon_intensity_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone TEXT NOT NULL,
    carbon_intensity REAL NOT NULL,
    datetime TEXT NOT NULL,
    updated_at TEXT,
    created_at TEXT,
    emission_factor_type TEXT,
    is_estimated BOOLEAN,
    estimation_method TEXT,
    UNIQUE(zone, datetime)
);

CREATE TABLE IF NOT EXISTS node_power_consumption (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_name TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    power_consumption_mw INTEGER NOT NULL,
    UNIQUE(node_name, timestamp)
);

CREATE TABLE IF NOT EXISTS pod_resource_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pod_name TEXT NOT NULL,
    namespace TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    cpu_usage_milli_cores REAL,
    memory_usage_bytes INTEGER,
    UNIQUE(pod_name, namespace, timestamp)
);

CREATE TABLE IF NOT EXISTS instance_carbon_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    instance_type TEXT NOT NULL,
    gwp_manufacture REAL NOT NULL,
    lifespan_hours INTEGER NOT NULL,
    source TEXT,
    last_updated TEXT,
    UNIQUE(provider, instance_type)
);

CREATE TABLE IF NOT EXISTS recommendation_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pod_name TEXT,
    namespace TEXT,
    type TEXT NOT NULL,
    description TEXT NOT NULL,
    reason TEXT,
    priority TEXT,
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

ALTER TABLE combined_metrics ADD COLUMN node_instance_type TEXT;
ALTER TABLE combined_metrics ADD COLUMN node_zone TEXT;
ALTER TABLE combined_metrics ADD COLUMN emaps_zone TEXT;
ALTER TABLE combined_metrics ADD COLUMN is_estimated BOOLEAN;
ALTER TABLE combined_metrics ADD COLUMN estimation_reasons TEXT;
ALTER TABLE combined_metrics ADD COLUMN embodied_co2e_grams REAL;
ALTER TABLE combined_metrics ADD COLUMN cpu_usage_millicores INTEGER;
ALTER TABLE combined_metrics ADD COLUMN memory_usage_bytes INTEGER;
ALTER TABLE combined_metrics ADD COLUMN owner_kind TEXT;
ALTER TABLE combined_metrics ADD COLUMN owner_name TEXT;

ALTER TABLE node_snapshots ADD COLUMN embodied_emissions_kg REAL;

ALTER TABLE combined_metrics ADD COLUMN network_receive_bytes REAL;
ALTER TABLE combined_metrics ADD COLUMN network_transmit_bytes REAL;
ALTER TABLE combined_metrics ADD COLUMN disk_read_bytes REAL;
ALTER TABLE combined_metrics ADD COLUMN disk_write_bytes REAL;
ALTER TABLE combined_metrics ADD COLUMN storage_request_bytes INTEGER;
ALTER TABLE combined_metrics ADD COLUMN storage_usage_bytes INTEGER;
ALTER TABLE combined_metrics ADD COLUMN ephemeral_storage_request_bytes INTEGER;
ALTER TABLE combined_metrics ADD COLUMN ephemeral_storage_usage_bytes INTEGER;
ALTER TABLE combined_metrics ADD COLUMN gpu_usage_millicores INTEGER;
ALTER TABLE combined_metrics ADD COLUMN restart_count INTEGER;

ALTER TABLE combined_metrics ADD COLUMN node TEXT;
ALTER TABLE combined_metrics ADD COLUMN calculation_version TEXT;

CREATE INDEX IF NOT EXISTS idx_combined_ts ON combined_metrics("timestamp");
CREATE INDEX IF NOT EXISTS idx_combined_ns_ts ON combined_metrics(namespace, "timestamp");
