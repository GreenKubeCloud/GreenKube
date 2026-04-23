-- 0006: Recommendation lifecycle columns (PostgreSQL)
-- Adds status tracking and applied/ignored audit fields to the
-- recommendation_history table, plus a unique key for upsert semantics.
-- Recommended CPU/memory values are clamped by the engine to configured
-- minimums, so no separate "unrealistic" flag is needed.

ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS applied_at TIMESTAMPTZ;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS actual_cpu_request_millicores INTEGER;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS actual_memory_request_bytes BIGINT;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS carbon_saved_co2e_grams DOUBLE PRECISION;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS cost_saved DOUBLE PRECISION;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS ignored_at TIMESTAMPTZ;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS ignored_reason TEXT;
ALTER TABLE recommendation_history ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;

-- Deduplicate existing active rows: keep only the most recent per (pod_name, namespace, type).
-- This is required before the partial unique index can be created.
DELETE FROM recommendation_history
WHERE status = 'active'
  AND id NOT IN (
      SELECT DISTINCT ON (pod_name, namespace, type) id
      FROM recommendation_history
      WHERE status = 'active'
      ORDER BY pod_name, namespace, type, created_at DESC NULLS LAST, id DESC
  );

-- Unique constraint used for upsert: one active recommendation per (pod, namespace, type).
-- Uses a partial index so multiple historical records (applied/ignored) can coexist.
CREATE UNIQUE INDEX IF NOT EXISTS idx_reco_active_key
    ON recommendation_history (pod_name, namespace, type)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_reco_status ON recommendation_history(status);
CREATE INDEX IF NOT EXISTS idx_reco_ns_status ON recommendation_history(namespace, status);
