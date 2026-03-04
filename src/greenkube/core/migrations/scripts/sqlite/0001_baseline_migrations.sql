-- 0001: Baseline migrations
-- Extracted from the inline ALTER TABLE statements in db.py.
-- These add columns that were missing from early schema versions.
-- SQLite does not support ADD COLUMN IF NOT EXISTS, but the migration
-- runner ensures this script runs only once.

ALTER TABLE combined_metrics ADD COLUMN node_instance_type TEXT;
ALTER TABLE combined_metrics ADD COLUMN node_zone TEXT;
ALTER TABLE combined_metrics ADD COLUMN emaps_zone TEXT;
ALTER TABLE combined_metrics ADD COLUMN is_estimated BOOLEAN;
ALTER TABLE combined_metrics ADD COLUMN estimation_reasons TEXT;
ALTER TABLE combined_metrics ADD COLUMN embodied_co2e_grams REAL;
ALTER TABLE combined_metrics ADD COLUMN cpu_usage_millicores INTEGER;
ALTER TABLE combined_metrics ADD COLUMN memory_usage_bytes INTEGER;
ALTER TABLE combined_metrics ADD COLUMN owner_kind TEXT;
ALTER TABLE combined_metrics ADD COLUMN owner_name TEXT;

ALTER TABLE node_snapshots ADD COLUMN embodied_emissions_kg REAL;

ALTER TABLE combined_metrics ADD COLUMN network_receive_bytes REAL;
ALTER TABLE combined_metrics ADD COLUMN network_transmit_bytes REAL;
ALTER TABLE combined_metrics ADD COLUMN disk_read_bytes REAL;
ALTER TABLE combined_metrics ADD COLUMN disk_write_bytes REAL;
ALTER TABLE combined_metrics ADD COLUMN storage_request_bytes INTEGER;
ALTER TABLE combined_metrics ADD COLUMN storage_usage_bytes INTEGER;
ALTER TABLE combined_metrics ADD COLUMN ephemeral_storage_request_bytes INTEGER;
ALTER TABLE combined_metrics ADD COLUMN ephemeral_storage_usage_bytes INTEGER;
ALTER TABLE combined_metrics ADD COLUMN gpu_usage_millicores INTEGER;
ALTER TABLE combined_metrics ADD COLUMN restart_count INTEGER;

ALTER TABLE combined_metrics ADD COLUMN node TEXT;
ALTER TABLE combined_metrics ADD COLUMN calculation_version TEXT;

CREATE INDEX IF NOT EXISTS idx_combined_ts ON combined_metrics("timestamp");
CREATE INDEX IF NOT EXISTS idx_combined_ns_ts ON combined_metrics(namespace, "timestamp");
