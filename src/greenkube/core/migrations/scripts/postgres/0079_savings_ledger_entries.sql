-- 0079: append-only savings ledger v2 (roadmap C6, C7, N9, BUG-39) (PostgreSQL).
-- savings_ledger_entries is INSERT-only: triggers reject UPDATE, DELETE and TRUNCATE.
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
-- Note: the runner sends this script in one call, comments must not use semicolons.
CREATE TABLE IF NOT EXISTS savings_ledger_entries (
    entry_id TEXT PRIMARY KEY,
    cluster_uid TEXT NOT NULL,
    cluster_name TEXT NOT NULL,
    seq BIGINT NOT NULL,
    entry_key TEXT NOT NULL,
    group_key TEXT NOT NULL,
    entry_kind TEXT NOT NULL CHECK (entry_kind IN ('accrual', 'reversal', 'restatement')),
    book TEXT NOT NULL CHECK (book IN ('accrued', 'restatement')),
    reason TEXT NOT NULL,
    method TEXT NOT NULL CHECK (method IN ('projected', 'measured_allocated', 'realized_billed', 'user_declared')),
    quantity TEXT NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    unit TEXT NOT NULL,
    currency TEXT,
    recommendation_id BIGINT,
    recommendation_ids TEXT NOT NULL DEFAULT '[]',
    namespace TEXT NOT NULL DEFAULT '',
    recommendation_type TEXT NOT NULL DEFAULT '',
    change_id TEXT,
    measurement_id TEXT,
    pr_url TEXT,
    price_card_version TEXT,
    intensity_g_per_kwh DOUBLE PRECISION,
    intensity_source TEXT,
    method_version TEXT NOT NULL,
    confidence DOUBLE PRECISION,
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    slot_start TIMESTAMPTZ,
    reverses_entry_id TEXT,
    restates_entry_id TEXT,
    source_table TEXT,
    source_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL,
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

CREATE OR REPLACE FUNCTION greenkube_reject_ledger_entry_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'savings_ledger_entries rows are append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_savings_ledger_entries_append_only ON savings_ledger_entries;
CREATE TRIGGER trg_savings_ledger_entries_append_only
    BEFORE UPDATE OR DELETE ON savings_ledger_entries
    FOR EACH ROW EXECUTE FUNCTION greenkube_reject_ledger_entry_mutation();

DROP TRIGGER IF EXISTS trg_savings_ledger_entries_no_truncate ON savings_ledger_entries;
CREATE TRIGGER trg_savings_ledger_entries_no_truncate
    BEFORE TRUNCATE ON savings_ledger_entries
    FOR EACH STATEMENT EXECUTE FUNCTION greenkube_reject_ledger_entry_mutation();

CREATE TABLE IF NOT EXISTS ledger_projection_watermarks (
    cluster_uid TEXT NOT NULL,
    source_table TEXT NOT NULL,
    position BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (cluster_uid, source_table)
);

CREATE TABLE IF NOT EXISTS ledger_chain_status (
    cluster_uid TEXT PRIMARY KEY,
    verified_at TIMESTAMPTZ NOT NULL,
    ok BOOLEAN NOT NULL,
    entries BIGINT NOT NULL,
    head_seq BIGINT,
    head_hash TEXT,
    failure TEXT
);

CREATE TABLE IF NOT EXISTS cluster_identities (
    cluster_name TEXT PRIMARY KEY,
    cluster_uid TEXT NOT NULL,
    source TEXT NOT NULL,
    resolved_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE change_events ADD COLUMN IF NOT EXISTS cluster_uid TEXT;

CREATE INDEX IF NOT EXISTS idx_savings_ledger_superseded
    ON recommendation_savings_ledger(recommendation_id) WHERE superseded;

DROP VIEW IF EXISTS savings_ledger_daily_rollup;
CREATE VIEW savings_ledger_daily_rollup AS
SELECT
    cluster_uid,
    cluster_name,
    (date_trunc('day', period_start AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') AS day,
    namespace,
    method,
    quantity,
    unit,
    book,
    entry_kind,
    SUM(value) AS value,
    COUNT(*) AS entries
FROM savings_ledger_entries
GROUP BY
    cluster_uid,
    cluster_name,
    (date_trunc('day', period_start AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'),
    namespace,
    method,
    quantity,
    unit,
    book,
    entry_kind;
