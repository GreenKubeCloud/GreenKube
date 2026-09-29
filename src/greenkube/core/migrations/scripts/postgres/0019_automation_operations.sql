-- 0019: Durable automation queue and outbox state.
CREATE TABLE IF NOT EXISTS automation_operations (
    id                BIGSERIAL PRIMARY KEY,
    recommendation_id INTEGER NOT NULL,
    idempotency_key   TEXT NOT NULL UNIQUE,
    fingerprint       TEXT NOT NULL,
    request_json      JSONB NOT NULL,
    actor             TEXT NOT NULL DEFAULT 'user',
    status            TEXT NOT NULL DEFAULT 'queued',
    preview_digest    TEXT,
    commit_digest     TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    available_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    locked_at         TIMESTAMPTZ,
    error             TEXT,
    result_json       JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_automation_operations_claim
    ON automation_operations (status, available_at, created_at);
