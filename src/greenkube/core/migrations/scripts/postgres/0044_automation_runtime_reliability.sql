-- 0044: automation runtime reliability (roadmap D2, D7, D11, D15).

-- D2: retention keeps a content-free summary when it prunes plan file contents.
ALTER TABLE automation_plans ADD COLUMN IF NOT EXISTS file_plan_summary JSONB;

-- D7: the branch head a plan was committed on when it moved after planning.
ALTER TABLE automation_plans ADD COLUMN IF NOT EXISTS committed_on_revision TEXT;
