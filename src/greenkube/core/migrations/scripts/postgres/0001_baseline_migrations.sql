-- 0001: Baseline migrations
-- Extracted from the inline ALTER TABLE statements in db.py.
-- PostgreSQL supports ADD COLUMN IF NOT EXISTS natively.

ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS node_instance_type TEXT;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS node_zone TEXT;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS emaps_zone TEXT;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS is_estimated BOOLEAN;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS estimation_reasons TEXT;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS embodied_co2e_grams REAL DEFAULT 0.0;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS cpu_usage_millicores INTEGER;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS memory_usage_bytes BIGINT;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS owner_kind TEXT;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS owner_name TEXT;

UPDATE combined_metrics SET embodied_co2e_grams = 0.0 WHERE embodied_co2e_grams IS NULL;

ALTER TABLE node_snapshots ADD COLUMN IF NOT EXISTS embodied_emissions_kg REAL;

ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS network_receive_bytes DOUBLE PRECISION;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS network_transmit_bytes DOUBLE PRECISION;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS disk_read_bytes DOUBLE PRECISION;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS disk_write_bytes DOUBLE PRECISION;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS storage_request_bytes BIGINT;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS storage_usage_bytes BIGINT;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS ephemeral_storage_request_bytes BIGINT;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS ephemeral_storage_usage_bytes BIGINT;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS gpu_usage_millicores INTEGER;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS restart_count INTEGER;

ALTER TABLE carbon_intensity_history ALTER COLUMN carbon_intensity TYPE DOUBLE PRECISION;

ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS node TEXT;
ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS calculation_version TEXT;
