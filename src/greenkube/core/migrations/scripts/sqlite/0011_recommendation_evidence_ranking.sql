-- 0011: Recommendation evidence, risk and ranking (SQLite).
-- Adds the self-contained review evidence block (JSON), risk/confidence/effort
-- assessment, multi-criteria ranking score and the machine-readable patch plan.

ALTER TABLE recommendation_history ADD COLUMN evidence TEXT;
ALTER TABLE recommendation_history ADD COLUMN risk_level TEXT;
ALTER TABLE recommendation_history ADD COLUMN risk_factors TEXT;
ALTER TABLE recommendation_history ADD COLUMN confidence REAL;
ALTER TABLE recommendation_history ADD COLUMN effort TEXT;
ALTER TABLE recommendation_history ADD COLUMN ranking_score REAL;
ALTER TABLE recommendation_history ADD COLUMN ranking_factors TEXT;
ALTER TABLE recommendation_history ADD COLUMN patch TEXT;
ALTER TABLE recommendation_history ADD COLUMN expires_at TEXT;
ALTER TABLE recommendation_history ADD COLUMN reversible INTEGER;
ALTER TABLE recommendation_history ADD COLUMN requires_restart INTEGER;

CREATE INDEX IF NOT EXISTS idx_reco_status_ranking ON recommendation_history (status, ranking_score);
CREATE INDEX IF NOT EXISTS idx_reco_expires_at ON recommendation_history (expires_at);
