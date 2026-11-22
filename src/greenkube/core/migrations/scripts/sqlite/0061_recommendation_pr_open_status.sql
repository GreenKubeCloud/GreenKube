-- 0061 (data only): recommendations whose pull request is open, merged or
-- syncing are `pr_open` (they stayed `active`, so scans rewrote their
-- baseline and proposal while the PR was in flight). The GitOps action-status
-- transition now keeps both in step, and this aligns the rows written before.
UPDATE recommendation_history
SET status = 'pr_open'
WHERE status = 'active'
  AND action_status IN ('pr_open', 'merged', 'sync_pending');

-- Repair: before this release a scan that regenerated an applied rightsizing
-- copied the request it read from the lagging hourly rollup (the replaced
-- value) into actual_* and recomputed the savings from it, so merged pull
-- requests showed the baseline as the value after the change (400m -> 400m)
-- with zero savings. The value after those changes is unknown: clear it, and
-- the savings zeroed from it, instead of claiming the change did nothing.
-- Changes detected outside a pull request (method 'detected') are left alone.
UPDATE recommendation_history
SET actual_cpu_request_millicores = NULL,
    carbon_saved_co2e_grams = CASE WHEN carbon_saved_co2e_grams = 0 THEN NULL ELSE carbon_saved_co2e_grams END,
    cost_saved = CASE WHEN cost_saved = 0 THEN NULL ELSE cost_saved END
WHERE type = 'RIGHTSIZING_CPU'
  AND status IN ('applied', 'verifying', 'verified', 'rollback_review', 'regressed')
  AND application_method = 'pr_merge'
  AND actual_cpu_request_millicores = current_cpu_request_millicores
  AND recommended_cpu_request_millicores <> current_cpu_request_millicores;

UPDATE recommendation_history
SET actual_memory_request_bytes = NULL,
    carbon_saved_co2e_grams = CASE WHEN carbon_saved_co2e_grams = 0 THEN NULL ELSE carbon_saved_co2e_grams END,
    cost_saved = CASE WHEN cost_saved = 0 THEN NULL ELSE cost_saved END
WHERE type = 'RIGHTSIZING_MEMORY'
  AND status IN ('applied', 'verifying', 'verified', 'rollback_review', 'regressed')
  AND application_method = 'pr_merge'
  AND actual_memory_request_bytes = current_memory_request_bytes
  AND recommended_memory_request_bytes <> current_memory_request_bytes;
