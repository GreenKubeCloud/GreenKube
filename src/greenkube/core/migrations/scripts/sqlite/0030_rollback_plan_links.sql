-- 0030: Persist the original plan referenced by a validated rollback plan (SQLite).

ALTER TABLE automation_plans
    ADD COLUMN rollback_of_plan_id TEXT
    REFERENCES automation_plans(plan_id) ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS idx_automation_plans_rollback_of
    ON automation_plans (rollback_of_plan_id);
