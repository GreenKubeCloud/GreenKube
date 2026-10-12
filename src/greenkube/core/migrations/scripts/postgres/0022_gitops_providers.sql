-- 0022: Git provider registry and repository allowlists (PostgreSQL).
-- Credentials are referenced through Kubernetes Secrets and secret values are never stored here.

CREATE TABLE IF NOT EXISTS gitops_providers (
    id TEXT PRIMARY KEY,
    provider_type TEXT NOT NULL,
    api_base_url TEXT,
    secret_namespace TEXT,
    secret_name TEXT,
    secret_key TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'unverified',
    last_checked_at TIMESTAMPTZ,
    last_error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK ((secret_name IS NULL) = (secret_key IS NULL)),
    CHECK (secret_namespace IS NULL OR secret_name IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS gitops_provider_allowlists (
    id BIGSERIAL PRIMARY KEY,
    provider_id TEXT NOT NULL REFERENCES gitops_providers(id) ON DELETE CASCADE,
    repository_pattern TEXT NOT NULL CHECK (length(trim(repository_pattern)) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (provider_id, repository_pattern)
);

CREATE INDEX IF NOT EXISTS idx_gitops_provider_allowlists_provider
    ON gitops_provider_allowlists (provider_id, repository_pattern);
