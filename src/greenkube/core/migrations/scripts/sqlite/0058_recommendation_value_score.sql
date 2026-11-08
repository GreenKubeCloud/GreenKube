-- 0058: risk-adjusted expected value on recommendations (SQLite).
-- Stores the ValueScore (roadmap I1: value_annual, p_success, incident_cost,
-- effort_cost, ev and components) as JSON text. Rows written before keep NULL
-- and are scored when read. Additive: the runner tolerates re-runs.
-- Note: the runner splits scripts on semicolons, so comments must not use them.

ALTER TABLE recommendation_history ADD COLUMN value_score TEXT;
