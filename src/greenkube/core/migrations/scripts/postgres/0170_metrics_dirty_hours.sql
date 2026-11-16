-- 0170: Re-aggregate compressed hours that receive late rows (ROL-02, STO-02, RECON-02).
--
-- A raw row inserted, updated or deleted in an hour below the compression
-- watermark of its table marks that hour in metrics_dirty_hours. The
-- compressor rebuilds the hourly rows of a dirty hour from its raw rows while
-- they are complete, that is while the hour is at or after raw_pruned_until
-- (the exclusive end of the raw hours already pruned). Pruning sets
-- raw_pruned_until before it deletes, so its own deletes mark nothing.
CREATE TABLE IF NOT EXISTS metrics_dirty_hours (
    table_name TEXT NOT NULL,
    hour_bucket TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (table_name, hour_bucket)
);

ALTER TABLE metrics_compression_watermarks ADD COLUMN IF NOT EXISTS raw_pruned_until TIMESTAMP WITH TIME ZONE;

CREATE OR REPLACE FUNCTION greenkube_mark_dirty_metric_hour() RETURNS trigger AS $$
DECLARE
    ts TIMESTAMP WITH TIME ZONE;
    mark RECORD;
BEGIN
    IF TG_OP = 'DELETE' THEN
        ts := OLD.timestamp;
    ELSE
        ts := NEW.timestamp;
    END IF;
    SELECT compressed_until, raw_pruned_until INTO mark
    FROM metrics_compression_watermarks WHERE table_name = TG_TABLE_NAME;
    IF FOUND AND ts < mark.compressed_until
        AND (TG_OP <> 'DELETE' OR mark.raw_pruned_until IS NULL OR ts >= mark.raw_pruned_until) THEN
        INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
        VALUES (TG_TABLE_NAME, date_trunc('hour', ts AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
        ON CONFLICT DO NOTHING;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_combined_metrics_dirty_hour ON combined_metrics;
CREATE TRIGGER trg_combined_metrics_dirty_hour
    AFTER INSERT OR UPDATE OR DELETE ON combined_metrics
    FOR EACH ROW EXECUTE FUNCTION greenkube_mark_dirty_metric_hour();

DROP TRIGGER IF EXISTS trg_container_metrics_dirty_hour ON container_metrics;
CREATE TRIGGER trg_container_metrics_dirty_hour
    AFTER INSERT OR UPDATE OR DELETE ON container_metrics
    FOR EACH ROW EXECUTE FUNCTION greenkube_mark_dirty_metric_hour();

-- Existing installs: the first raw hour may have been pruned at a non-aligned
-- cutoff by the legacy compressor, so raw hours count as complete only after it.
UPDATE metrics_compression_watermarks
SET raw_pruned_until = COALESCE(
    (SELECT date_trunc('hour', MIN(timestamp) AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' + INTERVAL '1 hour'
     FROM combined_metrics),
    compressed_until
)
WHERE table_name = 'combined_metrics';

UPDATE metrics_compression_watermarks
SET raw_pruned_until = COALESCE(
    (SELECT date_trunc('hour', MIN(timestamp) AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' + INTERVAL '1 hour'
     FROM container_metrics),
    compressed_until
)
WHERE table_name = 'container_metrics';

-- One-off repair: every compressed hour still in raw retention is re-aggregated
-- once, which brings rows written after its compression into the hourly table.
INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
SELECT DISTINCT 'combined_metrics', date_trunc('hour', timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
FROM combined_metrics
WHERE timestamp < (SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'combined_metrics')
ON CONFLICT DO NOTHING;

INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
SELECT DISTINCT 'container_metrics', date_trunc('hour', timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
FROM container_metrics
WHERE timestamp < (SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'container_metrics')
ON CONFLICT DO NOTHING;
