-- 0070: inbox of pushed signals (roadmap G2, G6).
-- POST /api/v1/signals batches and POST /api/v1/sources/krr/reports are
-- stored here and replayed into each scan by the built-in "push" and "krr"
-- sources, so pushed advice goes through the source runner and consensus
-- like any other signal. source_inbox_signals keeps one row per source and
-- signal_id (a re-pushed signal is refreshed, never duplicated), and a
-- complete batch withdraws the source's signals it does not contain.
-- Rows not pushed again for 30 days are pruned.

CREATE TABLE IF NOT EXISTS source_inbox_batches (
    batch_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    complete INTEGER NOT NULL DEFAULT 0,
    signal_count INTEGER NOT NULL DEFAULT 0,
    withdrawn_count INTEGER NOT NULL DEFAULT 0,
    pushed_by TEXT,
    diagnostics TEXT
);

CREATE INDEX IF NOT EXISTS idx_source_inbox_batches_source ON source_inbox_batches (source_id, received_at);
CREATE INDEX IF NOT EXISTS idx_source_inbox_batches_received ON source_inbox_batches (received_at);

CREATE TABLE IF NOT EXISTS source_inbox_signals (
    source_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    namespace TEXT,
    received_at TEXT NOT NULL,
    signal TEXT NOT NULL,
    PRIMARY KEY (source_id, signal_id)
);

CREATE INDEX IF NOT EXISTS idx_source_inbox_signals_received ON source_inbox_signals (received_at);
