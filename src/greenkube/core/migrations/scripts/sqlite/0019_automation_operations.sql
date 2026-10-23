-- 0019: Durable automation queue and outbox state.
CREATE TABLE IF NOT EXISTS automation_operations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    recommendation_id INTEGER NOT NULL,
    idempotency_key  TEXT NOT NULL UNIQUE,
    fingerprint      TEXT NOT NULL,
    request_json     TEXT NOT NULL,
    actor            TEXT NOT NULL DEFAULT 'user',
    status           TEXT NOT NULL DEFAULT 'queued',
    preview_digest   TEXT,
    commit_digest    TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    available_at     TEXT NOT NULL DEFAULT (datetime('now')),
    locked_at        TEXT,
    error            TEXT,
    result_json      TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_automation_operations_claim
    ON automation_operations (status, available_at, created_at);
