-- 0013: Pull requests opened by the GreenKube bot (PostgreSQL).
-- Tracks PR lifecycle independently of recommendation status: a recommendation
-- can have multiple attempts and stays active until the change is merged.

CREATE TABLE IF NOT EXISTS recommendation_pull_requests (
    id                 SERIAL PRIMARY KEY,
    recommendation_id  INTEGER      NOT NULL,
    provider           TEXT         NOT NULL,
    repo               TEXT         NOT NULL,
    base_branch        TEXT         NOT NULL DEFAULT 'main',
    head_branch        TEXT,
    pr_number          INTEGER,
    pr_url             TEXT,
    status             TEXT         NOT NULL DEFAULT 'pending',
    error              TEXT,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_reco_pr_reco
    ON recommendation_pull_requests (recommendation_id);
CREATE INDEX IF NOT EXISTS idx_reco_pr_status
    ON recommendation_pull_requests (status, created_at);
