-- 0050: Impact Model allocation of stored metric rows (roadmap B2, B5, B8) (SQLite).
-- model_version records the Impact Model version that allocated a raw row
-- (NULL for rows written before the model allocated stored rows), and
-- gpu_power_watts the DCGM GPU power attributed to the pod. node_idle_metrics
-- holds the __idle__ workload of every node and step: unreserved capacity,
-- so that pod rows plus idle rows add up to the node totals. Additive.
-- Note: the runner splits scripts on semicolons, so comments must not use them.

ALTER TABLE combined_metrics ADD COLUMN model_version TEXT;
ALTER TABLE combined_metrics ADD COLUMN gpu_power_watts REAL;
ALTER TABLE container_metrics ADD COLUMN model_version TEXT;
ALTER TABLE container_metrics ADD COLUMN gpu_power_watts REAL;

CREATE TABLE IF NOT EXISTS node_idle_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node TEXT NOT NULL,
    "timestamp" TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    scope TEXT NOT NULL DEFAULT 'idle',
    joules REAL NOT NULL DEFAULT 0,
    co2e_grams REAL NOT NULL DEFAULT 0,
    embodied_co2e_grams REAL NOT NULL DEFAULT 0,
    total_cost REAL NOT NULL DEFAULT 0,
    cost_cpu REAL,
    cost_ram REAL,
    cost_source TEXT,
    cpu_unreserved_cores REAL,
    memory_unreserved_bytes REAL,
    gpu_power_watts REAL,
    pue REAL,
    grid_intensity REAL,
    emaps_zone TEXT,
    power_source TEXT,
    intensity_source TEXT,
    model_version TEXT,
    UNIQUE (node, "timestamp")
);
CREATE INDEX IF NOT EXISTS idx_node_idle_metrics_timestamp ON node_idle_metrics("timestamp");
