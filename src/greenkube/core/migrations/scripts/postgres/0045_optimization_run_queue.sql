-- 0045: optimization runs become the scan request queue (E1) and keep
-- per-source health plus the diff against the previous active set (E8, E10).
-- A queued run has not started yet, so started_at becomes nullable and
-- requested_at carries the queue order. Existing rows are tagged 'legacy'.

ALTER TABLE optimization_runs ALTER COLUMN started_at DROP NOT NULL;
ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS trigger_type TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE optimization_runs ALTER COLUMN trigger_type SET DEFAULT 'direct';
ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS requested_by TEXT;
ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS requested_at TIMESTAMPTZ;
UPDATE optimization_runs SET requested_at = started_at WHERE requested_at IS NULL;
ALTER TABLE optimization_runs ALTER COLUMN requested_at SET DEFAULT NOW();
ALTER TABLE optimization_runs ALTER COLUMN requested_at SET NOT NULL;
ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS claimed_by TEXT;
ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS new_count INTEGER;
ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS changed_count INTEGER;
ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS resolved_count INTEGER;
ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS unchanged_count INTEGER;
ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS source_outcomes JSONB;
ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS diff JSONB;

CREATE INDEX IF NOT EXISTS idx_optimization_runs_status_requested
    ON optimization_runs (status, requested_at, id);

CREATE INDEX IF NOT EXISTS idx_optimization_runs_scope_requested
    ON optimization_runs (namespace, requested_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_optimization_runs_pending_scope
    ON optimization_runs ((COALESCE(namespace, ''))) WHERE status = 'queued';
