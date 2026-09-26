-- 0010: Recommendation sources, provenance and ownership (PostgreSQL).
-- Adds connector provenance (source/source_ref/sources/superseded_by),
-- the optimization capability used for cross-source arbitration, and the
-- workload owner identity required by the future GitOps patcher.

ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'greenkube';
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS source_ref TEXT;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS sources TEXT;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS superseded_by TEXT;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS capability TEXT;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS owner_kind TEXT;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS owner_name TEXT;

CREATE INDEX IF NOT EXISTS idx_reco_source_status ON recommendation_history (source, status);
