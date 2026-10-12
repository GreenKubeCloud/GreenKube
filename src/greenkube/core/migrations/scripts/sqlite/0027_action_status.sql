-- 0027: Separate recommendation action lifecycle from recommendation lifecycle (SQLite).

ALTER TABLE recommendation_history
    ADD COLUMN action_status TEXT NOT NULL DEFAULT 'none'
    CHECK (action_status IN (
        'none', 'grouped', 'plan_ready', 'pr_open', 'merged', 'sync_pending',
        'applied', 'closed', 'failed', 'reverted'
    ));

UPDATE recommendation_history
SET status = 'active',
    action_status = 'pr_open'
WHERE status = 'pr_open';

CREATE INDEX IF NOT EXISTS idx_reco_action_status
    ON recommendation_history (action_status);
