-- 0050: Impact Model allocation of stored metric rows (roadmap B2, B5, B8).
-- model_version records the Impact Model version that allocated a raw row
-- (NULL for rows written before the model allocated stored rows), and
-- gpu_power_watts the DCGM GPU power attributed to the pod. node_idle_metrics
-- holds the __idle__ workload of every node and step: unreserved capacity,
-- so that pod rows plus idle rows add up to the node totals. Additive.

ALTER TABLE combined_metrics
    ADD COLUMN IF NOT EXISTS model_version TEXT,
    ADD COLUMN IF NOT EXISTS gpu_power_watts DOUBLE PRECISION;

ALTER TABLE container_metrics
    ADD COLUMN IF NOT EXISTS model_version TEXT,
    ADD COLUMN IF NOT EXISTS gpu_power_watts DOUBLE PRECISION;

CREATE TABLE IF NOT EXISTS node_idle_metrics (
    id BIGSERIAL PRIMARY KEY,
    node TEXT NOT NULL,
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    duration_seconds INTEGER NOT NULL,
    scope TEXT NOT NULL DEFAULT 'idle',
    joules DOUBLE PRECISION NOT NULL DEFAULT 0,
    co2e_grams DOUBLE PRECISION NOT NULL DEFAULT 0,
    embodied_co2e_grams DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
    cost_cpu DOUBLE PRECISION,
    cost_ram DOUBLE PRECISION,
    cost_source TEXT,
    cpu_unreserved_cores DOUBLE PRECISION,
    memory_unreserved_bytes DOUBLE PRECISION,
    gpu_power_watts DOUBLE PRECISION,
    pue DOUBLE PRECISION,
    grid_intensity DOUBLE PRECISION,
    emaps_zone TEXT,
    power_source TEXT,
    intensity_source TEXT,
    model_version TEXT,
    UNIQUE (node, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_node_idle_metrics_timestamp ON node_idle_metrics(timestamp);
