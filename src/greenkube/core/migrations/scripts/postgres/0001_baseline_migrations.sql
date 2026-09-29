-- 0001: Baseline migrations
-- Extracted from the inline ALTER TABLE statements in db.py.
-- PostgreSQL supports ADD COLUMN IF NOT EXISTS natively.

CREATE TABLE IF NOT EXISTS combined_metrics (
    id BIGSERIAL PRIMARY KEY,
    pod_name TEXT NOT NULL,
    namespace TEXT NOT NULL,
    total_cost REAL,
    co2e_grams REAL,
    pue REAL,
    grid_intensity REAL,
    joules REAL,
    cpu_request INTEGER,
    memory_request BIGINT,
    period TEXT,
    timestamp TIMESTAMP WITH TIME ZONE,
    duration_seconds INTEGER,
    grid_intensity_timestamp TIMESTAMP WITH TIME ZONE,
    UNIQUE(pod_name, namespace, timestamp)
);

CREATE TABLE IF NOT EXISTS node_snapshots (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    node_name TEXT NOT NULL,
    instance_type TEXT,
    cpu_capacity_cores REAL,
    architecture TEXT,
    cloud_provider TEXT,
    region TEXT,
    zone TEXT,
    node_pool TEXT,
    memory_capacity_bytes BIGINT,
    UNIQUE(node_name, timestamp)
);

CREATE TABLE IF NOT EXISTS carbon_intensity_history (
    id BIGSERIAL PRIMARY KEY,
    zone TEXT NOT NULL,
    carbon_intensity DOUBLE PRECISION NOT NULL,
    datetime TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE,
    emission_factor_type TEXT,
    is_estimated BOOLEAN,
    estimation_method TEXT,
    UNIQUE(zone, datetime)
);

CREATE TABLE IF NOT EXISTS node_power_consumption (
    id BIGSERIAL PRIMARY KEY,
    node_name TEXT NOT NULL,
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    power_consumption_mw INTEGER NOT NULL,
    UNIQUE(node_name, timestamp)
);

CREATE TABLE IF NOT EXISTS pod_resource_usage (
    id BIGSERIAL PRIMARY KEY,
    pod_name TEXT NOT NULL,
    namespace TEXT NOT NULL,
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    cpu_usage_milli_cores REAL,
    memory_usage_bytes BIGINT,
    UNIQUE(pod_name, namespace, timestamp)
);

CREATE TABLE IF NOT EXISTS instance_carbon_profiles (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    instance_type TEXT NOT NULL,
    gwp_manufacture REAL NOT NULL,
    lifespan_hours INTEGER NOT NULL,
    source TEXT,
    last_updated TIMESTAMP WITH TIME ZONE,
    UNIQUE(provider, instance_type)
);

CREATE TABLE IF NOT EXISTS recommendation_history (
    id BIGSERIAL PRIMARY KEY,
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
    current_memory_request_bytes BIGINT,
    recommended_memory_request_bytes BIGINT,
    cron_schedule TEXT,
    target_node TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL
);

ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS node_instance_type TEXT;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS node_zone TEXT;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS emaps_zone TEXT;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS is_estimated BOOLEAN;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS estimation_reasons TEXT;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS embodied_co2e_grams REAL DEFAULT 0.0;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS cpu_usage_millicores INTEGER;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS memory_usage_bytes BIGINT;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS owner_kind TEXT;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS owner_name TEXT;

UPDATE combined_metrics SET embodied_co2e_grams = 0.0 WHERE embodied_co2e_grams IS NULL;

ALTER TABLE node_snapshots ADD COLUMN IF NOT EXISTS embodied_emissions_kg REAL;

ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS network_receive_bytes DOUBLE PRECISION;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS network_transmit_bytes DOUBLE PRECISION;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS disk_read_bytes DOUBLE PRECISION;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS disk_write_bytes DOUBLE PRECISION;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS storage_request_bytes BIGINT;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS storage_usage_bytes BIGINT;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS ephemeral_storage_request_bytes BIGINT;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS ephemeral_storage_usage_bytes BIGINT;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS gpu_usage_millicores INTEGER;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS restart_count INTEGER;

ALTER TABLE carbon_intensity_history ALTER COLUMN carbon_intensity TYPE DOUBLE PRECISION;

ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS node TEXT;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS calculation_version TEXT;
