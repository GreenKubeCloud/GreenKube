-- 0066: signals emitted by sources (roadmap E4, E5).
-- One row per distinct signal: signal_id hashes the source, target, capability
-- and proposal, so a re-emitted signal only moves last_seen_at/last_run_id
-- forward and a changed proposal is a new row. "signal" is the full v2
-- SourceSignal JSON; recommendation_fingerprint links it to the v1
-- recommendation it produced. Rows not seen for 30 days are pruned.

CREATE TABLE IF NOT EXISTS source_signals (
    source_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    target_key TEXT NOT NULL,
    capability TEXT NOT NULL,
    kind TEXT NOT NULL,
    namespace TEXT,
    recommendation_fingerprint TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    last_run_id BIGINT,
    signal JSONB NOT NULL,
    PRIMARY KEY (source_id, signal_id)
);

CREATE INDEX IF NOT EXISTS idx_source_signals_source_seen ON source_signals (source_id, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_signals_target ON source_signals (target_key, capability);
CREATE INDEX IF NOT EXISTS idx_source_signals_fingerprint ON source_signals (recommendation_fingerprint);
CREATE INDEX IF NOT EXISTS idx_source_signals_last_seen ON source_signals (last_seen_at);
