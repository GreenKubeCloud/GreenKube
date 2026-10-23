-- 0024: Durable automation plans, file edits and recommendation links (SQLite).

CREATE TABLE IF NOT EXISTS automation_plans (
    plan_id TEXT PRIMARY KEY,
    source_id TEXT REFERENCES workload_sources(id) ON DELETE SET NULL,
    provider_id TEXT REFERENCES gitops_providers(id) ON DELETE SET NULL,
    source_snapshot TEXT NOT NULL DEFAULT '{}',
    action_json TEXT NOT NULL DEFAULT '{}',
    change_set_id TEXT,
    base_revision TEXT,
    source_revision TEXT,
    renderer TEXT,
    diff TEXT,
    rendered_diff TEXT,
    digest TEXT,
    validation TEXT NOT NULL DEFAULT '{}',
    warnings TEXT NOT NULL DEFAULT '[]',
    policy_result TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN (
            'draft', 'validated', 'approved', 'applying', 'pr_open', 'merged',
            'synced', 'applied', 'sync_failed', 'closed', 'stale', 'failed',
            'needs_mapping', 'ambiguous', 'rejected', 'superseded'
        )),
    expires_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS automation_plan_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES automation_plans(plan_id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('create', 'update', 'delete')),
    original_sha TEXT,
    content TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (plan_id, path)
);

CREATE TABLE IF NOT EXISTS automation_plan_recommendations (
    plan_id TEXT NOT NULL REFERENCES automation_plans(plan_id) ON DELETE CASCADE,
    recommendation_id INTEGER NOT NULL REFERENCES recommendation_history(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (plan_id, recommendation_id)
);

CREATE INDEX IF NOT EXISTS idx_automation_plans_status_expiry
    ON automation_plans (status, expires_at);
CREATE INDEX IF NOT EXISTS idx_automation_plan_files_plan
    ON automation_plan_files (plan_id);
CREATE INDEX IF NOT EXISTS idx_automation_plan_recommendations_recommendation
    ON automation_plan_recommendations (recommendation_id);
