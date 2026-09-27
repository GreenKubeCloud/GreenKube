-- 0012: Recommendation lifecycle v2 (SQLite).
-- Separates "apply succeeded" (applied_at/application_method) from
-- "recommendation succeeded" (verified_at/verification_status) and adds the
-- frozen baseline, measured savings and the recommendation_events audit trail.

ALTER TABLE recommendation_history ADD COLUMN application_method TEXT;
ALTER TABLE recommendation_history ADD COLUMN verified_at TEXT;
ALTER TABLE recommendation_history ADD COLUMN verification_status TEXT;
ALTER TABLE recommendation_history ADD COLUMN verification_window_start TEXT;
ALTER TABLE recommendation_history ADD COLUMN verification_window_end TEXT;
ALTER TABLE recommendation_history ADD COLUMN baseline TEXT;
ALTER TABLE recommendation_history ADD COLUMN measured_co2e_saved_grams REAL;
ALTER TABLE recommendation_history ADD COLUMN measured_cost_saved REAL;
ALTER TABLE recommendation_history ADD COLUMN savings_realized INTEGER;

CREATE INDEX IF NOT EXISTS idx_reco_status_expiry ON recommendation_history (status, expires_at);
CREATE INDEX IF NOT EXISTS idx_reco_verification ON recommendation_history (verification_status, applied_at);

CREATE TABLE IF NOT EXISTS recommendation_events (
    id                 INTEGER  PRIMARY KEY AUTOINCREMENT,
    recommendation_id  INTEGER  NOT NULL,
    event_type         TEXT     NOT NULL,
    actor              TEXT     NOT NULL DEFAULT 'system',
    payload            TEXT,
    created_at         TEXT     NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_reco_events_reco
    ON recommendation_events (recommendation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_reco_events_type
    ON recommendation_events (event_type, created_at);
