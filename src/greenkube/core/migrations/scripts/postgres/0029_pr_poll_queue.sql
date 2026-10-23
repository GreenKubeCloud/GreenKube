-- 0029: Index the bounded, plan-linked open PR polling queue (PostgreSQL).

CREATE INDEX IF NOT EXISTS idx_reco_pr_poll_queue
    ON recommendation_pull_requests (id)
    WHERE status IN ('pending', 'open')
      AND plan_id IS NOT NULL AND plan_id <> ''
      AND provider_id IS NOT NULL AND provider_id <> ''
      AND repo <> ''
      AND pr_number IS NOT NULL AND pr_number > 0;

CREATE INDEX IF NOT EXISTS idx_reco_pr_provider_reference
    ON recommendation_pull_requests (provider_id, repo, pr_number)
    WHERE provider_id IS NOT NULL AND pr_number IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_reco_pr_sync_queue
    ON recommendation_pull_requests (id)
    WHERE status = 'merged'
      AND plan_id IS NOT NULL AND plan_id <> ''
      AND provider_id IS NOT NULL AND provider_id <> ''
      AND repo <> ''
      AND pr_number IS NOT NULL AND pr_number > 0
      AND merge_commit_sha IS NOT NULL AND merge_commit_sha <> ''
      AND (sync_status IS NULL OR sync_status NOT IN ('synced', 'failed'));
