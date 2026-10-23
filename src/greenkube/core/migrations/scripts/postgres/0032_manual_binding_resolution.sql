-- 0032: exact discovery generations and durable manual-binding audit events (PostgreSQL).

ALTER TABLE workload_sources ADD COLUMN IF NOT EXISTS controller_uid TEXT;
ALTER TABLE workload_sources ADD COLUMN IF NOT EXISTS controller_generation BIGINT
    CHECK (controller_generation IS NULL OR controller_generation > 0);
ALTER TABLE workload_sources ADD COLUMN IF NOT EXISTS source_uid TEXT;
ALTER TABLE workload_sources ADD COLUMN IF NOT EXISTS source_generation BIGINT
    CHECK (source_generation IS NULL OR source_generation > 0);

CREATE TABLE IF NOT EXISTS workload_binding_events (
    id BIGSERIAL PRIMARY KEY,
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
    actor TEXT NOT NULL CHECK (char_length(actor) BETWEEN 1 AND 80),
    source_id TEXT,
    source_generation TEXT,
    expires_at TIMESTAMPTZ,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workload_binding_events_workload
    ON workload_binding_events (cluster, namespace, workload_kind, workload_name, created_at, id);
