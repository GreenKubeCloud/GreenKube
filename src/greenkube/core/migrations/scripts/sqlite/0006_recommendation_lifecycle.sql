-- 0006: Recommendation lifecycle columns (SQLite)
-- Adds status tracking and applied/ignored audit fields to the
-- recommendation_history table.
-- Recommended values are clamped by the engine so no "unrealistic" flag is needed.

ALTER TABLE recommendation_history ADD COLUMN status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE recommendation_history ADD COLUMN applied_at TEXT;
ALTER TABLE recommendation_history ADD COLUMN actual_cpu_request_millicores INTEGER;
ALTER TABLE recommendation_history ADD COLUMN actual_memory_request_bytes INTEGER;
ALTER TABLE recommendation_history ADD COLUMN carbon_saved_co2e_grams REAL;
ALTER TABLE recommendation_history ADD COLUMN cost_saved REAL;
ALTER TABLE recommendation_history ADD COLUMN ignored_at TEXT;
ALTER TABLE recommendation_history ADD COLUMN ignored_reason TEXT;
ALTER TABLE recommendation_history ADD COLUMN updated_at TEXT;

CREATE INDEX IF NOT EXISTS idx_reco_status ON recommendation_history(status);
CREATE INDEX IF NOT EXISTS idx_reco_ns_status ON recommendation_history(namespace, status);
