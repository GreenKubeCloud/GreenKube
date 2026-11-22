-- 0220: user-cancelled plans and the reason of the last plan status change (F-10). Additive.
--
-- POST /automation/plans/{id}/cancel used to mark a plan "stale" and its reason
-- "cancelled_by_user" was only kept in the recommendation audit trail. The plan
-- now ends in "cancelled" and status_reason keeps the reason code of the last
-- status change. Existing rows keep their status, status_reason stays NULL.

ALTER TABLE automation_plans ADD COLUMN IF NOT EXISTS status_reason TEXT;

ALTER TABLE automation_plans
    DROP CONSTRAINT IF EXISTS automation_plans_status_check;
ALTER TABLE automation_plans
    ADD CONSTRAINT automation_plans_status_check
    CHECK (status IN (
        'draft', 'validated', 'approved', 'applying', 'pr_open', 'merged',
        'synced', 'applied', 'sync_failed', 'closed', 'stale', 'failed',
        'needs_mapping', 'ambiguous', 'rejected', 'superseded', 'cancelled'
    ));
