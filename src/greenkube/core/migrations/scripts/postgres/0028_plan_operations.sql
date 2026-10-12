-- 0028: Durable, digest-addressed plan operations (PostgreSQL).

CREATE TABLE IF NOT EXISTS automation_plan_operations (
    operation_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL UNIQUE REFERENCES automation_plans(plan_id) ON DELETE CASCADE,
    plan_digest TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'retrying', 'succeeded', 'failed', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    backoff_base_seconds INTEGER NOT NULL DEFAULT 30 CHECK (backoff_base_seconds >= 0),
    backoff_max_seconds INTEGER NOT NULL DEFAULT 3600
        CHECK (backoff_max_seconds >= backoff_base_seconds),
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    locked_at TIMESTAMPTZ,
    worker_id TEXT,
    lease_id TEXT UNIQUE,
    lease_expires_at TIMESTAMPTZ,
    last_error TEXT,
    provider_id TEXT,
    commit_sha TEXT,
    pull_request_number INTEGER CHECK (pull_request_number IS NULL OR pull_request_number > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        status != 'running'
        OR (worker_id IS NOT NULL AND lease_id IS NOT NULL AND lease_expires_at IS NOT NULL AND locked_at IS NOT NULL)
    ),
    CHECK (
        status = 'running'
        OR (lease_id IS NULL AND lease_expires_at IS NULL AND locked_at IS NULL)
    ),
    CHECK (attempts <= max_attempts)
);

CREATE INDEX IF NOT EXISTS idx_automation_plan_operations_claim
    ON automation_plan_operations (status, available_at, created_at);
CREATE INDEX IF NOT EXISTS idx_automation_plan_operations_expired_lease
    ON automation_plan_operations (status, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_automation_plan_operations_worker
    ON automation_plan_operations (worker_id, lease_expires_at);

ALTER TABLE automation_worker_heartbeats
    ADD COLUMN IF NOT EXISTS lease_plan_operation_id TEXT
    REFERENCES automation_plan_operations(operation_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_automation_worker_heartbeats_plan_lease
    ON automation_worker_heartbeats (lease_plan_operation_id);
