-- 0023: GitOps source units, workload bindings and binding candidates (SQLite).

CREATE TABLE IF NOT EXISTS repository_bindings_new_0023 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster TEXT NOT NULL DEFAULT '',
    namespace TEXT NOT NULL,
    workload_kind TEXT NOT NULL,
    workload_name TEXT NOT NULL,
    repo_url TEXT NOT NULL,
    path TEXT,
    branch TEXT,
    source TEXT NOT NULL CHECK (source IN ('argocd', 'flux', 'annotation')),
    priority INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('resolved', 'ambiguous', 'unresolved', 'blocked', 'stale')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence TEXT NOT NULL DEFAULT '{}',
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (cluster, namespace, workload_kind, workload_name, source, repo_url, path)
);

INSERT INTO repository_bindings_new_0023 (
    id, cluster, namespace, workload_kind, workload_name, repo_url, path, branch,
    source, priority, status, confidence, evidence, discovered_at, updated_at
)
SELECT
    id, cluster, namespace, workload_kind, workload_name, repo_url, path, branch,
    source, priority, status, confidence, evidence, discovered_at, updated_at
FROM repository_bindings;

INSERT INTO sqlite_sequence (name, seq)
SELECT 'repository_bindings_new_0023', seq
FROM sqlite_sequence
WHERE name = 'repository_bindings'
  AND NOT EXISTS (
      SELECT 1 FROM sqlite_sequence WHERE name = 'repository_bindings_new_0023'
  );

UPDATE sqlite_sequence
SET seq = MAX(seq, COALESCE(
    (SELECT seq FROM sqlite_sequence WHERE name = 'repository_bindings'),
    0
))
WHERE name = 'repository_bindings_new_0023';

DROP TABLE repository_bindings;
ALTER TABLE repository_bindings_new_0023 RENAME TO repository_bindings;

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
    renderer_config TEXT NOT NULL DEFAULT '{}',
    observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS workload_source_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster TEXT NOT NULL DEFAULT '',
    namespace TEXT NOT NULL,
    workload_kind TEXT NOT NULL,
    workload_name TEXT NOT NULL,
    source_id TEXT REFERENCES workload_sources(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (status IN ('resolved', 'ambiguous', 'unresolved', 'blocked', 'stale')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence TEXT NOT NULL DEFAULT '{}',
    reason TEXT,
    observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (cluster, namespace, workload_kind, workload_name)
);

CREATE TABLE IF NOT EXISTS workload_binding_candidates (
    binding_id INTEGER NOT NULL REFERENCES workload_source_bindings(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES workload_sources(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('resolved', 'ambiguous', 'unresolved', 'blocked', 'stale')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence TEXT NOT NULL DEFAULT '{}',
    reason TEXT,
    observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (binding_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_workload_sources_provider
    ON workload_sources (provider_id, repo_url);
CREATE INDEX IF NOT EXISTS idx_workload_source_bindings_workload
    ON workload_source_bindings (cluster, namespace, workload_kind, workload_name, status);
CREATE INDEX IF NOT EXISTS idx_workload_binding_candidates_source
    ON workload_binding_candidates (source_id);

CREATE INDEX IF NOT EXISTS idx_repository_bindings_workload
    ON repository_bindings (cluster, namespace, workload_kind, workload_name, priority DESC);
