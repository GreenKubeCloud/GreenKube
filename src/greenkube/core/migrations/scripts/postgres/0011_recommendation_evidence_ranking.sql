-- 0011: Recommendation evidence, risk and ranking (PostgreSQL).
-- Adds the self-contained review evidence block (JSON), risk/confidence/effort
-- assessment, multi-criteria ranking score and the machine-readable patch plan.

ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS evidence TEXT;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS risk_level TEXT;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS risk_factors TEXT;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS effort TEXT;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS ranking_score DOUBLE PRECISION;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS ranking_factors TEXT;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS patch TEXT;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS reversible BOOLEAN;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS requires_restart BOOLEAN;

CREATE INDEX IF NOT EXISTS idx_reco_status_ranking ON recommendation_history (status, ranking_score);
CREATE INDEX IF NOT EXISTS idx_reco_expires_at ON recommendation_history (expires_at);
