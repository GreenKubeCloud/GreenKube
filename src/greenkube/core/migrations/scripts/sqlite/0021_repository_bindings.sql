CREATE TABLE IF NOT EXISTS repository_bindings (
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
    status TEXT NOT NULL CHECK (status IN ('resolved', 'ambiguous', 'unresolved')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence TEXT NOT NULL DEFAULT '{}',
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (cluster, namespace, workload_kind, workload_name, source, repo_url, path)
);

CREATE INDEX IF NOT EXISTS idx_repository_bindings_workload
    ON repository_bindings (cluster, namespace, workload_kind, workload_name, priority DESC);
