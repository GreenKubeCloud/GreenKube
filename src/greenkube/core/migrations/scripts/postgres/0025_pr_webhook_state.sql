-- 0025: Plan, provider, commit, merge and sync state for PRs plus webhook deliveries (PostgreSQL).

ALTER TABLE recommendation_pull_requests ADD COLUMN IF NOT EXISTS plan_id TEXT;
ALTER TABLE recommendation_pull_requests ADD COLUMN IF NOT EXISTS provider_id TEXT;
ALTER TABLE recommendation_pull_requests ADD COLUMN IF NOT EXISTS commit_sha TEXT;
ALTER TABLE recommendation_pull_requests ADD COLUMN IF NOT EXISTS merge_commit_sha TEXT;
ALTER TABLE recommendation_pull_requests ADD COLUMN IF NOT EXISTS merged_at TIMESTAMPTZ;
ALTER TABLE recommendation_pull_requests ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;
ALTER TABLE recommendation_pull_requests ADD COLUMN IF NOT EXISTS sync_status TEXT;
ALTER TABLE recommendation_pull_requests ADD COLUMN IF NOT EXISTS sync_revision TEXT;
ALTER TABLE recommendation_pull_requests ADD COLUMN IF NOT EXISTS sync_observed_at TIMESTAMPTZ;
ALTER TABLE recommendation_pull_requests ADD COLUMN IF NOT EXISTS sync_error_code TEXT;

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id BIGSERIAL PRIMARY KEY,
    provider_id TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    repo TEXT,
    pr_number INTEGER CHECK (pr_number IS NULL OR pr_number > 0),
    action TEXT,
    merged BOOLEAN NOT NULL DEFAULT FALSE,
    merge_commit_sha TEXT,
    head_sha TEXT,
    base_sha TEXT,
    occurred_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'received',
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (provider_id, delivery_id)
);

CREATE INDEX IF NOT EXISTS idx_reco_pr_plan
    ON recommendation_pull_requests (plan_id);
CREATE INDEX IF NOT EXISTS idx_reco_pr_provider_status
    ON recommendation_pull_requests (provider_id, status);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_status
    ON webhook_deliveries (status, received_at);
