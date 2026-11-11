-- 0068: Recommendation v2 columns (roadmap E4, E6, E7) (PostgreSQL).
-- target: canonical cluster-qualified TargetRef (JSON text).
-- proposal: consolidated typed proposal (JSON text), the flat columns mirror it.
-- consensus: how the proposal was chosen across sources (JSON text).
-- revision: revision of the consolidated proposal, 1 for every existing row.
-- fingerprint_v4: sha256 of the cluster, target key, capability and kind.
-- The projected impact already lives in the impact column (0046). JSON is
-- stored as text like every other recommendation_history JSON column.
-- Additive only. Existing rows keep NULL v2 columns until the scan backfills
-- them from their flat columns (the cluster identity is a runtime setting).
-- Readers derive the same values from the flat columns meanwhile.

ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS target TEXT;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS proposal TEXT;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS consensus TEXT;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS fingerprint_v4 TEXT;

CREATE INDEX IF NOT EXISTS idx_reco_fingerprint_v4 ON recommendation_history (fingerprint_v4);
