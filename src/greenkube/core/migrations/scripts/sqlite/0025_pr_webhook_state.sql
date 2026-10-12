-- 0025: Plan, provider, commit, merge and sync state for PRs plus webhook deliveries (SQLite).

ALTER TABLE recommendation_pull_requests ADD COLUMN plan_id TEXT;
ALTER TABLE recommendation_pull_requests ADD COLUMN provider_id TEXT;
ALTER TABLE recommendation_pull_requests ADD COLUMN commit_sha TEXT;
ALTER TABLE recommendation_pull_requests ADD COLUMN merge_commit_sha TEXT;
ALTER TABLE recommendation_pull_requests ADD COLUMN merged_at TEXT;
ALTER TABLE recommendation_pull_requests ADD COLUMN closed_at TEXT;
ALTER TABLE recommendation_pull_requests ADD COLUMN sync_status TEXT;
ALTER TABLE recommendation_pull_requests ADD COLUMN sync_revision TEXT;
ALTER TABLE recommendation_pull_requests ADD COLUMN sync_observed_at TEXT;
ALTER TABLE recommendation_pull_requests ADD COLUMN sync_error_code TEXT;

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    repo TEXT,
    pr_number INTEGER CHECK (pr_number IS NULL OR pr_number > 0),
    action TEXT,
    merged INTEGER NOT NULL DEFAULT 0 CHECK (merged IN (0, 1)),
    merge_commit_sha TEXT,
    head_sha TEXT,
    base_sha TEXT,
    occurred_at TEXT,
    status TEXT NOT NULL DEFAULT 'received',
    received_at TEXT NOT NULL DEFAULT (datetime('now')),
    processed_at TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (provider_id, delivery_id)
);

CREATE INDEX IF NOT EXISTS idx_reco_pr_plan
    ON recommendation_pull_requests (plan_id);
CREATE INDEX IF NOT EXISTS idx_reco_pr_provider_status
    ON recommendation_pull_requests (provider_id, status);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_status
    ON webhook_deliveries (status, received_at);
