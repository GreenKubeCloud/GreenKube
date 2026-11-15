-- 0124: record a refused cluster identity (review of COL-02).
-- mismatch_uid is the kube-system UID of the last controller that refused to collect into this
-- database because it runs in another cluster, so the API and alerts can show it. NULL when none.

ALTER TABLE installation ADD COLUMN mismatch_uid TEXT;
ALTER TABLE installation ADD COLUMN mismatch_seen_at TEXT;
