-- 0055: scoped API keys for machines (roadmap F15).
-- A key is shown once as gk_<id>_<secret>; only the SHA-256 of the secret is
-- stored. Additive and idempotent.

CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    prefix TEXT NOT NULL,
    secret_sha256 TEXT NOT NULL,
    scopes TEXT[] NOT NULL DEFAULT '{}',
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    last_used_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_api_keys_created_at ON api_keys (created_at);
