-- 0132: one open row per recommendation fingerprint (REC-06).
--
-- A scan used to insert ignored advice again as a new active row, so a
-- fingerprint could hold an ignored row and an active twin (and, once
-- restored, two active rows). Existing duplicates are merged first, each
-- retired row becoming stale with a stale event: an active or ignored row
-- with a pr_open twin (reason superseded_by_pr, the pull request in flight
-- wins), an active row with an ignored twin (reason superseded_by_ignored,
-- the user's decision wins), of several ignored rows all but the one ignored
-- last (by ignored_at, then id) and of several pr_open rows all but the
-- newest (reason superseded_by_ignored and superseded_by_pr). Then a partial
-- unique index makes a duplicate among active, ignored and pr_open rows
-- impossible.
CREATE TEMP TABLE reco_0132_retired AS
SELECT r.id AS id,
    CASE
        WHEN r.status IN ('active', 'ignored') AND EXISTS (
            SELECT 1 FROM recommendation_history o
            WHERE o.fingerprint = r.fingerprint AND o.id <> r.id AND o.status = 'pr_open')
            THEN 'superseded_by_pr'
        WHEN r.status = 'pr_open' THEN 'superseded_by_pr'
        ELSE 'superseded_by_ignored'
    END AS reason
FROM recommendation_history r
WHERE r.fingerprint IS NOT NULL AND (
    (r.status IN ('active', 'ignored') AND EXISTS (
        SELECT 1 FROM recommendation_history o
        WHERE o.fingerprint = r.fingerprint AND o.id <> r.id AND o.status = 'pr_open'))
    OR (r.status = 'active' AND EXISTS (
        SELECT 1 FROM recommendation_history o
        WHERE o.fingerprint = r.fingerprint AND o.id <> r.id AND o.status = 'ignored'))
    OR (r.status = 'ignored' AND EXISTS (
        SELECT 1 FROM recommendation_history o
        WHERE o.fingerprint = r.fingerprint AND o.id <> r.id AND o.status = 'ignored'
          AND (COALESCE(o.ignored_at, '-infinity'::timestamptz) > COALESCE(r.ignored_at, '-infinity'::timestamptz)
               OR (o.ignored_at IS NOT DISTINCT FROM r.ignored_at AND o.id > r.id))))
    OR (r.status = 'pr_open' AND EXISTS (
        SELECT 1 FROM recommendation_history o
        WHERE o.fingerprint = r.fingerprint AND o.id > r.id AND o.status = 'pr_open'))
);

INSERT INTO recommendation_events (recommendation_id, event_type, actor, payload, created_at)
SELECT id, 'stale', 'system', '{"reason": "' || reason || '"}', NOW()
FROM reco_0132_retired;

UPDATE recommendation_history
SET status = 'stale', updated_at = NOW()
WHERE id IN (SELECT id FROM reco_0132_retired);

DROP TABLE reco_0132_retired;

CREATE UNIQUE INDEX IF NOT EXISTS idx_reco_open_fingerprint
    ON recommendation_history (fingerprint) WHERE status IN ('active', 'ignored', 'pr_open');
