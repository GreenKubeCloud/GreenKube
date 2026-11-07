-- 0038: per-source collection watermarks for gap-aware, idempotent metric collection (A2).
-- last_complete_end is the exclusive end of the last window whose rows were fully written.

CREATE TABLE IF NOT EXISTS collection_watermarks (
    cluster_uid TEXT NOT NULL,
    source TEXT NOT NULL,
    last_complete_end TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (cluster_uid, source)
);
