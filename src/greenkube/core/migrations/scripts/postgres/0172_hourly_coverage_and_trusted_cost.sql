-- 0172: Faithful hourly rows for pod replacement and mixed trust (ROL-03, MEA-05).
--
-- first_sample_at and last_sample_at bound the raw samples of each pod and
-- hour, so a change measurement covers the union of the pods' lifetimes
-- instead of the longest one. trusted_cost, trusted_cost_cpu,
-- trusted_cost_ram and trusted_cost_seconds keep the trusted part of an hour
-- that holds one untrusted cost sample. Older hourly rows keep NULL. Every
-- compressed hour whose raw rows are still complete is queued once for a
-- rebuild (metrics_dirty_hours, migration 0170), which fills the new columns.
ALTER TABLE combined_metrics_hourly
    ADD COLUMN IF NOT EXISTS first_sample_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS last_sample_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS trusted_cost DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS trusted_cost_cpu DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS trusted_cost_ram DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS trusted_cost_seconds DOUBLE PRECISION;

ALTER TABLE container_metrics_hourly
    ADD COLUMN IF NOT EXISTS first_sample_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS last_sample_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS trusted_cost DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS trusted_cost_cpu DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS trusted_cost_ram DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS trusted_cost_seconds DOUBLE PRECISION;

INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
SELECT DISTINCT 'combined_metrics', date_trunc('hour', timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
FROM combined_metrics
WHERE timestamp < (SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'combined_metrics')
  AND timestamp >= COALESCE(
      (SELECT raw_pruned_until FROM metrics_compression_watermarks WHERE table_name = 'combined_metrics'),
      '-infinity'::timestamptz
  )
ON CONFLICT DO NOTHING;

INSERT INTO metrics_dirty_hours (table_name, hour_bucket)
SELECT DISTINCT 'container_metrics', date_trunc('hour', timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
FROM container_metrics
WHERE timestamp < (SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'container_metrics')
  AND timestamp >= COALESCE(
      (SELECT raw_pruned_until FROM metrics_compression_watermarks WHERE table_name = 'container_metrics'),
      '-infinity'::timestamptz
  )
ON CONFLICT DO NOTHING;
