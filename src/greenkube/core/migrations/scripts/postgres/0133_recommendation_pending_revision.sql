-- 0133: confirmation memory of the revision hysteresis (IMP-13). Additive.
--
-- A rightsizing proposal that asks for a lower request than the stored advice,
-- beyond the hysteresis band, becomes a new revision only once consecutive
-- scans confirmed it. The JSON object {value, scans} holds the latest such
-- proposal and how many consecutive scans proposed it. NULL when nothing is
-- waiting, and for rows written before this version.
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS pending_revision TEXT;
