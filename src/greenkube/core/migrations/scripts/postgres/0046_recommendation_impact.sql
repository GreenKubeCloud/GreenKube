-- 0046: Impact Model projection on recommendations (PostgreSQL).
-- Stores the projected ImpactDelta (roadmap B4) as JSON text, like the other
-- JSON columns of recommendation_history. Rows written before the Impact Model
-- keep NULL. Additive and idempotent.

ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS impact TEXT;
