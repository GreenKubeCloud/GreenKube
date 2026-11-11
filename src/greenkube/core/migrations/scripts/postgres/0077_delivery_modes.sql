-- 0077: delivery modes (roadmap F4). Additive.
--
-- How an approved plan is delivered: "direct" (GreenKube opens the pull
-- request), "patch" (an operator applies the downloadable patch and opens it)
-- or "ci" (a CI job applies the patch with its own token and reports the pull
-- request back). Set when the plan is approved. NULL for plans approved before
-- this version, which were all delivered directly.
ALTER TABLE automation_plans ADD COLUMN IF NOT EXISTS delivery_mode TEXT;
