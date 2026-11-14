-- 0113: Per-record provider backoff for plan-linked pull requests.
--
-- PR polling, plan branch cleanup and the lifecycle comment retried every
-- record of a repository that left the provider allowlist (or whose
-- credentials no longer authorize it) on every cycle. Each record now keeps a
-- capped exponential backoff: the consecutive failure count, the time before
-- which it is not retried, and why it is detached (for example
-- repository_not_allowed). A successful provider call clears the three columns.
ALTER TABLE recommendation_pull_requests ADD COLUMN IF NOT EXISTS provider_retry_failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE recommendation_pull_requests ADD COLUMN IF NOT EXISTS provider_retry_at TIMESTAMPTZ;
ALTER TABLE recommendation_pull_requests ADD COLUMN IF NOT EXISTS provider_detached_reason TEXT;
