-- 0079: append-only savings ledger v2 (roadmap C6, C7, N9, BUG-39).
-- savings_ledger_entries is INSERT-only: triggers reject UPDATE and DELETE.
-- Every entry is chained per cluster: seq is a gap-free position and
-- hash = sha256(prev_hash || canonical JSON of the entry), so a modified,
-- removed or reordered row is detected by the chain verification loop.
-- entry_kind is accrual, reversal or restatement. book separates what was
-- accrued (book accrued, reconciles with recommendation_savings_ledger) from
-- the restatement of projected accruals by measured ones (book restatement).
-- The v1 ledger keeps being written: entries are projected from it by the
-- ledger projector, which records its position in ledger_projection_watermarks.
-- cluster_identities stores the kube-system namespace UID next to CLUSTER_NAME
-- (first resolution wins, so the chain key never changes).
CREATE TABLE IF NOT EXISTS savings_ledger_entries (
    entry_id TEXT PRIMARY KEY,
    cluster_uid TEXT NOT NULL,
    cluster_name TEXT NOT NULL,
    seq INTEGER NOT NULL,
    entry_key TEXT NOT NULL,
    group_key TEXT NOT NULL,
    entry_kind TEXT NOT NULL CHECK (entry_kind IN ('accrual', 'reversal', 'restatement')),
    book TEXT NOT NULL CHECK (book IN ('accrued', 'restatement')),
    reason TEXT NOT NULL,
    method TEXT NOT NULL CHECK (method IN ('projected', 'measured_allocated', 'realized_billed', 'user_declared')),
    quantity TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    currency TEXT,
    recommendation_id INTEGER,
    recommendation_ids TEXT NOT NULL DEFAULT '[]',
    namespace TEXT NOT NULL DEFAULT '',
    recommendation_type TEXT NOT NULL DEFAULT '',
    change_id TEXT,
    measurement_id TEXT,
    pr_url TEXT,
    price_card_version TEXT,
    intensity_g_per_kwh REAL,
    intensity_source TEXT,
    method_version TEXT NOT NULL,
    confidence REAL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    slot_start TEXT,
    reverses_entry_id TEXT,
    restates_entry_id TEXT,
    source_table TEXT,
    source_id INTEGER,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    prev_hash TEXT,
    hash TEXT NOT NULL,
    UNIQUE (cluster_uid, seq),
    UNIQUE (cluster_uid, entry_key)
);
CREATE INDEX IF NOT EXISTS idx_ledger_entries_recommendation
    ON savings_ledger_entries(recommendation_id, method, entry_kind);
CREATE INDEX IF NOT EXISTS idx_ledger_entries_reverses ON savings_ledger_entries(reverses_entry_id);
CREATE INDEX IF NOT EXISTS idx_ledger_entries_restates ON savings_ledger_entries(restates_entry_id);
CREATE INDEX IF NOT EXISTS idx_ledger_entries_source ON savings_ledger_entries(source_table, source_id);
CREATE INDEX IF NOT EXISTS idx_ledger_entries_change ON savings_ledger_entries(change_id);
CREATE INDEX IF NOT EXISTS idx_ledger_entries_period ON savings_ledger_entries(cluster_uid, period_start);
CREATE TRIGGER IF NOT EXISTS trg_savings_ledger_entries_no_update
BEFORE UPDATE ON savings_ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'savings_ledger_entries rows are append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_savings_ledger_entries_no_delete
BEFORE DELETE ON savings_ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'savings_ledger_entries rows are append-only');
END;

CREATE TABLE IF NOT EXISTS ledger_projection_watermarks (
    cluster_uid TEXT NOT NULL,
    source_table TEXT NOT NULL,
    position INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (cluster_uid, source_table)
);

CREATE TABLE IF NOT EXISTS ledger_chain_status (
    cluster_uid TEXT PRIMARY KEY,
    verified_at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    entries INTEGER NOT NULL,
    head_seq INTEGER,
    head_hash TEXT,
    failure TEXT
);

CREATE TABLE IF NOT EXISTS cluster_identities (
    cluster_name TEXT PRIMARY KEY,
    cluster_uid TEXT NOT NULL,
    source TEXT NOT NULL,
    resolved_at TEXT NOT NULL
);

ALTER TABLE change_events ADD COLUMN cluster_uid TEXT;

CREATE INDEX IF NOT EXISTS idx_savings_ledger_superseded
    ON recommendation_savings_ledger(recommendation_id) WHERE superseded = 1;

DROP VIEW IF EXISTS savings_ledger_daily_rollup;
CREATE VIEW savings_ledger_daily_rollup AS
SELECT
    cluster_uid,
    cluster_name,
    substr(period_start, 1, 10) AS day,
    namespace,
    method,
    quantity,
    unit,
    book,
    entry_kind,
    SUM(value) AS value,
    COUNT(*) AS entries
FROM savings_ledger_entries
GROUP BY cluster_uid, cluster_name, substr(period_start, 1, 10), namespace, method, quantity, unit, book, entry_kind;
