-- 0172: Faithful hourly rows for pod replacement and mixed trust (ROL-03, MEA-05).
--
-- first_sample_at and last_sample_at bound the raw samples of each pod and
-- hour, so a change measurement covers the union of the pods' lifetimes
-- instead of the longest one. trusted_cost, trusted_cost_cpu,
-- trusted_cost_ram and trusted_cost_seconds keep the trusted part of an hour
-- that holds one untrusted cost sample. Older hourly rows keep NULL. Every
-- compressed hour whose raw rows are still complete is queued once for a
-- rebuild (metrics_dirty_hours, migration 0170), which fills the new columns.
ALTER TABLE combined_metrics_hourly ADD COLUMN first_sample_at TEXT;
ALTER TABLE combined_metrics_hourly ADD COLUMN last_sample_at TEXT;
ALTER TABLE combined_metrics_hourly ADD COLUMN trusted_cost REAL;
ALTER TABLE combined_metrics_hourly ADD COLUMN trusted_cost_cpu REAL;
ALTER TABLE combined_metrics_hourly ADD COLUMN trusted_cost_ram REAL;
ALTER TABLE combined_metrics_hourly ADD COLUMN trusted_cost_seconds REAL;

ALTER TABLE container_metrics_hourly ADD COLUMN first_sample_at TEXT;
ALTER TABLE container_metrics_hourly ADD COLUMN last_sample_at TEXT;
ALTER TABLE container_metrics_hourly ADD COLUMN trusted_cost REAL;
ALTER TABLE container_metrics_hourly ADD COLUMN trusted_cost_cpu REAL;
ALTER TABLE container_metrics_hourly ADD COLUMN trusted_cost_ram REAL;
ALTER TABLE container_metrics_hourly ADD COLUMN trusted_cost_seconds REAL;

INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
SELECT DISTINCT 'combined_metrics', strftime('%Y-%m-%dT%H:00:00Z', "timestamp")
FROM combined_metrics
WHERE "timestamp" < (SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'combined_metrics')
  AND "timestamp" >= COALESCE(
      (SELECT raw_pruned_until FROM metrics_compression_watermarks WHERE table_name = 'combined_metrics'), ''
  )
  AND NOT EXISTS (
      SELECT 1 FROM metrics_dirty_hours AS d
      WHERE d.table_name = 'combined_metrics' AND d.hour_bucket = strftime('%Y-%m-%dT%H:00:00Z', "timestamp")
  );

INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
SELECT DISTINCT 'container_metrics', strftime('%Y-%m-%dT%H:00:00Z', "timestamp")
FROM container_metrics
WHERE "timestamp" < (SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'container_metrics')
  AND "timestamp" >= COALESCE(
      (SELECT raw_pruned_until FROM metrics_compression_watermarks WHERE table_name = 'container_metrics'), ''
  )
  AND NOT EXISTS (
      SELECT 1 FROM metrics_dirty_hours AS d
      WHERE d.table_name = 'container_metrics' AND d.hour_bucket = strftime('%Y-%m-%dT%H:00:00Z', "timestamp")
  );
