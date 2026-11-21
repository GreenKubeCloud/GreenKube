-- 0220: user-cancelled plans and the reason of the last plan status change (F-10). Additive.
--
-- POST /automation/plans/{id}/cancel used to mark a plan "stale" and its reason
-- "cancelled_by_user" was only kept in the recommendation audit trail. The plan
-- now ends in "cancelled" and status_reason keeps the reason code of the last
-- status change. Existing rows keep their status, status_reason stays NULL.
--
-- SQLite cannot alter a CHECK constraint, so the table is rebuilt (as in 0023):
-- every row is copied unchanged, then the indexes are recreated. Foreign keys
-- are not enforced on GreenKube SQLite connections, so the rows of the tables
-- that reference automation_plans are kept.

DROP TABLE IF EXISTS automation_plans_new_0220;

CREATE TABLE automation_plans_new_0220 (
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
            'needs_mapping', 'ambiguous', 'rejected', 'superseded', 'cancelled'
        )),
    expires_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    rollback_of_plan_id TEXT REFERENCES automation_plans(plan_id) ON DELETE RESTRICT,
    file_plan_summary TEXT,
    committed_on_revision TEXT,
    delivery_mode TEXT,
    status_reason TEXT
);

INSERT INTO automation_plans_new_0220 (
    plan_id, source_id, provider_id, source_snapshot, action_json, change_set_id,
    base_revision, source_revision, renderer, diff, rendered_diff, digest,
    validation, warnings, policy_result, status, expires_at, created_at, updated_at,
    rollback_of_plan_id, file_plan_summary, committed_on_revision, delivery_mode
)
SELECT
    plan_id, source_id, provider_id, source_snapshot, action_json, change_set_id,
    base_revision, source_revision, renderer, diff, rendered_diff, digest,
    validation, warnings, policy_result, status, expires_at, created_at, updated_at,
    rollback_of_plan_id, file_plan_summary, committed_on_revision, delivery_mode
FROM automation_plans;

DROP TABLE automation_plans;
ALTER TABLE automation_plans_new_0220 RENAME TO automation_plans;

CREATE INDEX IF NOT EXISTS idx_automation_plans_status_expiry
    ON automation_plans (status, expires_at);
CREATE INDEX IF NOT EXISTS idx_automation_plans_rollback_of
    ON automation_plans (rollback_of_plan_id);
CREATE INDEX IF NOT EXISTS idx_automation_plans_status_created
    ON automation_plans (status, created_at, plan_id);
CREATE INDEX IF NOT EXISTS idx_automation_plans_source_created
    ON automation_plans (source_id, created_at, plan_id);
CREATE INDEX IF NOT EXISTS idx_automation_plans_workload
    ON automation_plans (
        json_extract(source_snapshot, '$.cluster'),
        json_extract(source_snapshot, '$.namespace'),
        json_extract(source_snapshot, '$.workload_kind'),
        json_extract(source_snapshot, '$.workload_name'),
        created_at,
        plan_id
    );
