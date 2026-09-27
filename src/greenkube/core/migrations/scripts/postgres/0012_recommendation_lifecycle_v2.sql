-- 0012: Recommendation lifecycle v2 (PostgreSQL).
-- Separates "apply succeeded" (applied_at/application_method) from
-- "recommendation succeeded" (verified_at/verification_status) and adds the
-- frozen baseline, measured savings and the recommendation_events audit trail.

ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS application_method TEXT;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS verification_status TEXT;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS verification_window_start TIMESTAMPTZ;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS verification_window_end TIMESTAMPTZ;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS baseline TEXT;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS measured_co2e_saved_grams DOUBLE PRECISION;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS measured_cost_saved DOUBLE PRECISION;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS savings_realized BOOLEAN;

CREATE INDEX IF NOT EXISTS idx_reco_status_expiry ON recommendation_history (status, expires_at);
CREATE INDEX IF NOT EXISTS idx_reco_verification ON recommendation_history (verification_status, applied_at);

CREATE TABLE IF NOT EXISTS recommendation_events (
    id                 SERIAL PRIMARY KEY,
    recommendation_id  INTEGER      NOT NULL,
    event_type         TEXT         NOT NULL,
    actor              TEXT         NOT NULL DEFAULT 'system',
    payload            TEXT,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reco_events_reco
    ON recommendation_events (recommendation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_reco_events_type
    ON recommendation_events (event_type, created_at);
