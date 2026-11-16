-- 0171: Hourly aggregate of the node idle share (NRG-02, ROL-07, RECON-13).
--
-- node_idle_metrics holds the __idle__ row of every node and step. The
-- compressor now aggregates it into node_idle_metrics_hourly behind its own
-- compression watermark, like combined_metrics, prunes the raw rows with the
-- raw retention and the aggregates with the hourly retention, and rebuilds
-- an hour whose raw rows change after compression (dirty-hour trigger of
-- migration 0170). Sums are kept for additive measures, duration-weighted
-- averages for the unreserved capacity and the GPU power. The first run
-- aggregates every idle hour still in raw retention.
CREATE TABLE IF NOT EXISTS node_idle_metrics_hourly (
    id BIGSERIAL PRIMARY KEY,
    node TEXT NOT NULL,
    hour_bucket TIMESTAMP WITH TIME ZONE NOT NULL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    duration_seconds BIGINT NOT NULL DEFAULT 0,
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
    UNIQUE (node, hour_bucket)
);
CREATE INDEX IF NOT EXISTS idx_node_idle_metrics_hourly_bucket ON node_idle_metrics_hourly (hour_bucket);

DROP TRIGGER IF EXISTS trg_node_idle_metrics_dirty_hour ON node_idle_metrics;
CREATE TRIGGER trg_node_idle_metrics_dirty_hour
    AFTER INSERT OR UPDATE OR DELETE ON node_idle_metrics
    FOR EACH ROW EXECUTE FUNCTION greenkube_mark_dirty_metric_hour();
