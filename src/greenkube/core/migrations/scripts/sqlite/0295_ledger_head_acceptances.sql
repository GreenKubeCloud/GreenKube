-- 0295: Audit trail of ledger heads accepted by an operator (LED-05).
--
-- greenkube db ledger-accept-head replaces the anchor of a chain that lost
-- entries (restore, failover). Each acceptance is kept here with its reason
-- and the head it replaced. The table is only appended to.
CREATE TABLE IF NOT EXISTS ledger_head_acceptances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_uid TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    previous_head_seq BIGINT,
    previous_head_hash TEXT,
    head_seq BIGINT,
    head_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_head_acceptances_cluster ON ledger_head_acceptances (cluster_uid, accepted_at);
