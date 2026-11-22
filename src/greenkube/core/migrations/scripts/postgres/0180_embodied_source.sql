-- 0180: Provenance of embodied emissions (NRG-01).
-- embodied_source on raw rows: boavizta, configured, fallback_per_vcpu or fallback_default.
-- Rows written before stay NULL: they used the flat DEFAULT_EMBODIED_EMISSIONS_KG fallback
-- and are re-allocated by `greenkube recompute`.
-- instance_carbon_profiles.error keeps why a Boavizta lookup failed (source = 'fallback'),
-- a negative cache entry retried after 24 hours.

ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS embodied_source TEXT;
ALTER TABLE container_metrics ADD COLUMN IF NOT EXISTS embodied_source TEXT;
ALTER TABLE node_idle_metrics ADD COLUMN IF NOT EXISTS embodied_source TEXT;
ALTER TABLE instance_carbon_profiles ADD COLUMN IF NOT EXISTS error TEXT;
