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
-- A NULL compressed_until means not compressed yet: every table has a row
-- from the start (seeded below), so the trigger always has a row to lock.
ALTER TABLE metrics_compression_watermarks ALTER COLUMN compressed_until DROP NOT NULL;

-- Concurrency: the watermark row is read FOR SHARE, so a compression or a
-- rebuild of this table (which lock it FOR UPDATE first) waits for the
-- writer to commit and then sees its rows, or the writer waits for the
-- compression and then marks the hour it closed. An existing mark is
-- updated, not skipped, so it is row-locked and a rebuild deleting it waits
-- for the writer. An UPDATE that moves a row marks its old hour as well.
CREATE OR REPLACE FUNCTION greenkube_mark_dirty_metric_hour() RETURNS trigger AS $$
DECLARE
    mark RECORD;
BEGIN
    SELECT compressed_until, raw_pruned_until INTO mark
    FROM metrics_compression_watermarks WHERE table_name = TG_TABLE_NAME
    FOR SHARE;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF TG_OP <> 'DELETE' AND NEW.timestamp < mark.compressed_until THEN
        INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
        VALUES (TG_TABLE_NAME, date_trunc('hour', NEW.timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
        ON CONFLICT (table_name, hour_bucket) DO UPDATE SET hour_bucket = EXCLUDED.hour_bucket;
    END IF;
    IF TG_OP <> 'INSERT' AND OLD.timestamp < mark.compressed_until
        AND (mark.raw_pruned_until IS NULL OR OLD.timestamp >= mark.raw_pruned_until)
        AND (TG_OP = 'DELETE'
             OR date_trunc('hour', OLD.timestamp AT TIME ZONE 'UTC')
                <> date_trunc('hour', NEW.timestamp AT TIME ZONE 'UTC')) THEN
        INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
        VALUES (TG_TABLE_NAME, date_trunc('hour', OLD.timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
        ON CONFLICT (table_name, hour_bucket) DO UPDATE SET hour_bucket = EXCLUDED.hour_bucket;
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

-- One-off repair: every compressed hour whose raw rows are complete is
-- re-aggregated once, which brings rows written after its compression into
-- the hourly table. The first raw hour, below raw_pruned_until, is left out.
INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
SELECT DISTINCT 'combined_metrics', date_trunc('hour', timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
FROM combined_metrics
WHERE timestamp < (SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'combined_metrics')
  AND timestamp >= (SELECT raw_pruned_until FROM metrics_compression_watermarks WHERE table_name = 'combined_metrics')
ON CONFLICT DO NOTHING;

INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
SELECT DISTINCT 'container_metrics', date_trunc('hour', timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
FROM container_metrics
WHERE timestamp < (SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'container_metrics')
  AND timestamp >= (SELECT raw_pruned_until FROM metrics_compression_watermarks WHERE table_name = 'container_metrics')
ON CONFLICT DO NOTHING;

-- Seed a watermark row for tables never compressed, with compressed_until
-- NULL. The trigger locks it FOR SHARE, so the first compression (which
-- upserts it first) waits for raw writers that are still open.
INSERT INTO metrics_compression_watermarks (table_name, compressed_until, updated_at)
VALUES ('combined_metrics', NULL, NOW()), ('container_metrics', NULL, NOW())
ON CONFLICT (table_name) DO NOTHING;
