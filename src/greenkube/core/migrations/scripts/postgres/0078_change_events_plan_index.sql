-- 0078: index change events by plan (roadmap J1, C2) (PostgreSQL).
-- The ledger deep link /ledger?change=<plan_id> and the change page read the
-- change of one plan through GET /api/v2/changes?plan_id=..., so change_events
-- is read by plan_id alone. Additive.
-- Note: the runner splits scripts on semicolons, so comments must not use them.

CREATE INDEX IF NOT EXISTS idx_change_events_plan
    ON change_events(plan_id, created_at, change_id);
