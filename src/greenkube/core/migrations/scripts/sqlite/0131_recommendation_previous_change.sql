-- 0131: link new advice to the applied change it follows (REC-01). Additive.
--
-- When a scan emits advice for a container whose rightsizing is applied and the
-- advice is not a restatement of that change (for example an OOM-driven
-- increase after a decrease), it is inserted as a new active recommendation
-- that names the applied recommendation and its recorded change. NULL for
-- rows written before this version and for advice that follows no change.
ALTER TABLE recommendation_history ADD COLUMN previous_recommendation_id INTEGER;
ALTER TABLE recommendation_history ADD COLUMN previous_change_id TEXT;
