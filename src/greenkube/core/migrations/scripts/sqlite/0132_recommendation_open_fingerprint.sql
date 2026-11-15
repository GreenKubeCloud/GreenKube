-- 0132: one open row per recommendation fingerprint (REC-06).
--
-- A scan used to insert ignored advice again as a new active row, so a
-- fingerprint could hold an ignored row and an active twin (and, once
-- restored, two active rows). Existing duplicates are merged first, each
-- retired row getting a stale event with the reason superseded_by_ignored:
-- an active row with an ignored or pr_open twin becomes stale (the user's
-- decision, or the pull request in flight, wins), an ignored row with a
-- pr_open twin becomes stale, and of several ignored or several pr_open rows
-- only the most recent stays. Then a partial unique index makes a duplicate
-- among active, ignored and pr_open rows impossible.
INSERT INTO recommendation_events (recommendation_id, event_type, actor, payload, created_at)
SELECT r.id, 'stale', 'system', '{"reason": "superseded_by_ignored"}',
       strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
FROM recommendation_history r
WHERE r.fingerprint IS NOT NULL AND (
    (r.status = 'active' AND EXISTS (
        SELECT 1 FROM recommendation_history o
        WHERE o.fingerprint = r.fingerprint AND o.id <> r.id AND o.status IN ('ignored', 'pr_open')))
    OR (r.status = 'ignored' AND EXISTS (
        SELECT 1 FROM recommendation_history o
        WHERE o.fingerprint = r.fingerprint AND o.id <> r.id AND o.status = 'pr_open'))
    OR (r.status IN ('ignored', 'pr_open') AND EXISTS (
        SELECT 1 FROM recommendation_history o
        WHERE o.fingerprint = r.fingerprint AND o.id > r.id AND o.status = r.status))
);

UPDATE recommendation_history
SET status = 'stale', updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE fingerprint IS NOT NULL AND (
    (status = 'active' AND EXISTS (
        SELECT 1 FROM recommendation_history o
        WHERE o.fingerprint = recommendation_history.fingerprint
          AND o.id <> recommendation_history.id AND o.status IN ('ignored', 'pr_open')))
    OR (status = 'ignored' AND EXISTS (
        SELECT 1 FROM recommendation_history o
        WHERE o.fingerprint = recommendation_history.fingerprint
          AND o.id <> recommendation_history.id AND o.status = 'pr_open'))
    OR (status IN ('ignored', 'pr_open') AND EXISTS (
        SELECT 1 FROM recommendation_history o
        WHERE o.fingerprint = recommendation_history.fingerprint
          AND o.id > recommendation_history.id AND o.status = recommendation_history.status))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_reco_open_fingerprint
    ON recommendation_history (fingerprint) WHERE status IN ('active', 'ignored', 'pr_open');
