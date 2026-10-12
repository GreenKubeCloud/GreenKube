-- 0022: Git provider registry and repository allowlists (SQLite).
-- Credentials are referenced through Kubernetes Secrets and secret values are never stored here.

CREATE TABLE IF NOT EXISTS gitops_providers (
    id TEXT PRIMARY KEY,
    provider_type TEXT NOT NULL,
    api_base_url TEXT,
    secret_namespace TEXT,
    secret_name TEXT,
    secret_key TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    capabilities TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'unverified',
    last_checked_at TEXT,
    last_error_code TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK ((secret_name IS NULL) = (secret_key IS NULL)),
    CHECK (secret_namespace IS NULL OR secret_name IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS gitops_provider_allowlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL REFERENCES gitops_providers(id) ON DELETE CASCADE,
    repository_pattern TEXT NOT NULL CHECK (length(trim(repository_pattern)) > 0),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (provider_id, repository_pattern)
);

CREATE INDEX IF NOT EXISTS idx_gitops_provider_allowlists_provider
    ON gitops_provider_allowlists (provider_id, repository_pattern);
