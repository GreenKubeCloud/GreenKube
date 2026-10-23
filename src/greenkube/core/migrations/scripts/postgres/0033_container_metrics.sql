-- 0033: Persist container-scoped resource evidence separately from pod aggregates.
CREATE TABLE IF NOT EXISTS container_metrics (
    id BIGSERIAL PRIMARY KEY,
    pod_name TEXT NOT NULL,
    namespace TEXT NOT NULL,
    container_name TEXT NOT NULL,
    total_cost REAL DEFAULT 0,
    co2e_grams REAL DEFAULT 0,
    embodied_co2e_grams REAL DEFAULT 0,
    pue REAL DEFAULT 1.0,
    grid_intensity REAL DEFAULT 0,
    joules REAL DEFAULT 0,
    cpu_request INTEGER DEFAULT 0,
    memory_request BIGINT DEFAULT 0,
    cpu_usage_millicores INTEGER,
    cpu_usage_max_millicores INTEGER,
    memory_usage_bytes BIGINT,
    memory_usage_max_bytes BIGINT,
    owner_kind TEXT,
    owner_name TEXT,
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    duration_seconds INTEGER,
    is_estimated BOOLEAN DEFAULT FALSE,
    estimation_reasons TEXT DEFAULT '[]',
    calculation_version TEXT,
    UNIQUE(namespace, pod_name, container_name, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_container_metrics_timestamp ON container_metrics(timestamp);
CREATE INDEX IF NOT EXISTS idx_container_metrics_ns_timestamp ON container_metrics(namespace, timestamp);

CREATE TABLE IF NOT EXISTS container_metrics_hourly (
    id BIGSERIAL PRIMARY KEY,
    pod_name TEXT NOT NULL,
    namespace TEXT NOT NULL,
    container_name TEXT NOT NULL,
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
    owner_kind TEXT,
    owner_name TEXT,
    duration_seconds INTEGER,
    is_estimated BOOLEAN DEFAULT FALSE,
    estimation_reasons TEXT DEFAULT '[]',
    calculation_version TEXT,
    UNIQUE(namespace, pod_name, container_name, hour_bucket)
);
CREATE INDEX IF NOT EXISTS idx_container_metrics_hourly_bucket ON container_metrics_hourly(hour_bucket);
CREATE INDEX IF NOT EXISTS idx_container_metrics_hourly_ns_bucket
    ON container_metrics_hourly(namespace, hour_bucket);
