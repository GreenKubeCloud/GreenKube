-- 0026: Automation worker heartbeat and operation lease state (PostgreSQL).

CREATE TABLE IF NOT EXISTS automation_worker_heartbeats (
    worker_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'starting',
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_id TEXT,
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    lease_operation_id INTEGER REFERENCES automation_operations(id) ON DELETE SET NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_automation_worker_heartbeats_heartbeat
    ON automation_worker_heartbeats (heartbeat_at);
CREATE INDEX IF NOT EXISTS idx_automation_worker_heartbeats_lease
    ON automation_worker_heartbeats (lease_expires_at);
