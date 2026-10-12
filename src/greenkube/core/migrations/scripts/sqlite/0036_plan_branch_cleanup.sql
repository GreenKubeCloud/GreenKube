-- 0036: persist idempotent cleanup completion for deterministic plan branches.

CREATE TABLE IF NOT EXISTS automation_branch_cleanup_completed (
    plan_id TEXT PRIMARY KEY REFERENCES automation_plans(plan_id) ON DELETE CASCADE,
    provider_id TEXT NOT NULL,
    repo TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_branch_cleanup_completed_reference
    ON automation_branch_cleanup_completed (provider_id, repo, branch_name);
