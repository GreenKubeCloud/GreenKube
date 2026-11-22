-- 0170: Re-aggregate compressed hours that receive late rows (ROL-02, STO-02, RECON-02).
--
-- A raw row inserted, updated or deleted in an hour below the compression
-- watermark of its table marks that hour in metrics_dirty_hours. The
-- compressor rebuilds the hourly rows of a dirty hour from its raw rows while
-- they are complete, that is while the hour is at or after raw_pruned_until
-- (the exclusive end of the raw hours already pruned). Pruning sets
-- raw_pruned_until before it deletes, so its own deletes mark nothing.
-- An UPDATE that moves a row to another hour marks its old hour as well.
-- The triggers test for an existing mark instead of INSERT OR IGNORE: the
-- conflict clause of an upsert that fires a trigger overrides the trigger's own.
CREATE TABLE IF NOT EXISTS metrics_dirty_hours (
    table_name TEXT NOT NULL,
    hour_bucket TEXT NOT NULL,
    PRIMARY KEY (table_name, hour_bucket)
);

ALTER TABLE metrics_compression_watermarks ADD COLUMN raw_pruned_until TEXT;

CREATE TRIGGER IF NOT EXISTS trg_combined_metrics_dirty_insert
AFTER INSERT ON combined_metrics
WHEN NEW."timestamp" < (SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'combined_metrics')
BEGIN
    INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
    SELECT 'combined_metrics', strftime('%Y-%m-%dT%H:00:00Z', NEW."timestamp")
    WHERE NOT EXISTS (
        SELECT 1 FROM metrics_dirty_hours WHERE table_name = 'combined_metrics' AND hour_bucket = strftime('%Y-%m-%dT%H:00:00Z', NEW."timestamp")
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_combined_metrics_dirty_update
AFTER UPDATE ON combined_metrics
WHEN NEW."timestamp" < (SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'combined_metrics')
BEGIN
    INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
    SELECT 'combined_metrics', strftime('%Y-%m-%dT%H:00:00Z', NEW."timestamp")
    WHERE NOT EXISTS (
        SELECT 1 FROM metrics_dirty_hours WHERE table_name = 'combined_metrics' AND hour_bucket = strftime('%Y-%m-%dT%H:00:00Z', NEW."timestamp")
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_combined_metrics_dirty_update_old
AFTER UPDATE ON combined_metrics
WHEN OLD."timestamp" < (SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'combined_metrics')
    AND OLD."timestamp" >= COALESCE(
        (SELECT raw_pruned_until FROM metrics_compression_watermarks WHERE table_name = 'combined_metrics'), ''
    )
    AND strftime('%Y-%m-%dT%H:00:00Z', OLD."timestamp") <> strftime('%Y-%m-%dT%H:00:00Z', NEW."timestamp")
BEGIN
    INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
    SELECT 'combined_metrics', strftime('%Y-%m-%dT%H:00:00Z', OLD."timestamp")
    WHERE NOT EXISTS (
        SELECT 1 FROM metrics_dirty_hours
        WHERE table_name = 'combined_metrics' AND hour_bucket = strftime('%Y-%m-%dT%H:00:00Z', OLD."timestamp")
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_combined_metrics_dirty_delete
AFTER DELETE ON combined_metrics
WHEN OLD."timestamp" < (SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'combined_metrics')
    AND OLD."timestamp" >= COALESCE(
        (SELECT raw_pruned_until FROM metrics_compression_watermarks WHERE table_name = 'combined_metrics'), ''
    )
BEGIN
    INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
    SELECT 'combined_metrics', strftime('%Y-%m-%dT%H:00:00Z', OLD."timestamp")
    WHERE NOT EXISTS (
        SELECT 1 FROM metrics_dirty_hours WHERE table_name = 'combined_metrics' AND hour_bucket = strftime('%Y-%m-%dT%H:00:00Z', OLD."timestamp")
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_container_metrics_dirty_insert
AFTER INSERT ON container_metrics
WHEN NEW."timestamp" < (SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'container_metrics')
BEGIN
    INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
    SELECT 'container_metrics', strftime('%Y-%m-%dT%H:00:00Z', NEW."timestamp")
    WHERE NOT EXISTS (
        SELECT 1 FROM metrics_dirty_hours WHERE table_name = 'container_metrics' AND hour_bucket = strftime('%Y-%m-%dT%H:00:00Z', NEW."timestamp")
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_container_metrics_dirty_update
AFTER UPDATE ON container_metrics
WHEN NEW."timestamp" < (SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'container_metrics')
BEGIN
    INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
    SELECT 'container_metrics', strftime('%Y-%m-%dT%H:00:00Z', NEW."timestamp")
    WHERE NOT EXISTS (
        SELECT 1 FROM metrics_dirty_hours WHERE table_name = 'container_metrics' AND hour_bucket = strftime('%Y-%m-%dT%H:00:00Z', NEW."timestamp")
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_container_metrics_dirty_update_old
AFTER UPDATE ON container_metrics
WHEN OLD."timestamp" < (SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'container_metrics')
    AND OLD."timestamp" >= COALESCE(
        (SELECT raw_pruned_until FROM metrics_compression_watermarks WHERE table_name = 'container_metrics'), ''
    )
    AND strftime('%Y-%m-%dT%H:00:00Z', OLD."timestamp") <> strftime('%Y-%m-%dT%H:00:00Z', NEW."timestamp")
BEGIN
    INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
    SELECT 'container_metrics', strftime('%Y-%m-%dT%H:00:00Z', OLD."timestamp")
    WHERE NOT EXISTS (
        SELECT 1 FROM metrics_dirty_hours
        WHERE table_name = 'container_metrics' AND hour_bucket = strftime('%Y-%m-%dT%H:00:00Z', OLD."timestamp")
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_container_metrics_dirty_delete
AFTER DELETE ON container_metrics
WHEN OLD."timestamp" < (SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'container_metrics')
    AND OLD."timestamp" >= COALESCE(
        (SELECT raw_pruned_until FROM metrics_compression_watermarks WHERE table_name = 'container_metrics'), ''
    )
BEGIN
    INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
    SELECT 'container_metrics', strftime('%Y-%m-%dT%H:00:00Z', OLD."timestamp")
    WHERE NOT EXISTS (
        SELECT 1 FROM metrics_dirty_hours WHERE table_name = 'container_metrics' AND hour_bucket = strftime('%Y-%m-%dT%H:00:00Z', OLD."timestamp")
    );
END;

-- Existing installs: the first raw hour may have been pruned at a non-aligned
-- cutoff by the legacy compressor, so raw hours count as complete only after it.
UPDATE metrics_compression_watermarks
SET raw_pruned_until = COALESCE(
    (SELECT strftime('%Y-%m-%dT%H:00:00Z', MIN("timestamp"), '+1 hour') FROM combined_metrics),
    compressed_until
)
WHERE table_name = 'combined_metrics';

UPDATE metrics_compression_watermarks
SET raw_pruned_until = COALESCE(
    (SELECT strftime('%Y-%m-%dT%H:00:00Z', MIN("timestamp"), '+1 hour') FROM container_metrics),
    compressed_until
)
WHERE table_name = 'container_metrics';

-- One-off repair: every compressed hour whose raw rows are complete is
-- re-aggregated once, which brings rows written after its compression into
-- the hourly table. The first raw hour, below raw_pruned_until, is left out.
INSERT OR IGNORE INTO metrics_dirty_hours (table_name, hour_bucket)
SELECT DISTINCT 'combined_metrics', strftime('%Y-%m-%dT%H:00:00Z', "timestamp")
FROM combined_metrics
WHERE "timestamp" < (SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'combined_metrics')
  AND "timestamp" >= (SELECT raw_pruned_until FROM metrics_compression_watermarks WHERE table_name = 'combined_metrics');

INSERT OR IGNORE INTO metrics_dirty_hours (table_name, hour_bucket)
SELECT DISTINCT 'container_metrics', strftime('%Y-%m-%dT%H:00:00Z', "timestamp")
FROM container_metrics
WHERE "timestamp" < (SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'container_metrics')
  AND "timestamp" >= (SELECT raw_pruned_until FROM metrics_compression_watermarks WHERE table_name = 'container_metrics');
