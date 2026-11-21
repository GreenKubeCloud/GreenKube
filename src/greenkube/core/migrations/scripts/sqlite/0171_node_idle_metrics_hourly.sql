-- 0171: Hourly aggregate of the node idle share (NRG-02, ROL-07, RECON-13).
--
-- node_idle_metrics holds the __idle__ row of every node and step. The
-- compressor now aggregates it into node_idle_metrics_hourly behind its own
-- compression watermark, like combined_metrics, prunes the raw rows with the
-- raw retention and the aggregates with the hourly retention, and rebuilds
-- an hour whose raw rows change after compression (dirty-hour triggers of
-- migration 0170). Sums are kept for additive measures, duration-weighted
-- averages for the unreserved capacity and the GPU power. The first run
-- aggregates every idle hour still in raw retention.
CREATE TABLE IF NOT EXISTS node_idle_metrics_hourly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node TEXT NOT NULL,
    hour_bucket TEXT NOT NULL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    duration_seconds INTEGER NOT NULL DEFAULT 0,
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
    UNIQUE (node, hour_bucket)
);
CREATE INDEX IF NOT EXISTS idx_node_idle_metrics_hourly_bucket ON node_idle_metrics_hourly (hour_bucket);

CREATE TRIGGER IF NOT EXISTS trg_node_idle_metrics_dirty_insert
AFTER INSERT ON node_idle_metrics
WHEN NEW."timestamp" < (SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'node_idle_metrics')
BEGIN
    INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
    SELECT 'node_idle_metrics', strftime('%Y-%m-%dT%H:00:00Z', NEW."timestamp")
    WHERE NOT EXISTS (
        SELECT 1 FROM metrics_dirty_hours
        WHERE table_name = 'node_idle_metrics' AND hour_bucket = strftime('%Y-%m-%dT%H:00:00Z', NEW."timestamp")
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_node_idle_metrics_dirty_update
AFTER UPDATE ON node_idle_metrics
WHEN NEW."timestamp" < (SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'node_idle_metrics')
BEGIN
    INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
    SELECT 'node_idle_metrics', strftime('%Y-%m-%dT%H:00:00Z', NEW."timestamp")
    WHERE NOT EXISTS (
        SELECT 1 FROM metrics_dirty_hours
        WHERE table_name = 'node_idle_metrics' AND hour_bucket = strftime('%Y-%m-%dT%H:00:00Z', NEW."timestamp")
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_node_idle_metrics_dirty_update_old
AFTER UPDATE ON node_idle_metrics
WHEN OLD."timestamp" < (SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'node_idle_metrics')
    AND OLD."timestamp" >= COALESCE(
        (SELECT raw_pruned_until FROM metrics_compression_watermarks WHERE table_name = 'node_idle_metrics'), ''
    )
    AND strftime('%Y-%m-%dT%H:00:00Z', OLD."timestamp") <> strftime('%Y-%m-%dT%H:00:00Z', NEW."timestamp")
BEGIN
    INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
    SELECT 'node_idle_metrics', strftime('%Y-%m-%dT%H:00:00Z', OLD."timestamp")
    WHERE NOT EXISTS (
        SELECT 1 FROM metrics_dirty_hours
        WHERE table_name = 'node_idle_metrics' AND hour_bucket = strftime('%Y-%m-%dT%H:00:00Z', OLD."timestamp")
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_node_idle_metrics_dirty_delete
AFTER DELETE ON node_idle_metrics
WHEN OLD."timestamp" < (SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'node_idle_metrics')
    AND OLD."timestamp" >= COALESCE(
        (SELECT raw_pruned_until FROM metrics_compression_watermarks WHERE table_name = 'node_idle_metrics'), ''
    )
BEGIN
    INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
    SELECT 'node_idle_metrics', strftime('%Y-%m-%dT%H:00:00Z', OLD."timestamp")
    WHERE NOT EXISTS (
        SELECT 1 FROM metrics_dirty_hours
        WHERE table_name = 'node_idle_metrics' AND hour_bucket = strftime('%Y-%m-%dT%H:00:00Z', OLD."timestamp")
    );
END;
