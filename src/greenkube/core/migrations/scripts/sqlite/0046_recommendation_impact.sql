-- 0046: Impact Model projection on recommendations (SQLite).
-- Stores the projected ImpactDelta (roadmap B4) as JSON text. Rows written
-- before the Impact Model keep NULL. Additive: the runner tolerates re-runs.
-- Note: the runner splits scripts on semicolons, so comments must not use them.

ALTER TABLE recommendation_history ADD COLUMN impact TEXT;
