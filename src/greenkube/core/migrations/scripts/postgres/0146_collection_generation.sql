-- 0146: Write generation of raw metric rows (STO-01).
--
-- collection_run_id names the collection pass of a window that wrote the row
-- and collected_at when it was written. A re-collected window replaces the
-- rows of the window the new pass no longer produces, so a window holds one
-- generation, except rows a quality guard kept (a stored OpenCost cost or
-- stored pod metadata are never replaced by worse values). NULL on rows
-- written before this migration. Metadata-only change, no table rewrite.
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS collected_at TIMESTAMPTZ;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS collection_run_id TEXT;
ALTER TABLE container_metrics ADD COLUMN IF NOT EXISTS collected_at TIMESTAMPTZ;
ALTER TABLE container_metrics ADD COLUMN IF NOT EXISTS collection_run_id TEXT;
ALTER TABLE node_idle_metrics ADD COLUMN IF NOT EXISTS collected_at TIMESTAMPTZ;
ALTER TABLE node_idle_metrics ADD COLUMN IF NOT EXISTS collection_run_id TEXT;
