-- 0058: risk-adjusted expected value on recommendations (PostgreSQL).
-- Stores the ValueScore (roadmap I1: value_annual, p_success, incident_cost,
-- effort_cost, ev and components) as JSON text, like the other JSON columns of
-- recommendation_history. Rows written before keep NULL and are scored when
-- read. Additive and idempotent.

ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS value_score TEXT;
