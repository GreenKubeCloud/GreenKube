-- 0023: GitOps source units, workload bindings and binding candidates (PostgreSQL).

ALTER TABLE repository_bindings
    DROP CONSTRAINT IF EXISTS repository_bindings_status_check;
ALTER TABLE repository_bindings
    ADD CONSTRAINT repository_bindings_status_check
    CHECK (status IN ('resolved', 'ambiguous', 'unresolved', 'blocked', 'stale'));

CREATE TABLE IF NOT EXISTS workload_sources (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL REFERENCES gitops_providers(id) ON DELETE RESTRICT,
    repo_url TEXT NOT NULL,
    ref TEXT NOT NULL,
    revision TEXT,
    path TEXT NOT NULL,
    controller_kind TEXT NOT NULL,
    controller_name TEXT NOT NULL,
    controller_namespace TEXT NOT NULL,
    source_revision TEXT,
    renderer TEXT NOT NULL CHECK (renderer IN ('raw', 'kustomize', 'helm_values', 'helm_release')),
    renderer_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS workload_source_bindings (
    id BIGSERIAL PRIMARY KEY,
    cluster TEXT NOT NULL DEFAULT '',
    namespace TEXT NOT NULL,
    workload_kind TEXT NOT NULL,
    workload_name TEXT NOT NULL,
    source_id TEXT REFERENCES workload_sources(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (status IN ('resolved', 'ambiguous', 'unresolved', 'blocked', 'stale')),
    confidence DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    reason TEXT,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (cluster, namespace, workload_kind, workload_name)
);

CREATE TABLE IF NOT EXISTS workload_binding_candidates (
    binding_id BIGINT NOT NULL REFERENCES workload_source_bindings(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES workload_sources(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('resolved', 'ambiguous', 'unresolved', 'blocked', 'stale')),
    confidence DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    reason TEXT,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (binding_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_workload_sources_provider
    ON workload_sources (provider_id, repo_url);
CREATE INDEX IF NOT EXISTS idx_workload_source_bindings_workload
    ON workload_source_bindings (cluster, namespace, workload_kind, workload_name, status);
CREATE INDEX IF NOT EXISTS idx_workload_binding_candidates_source
    ON workload_binding_candidates (source_id);
