-- 0044: automation runtime reliability (roadmap D2, D7, D11, D15).

-- D2: retention keeps a content-free summary when it prunes plan file contents.
ALTER TABLE automation_plans ADD COLUMN IF NOT EXISTS file_plan_summary JSONB;

-- D7: the branch head a plan was committed on when it moved after planning.
ALTER TABLE automation_plans ADD COLUMN IF NOT EXISTS committed_on_revision TEXT;

-- D15: the sync-pending query also retries timed-out syncs, so its partial index must match
-- the query predicate exactly or the planner cannot use it.
DROP INDEX IF EXISTS idx_reco_pr_sync_queue;
CREATE INDEX IF NOT EXISTS idx_reco_pr_sync_queue_v2
    ON recommendation_pull_requests (status, id)
    WHERE status = 'merged'
      AND plan_id IS NOT NULL AND plan_id <> ''
      AND provider_id IS NOT NULL AND provider_id <> ''
      AND repo <> ''
      AND pr_number IS NOT NULL AND pr_number > 0
      AND merge_commit_sha IS NOT NULL AND merge_commit_sha <> ''
      AND (sync_status IS NULL OR sync_status NOT IN ('synced', 'failed')
           OR (sync_status = 'failed' AND sync_error_code = 'sync_timeout'));

-- D15: branch cleanup walks only pull-request rows whose plan branch is not cleaned yet.
ALTER TABLE recommendation_pull_requests ADD COLUMN IF NOT EXISTS branch_cleanup_completed_at TIMESTAMPTZ;
UPDATE recommendation_pull_requests pr
SET branch_cleanup_completed_at = cleanup.completed_at
FROM automation_branch_cleanup_completed cleanup
WHERE pr.branch_cleanup_completed_at IS NULL
  AND cleanup.plan_id = pr.plan_id
  AND cleanup.provider_id = pr.provider_id
  AND cleanup.repo = pr.repo
  AND cleanup.branch_name = pr.head_branch;
CREATE INDEX IF NOT EXISTS idx_reco_pr_branch_cleanup_pending
    ON recommendation_pull_requests (id)
    WHERE branch_cleanup_completed_at IS NULL
      AND plan_id IS NOT NULL AND plan_id <> ''
      AND head_branch IS NOT NULL AND head_branch <> '';

-- D15: plan listings by status, source and workload identity without full scans. Each
-- index ends with the listing order so no sort is needed.
CREATE INDEX IF NOT EXISTS idx_automation_plans_status_created
    ON automation_plans (status, created_at, plan_id);
CREATE INDEX IF NOT EXISTS idx_automation_plans_source_created
    ON automation_plans (source_id, created_at, plan_id);
CREATE INDEX IF NOT EXISTS idx_automation_plans_workload
    ON automation_plans (
        (source_snapshot->>'cluster'),
        (source_snapshot->>'namespace'),
        (source_snapshot->>'workload_kind'),
        (source_snapshot->>'workload_name'),
        created_at,
        plan_id
    );
