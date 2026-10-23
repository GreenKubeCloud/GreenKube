-- 0032: exact discovery generations and durable manual-binding audit events (SQLite).

ALTER TABLE workload_sources ADD COLUMN controller_uid TEXT;
ALTER TABLE workload_sources ADD COLUMN controller_generation INTEGER;
ALTER TABLE workload_sources ADD COLUMN source_uid TEXT;
ALTER TABLE workload_sources ADD COLUMN source_generation INTEGER;

CREATE TABLE IF NOT EXISTS workload_binding_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    cluster TEXT NOT NULL,
    namespace TEXT NOT NULL,
    workload_kind TEXT NOT NULL,
    workload_name TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'manual_resolution_created',
            'manual_resolution_expired',
            'manual_resolution_invalidated'
        )
    ),
    actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 80),
    source_id TEXT,
    source_generation TEXT,
    expires_at TEXT,
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workload_binding_events_workload
    ON workload_binding_events (cluster, namespace, workload_kind, workload_name, created_at, id);
