-- 0155: cost status of a collection step (COST-01).
-- opencost_pending marks a step whose rows carry a modeled cost because OpenCost could not price
-- the window (outage, lag, rejected request). The collection job re-prices such steps from
-- OpenCost once it answers again and clears the status. NULL for every other step.
ALTER TABLE collection_windows ADD COLUMN cost_status TEXT;
