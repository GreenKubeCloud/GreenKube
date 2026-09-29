-- 0000: Initial schema extracted from the original inline PostgreSQL bootstrap.

CREATE TABLE IF NOT EXISTS combined_metrics (
    id SERIAL PRIMARY KEY,
    pod_name TEXT NOT NULL,
    namespace TEXT NOT NULL,
    total_cost REAL,
    co2e_grams REAL,
    pue REAL,
    grid_intensity REAL,
    joules REAL,
    cpu_request INTEGER,
    memory_request BIGINT,
    cpu_usage_millicores INTEGER,
    memory_usage_bytes BIGINT,
    network_receive_bytes DOUBLE PRECISION,
    network_transmit_bytes DOUBLE PRECISION,
    disk_read_bytes DOUBLE PRECISION,
    disk_write_bytes DOUBLE PRECISION,
    storage_request_bytes BIGINT,
    storage_usage_bytes BIGINT,
    ephemeral_storage_request_bytes BIGINT,
    ephemeral_storage_usage_bytes BIGINT,
    gpu_usage_millicores INTEGER,
    restart_count INTEGER,
    owner_kind TEXT,
    owner_name TEXT,
    period TEXT,
    timestamp TIMESTAMP WITH TIME ZONE,
    duration_seconds INTEGER,
    grid_intensity_timestamp TIMESTAMP WITH TIME ZONE,
    node TEXT,
    node_instance_type TEXT,
    node_zone TEXT,
    emaps_zone TEXT,
    is_estimated BOOLEAN,
    estimation_reasons TEXT,
    embodied_co2e_grams REAL,
    calculation_version TEXT,
    UNIQUE(pod_name, namespace, timestamp)
);

CREATE TABLE IF NOT EXISTS node_snapshots (
    id SERIAL PRIMARY KEY,
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
    embodied_emissions_kg REAL,
    UNIQUE(node_name, timestamp)
);

CREATE TABLE IF NOT EXISTS carbon_intensity_history (
    id SERIAL PRIMARY KEY,
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
    id SERIAL PRIMARY KEY,
    node_name TEXT NOT NULL,
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    power_consumption_mw INTEGER NOT NULL,
    UNIQUE(node_name, timestamp)
);

CREATE TABLE IF NOT EXISTS pod_resource_usage (
    id SERIAL PRIMARY KEY,
    pod_name TEXT NOT NULL,
    namespace TEXT NOT NULL,
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    cpu_usage_milli_cores REAL,
    memory_usage_bytes BIGINT,
    UNIQUE(pod_name, namespace, timestamp)
);

CREATE TABLE IF NOT EXISTS instance_carbon_profiles (
    id SERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    instance_type TEXT NOT NULL,
    gwp_manufacture REAL NOT NULL,
    lifespan_hours INTEGER NOT NULL,
    source TEXT,
    last_updated TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(provider, instance_type)
);

CREATE TABLE IF NOT EXISTS recommendation_history (
    id SERIAL PRIMARY KEY,
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

CREATE INDEX IF NOT EXISTS idx_combined_metrics_timestamp
    ON combined_metrics (timestamp);
CREATE INDEX IF NOT EXISTS idx_node_snapshots_timestamp
    ON node_snapshots (timestamp);
CREATE INDEX IF NOT EXISTS idx_instance_profiles_type
    ON instance_carbon_profiles (provider, instance_type);
CREATE INDEX IF NOT EXISTS idx_reco_history_created_at
    ON recommendation_history (created_at);
CREATE INDEX IF NOT EXISTS idx_reco_history_type
    ON recommendation_history (type);
