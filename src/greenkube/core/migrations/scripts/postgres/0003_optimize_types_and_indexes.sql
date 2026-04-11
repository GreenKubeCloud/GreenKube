-- 0003: Optimize data types and add missing indexes
-- Reduces storage footprint and improves query performance.

-- ── Optimize recommendation_history ──────────────────────────────────────────
-- Allow NULL pod_name for node-level and namespace-level recommendations
-- which were previously being dropped (scope = 'node' or 'namespace').
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS scope TEXT DEFAULT 'pod';
ALTER TABLE recommendation_history ALTER COLUMN pod_name DROP NOT NULL;
ALTER TABLE recommendation_history ALTER COLUMN namespace DROP NOT NULL;

-- ── Add compound index for report/export queries ─────────────────────────────
-- The report endpoint queries by (timestamp, namespace) and then groups.
CREATE INDEX IF NOT EXISTS idx_combined_metrics_ns_ts
    ON combined_metrics(namespace, timestamp);

-- ── Index for the namespace_cache last_seen column ───────────────────────────
CREATE INDEX IF NOT EXISTS idx_namespace_cache_last_seen
    ON namespace_cache(last_seen);

-- ── Optimize carbon_intensity_history ────────────────────────────────────────
-- Add index on datetime alone for range scans
CREATE INDEX IF NOT EXISTS idx_carbon_intensity_datetime
    ON carbon_intensity_history(datetime);
