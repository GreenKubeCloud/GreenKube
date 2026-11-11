-- 0061: recommendations whose pull request is open, merged or syncing are
-- `pr_open` (they stayed `active`, so scans rewrote their baseline and
-- proposal while the PR was in flight). The GitOps action-status transition
-- now keeps both in step; this aligns the rows written before. Data only.
UPDATE recommendation_history
SET status = 'pr_open'
WHERE status = 'active'
  AND action_status IN ('pr_open', 'merged', 'sync_pending');
