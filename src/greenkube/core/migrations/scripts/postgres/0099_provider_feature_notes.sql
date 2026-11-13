-- 0099: Degraded best-effort features of a Git provider (automation follow-ups).
--
-- A verified provider whose token cannot apply labels, request reviewers,
-- post comments or commit statuses (HTTP 403 on those best-effort calls)
-- keeps its verified status. Each refused feature is recorded here instead,
-- and removed again after the next successful call of that feature.
CREATE TABLE IF NOT EXISTS gitops_provider_feature_notes (
    provider_id TEXT NOT NULL REFERENCES gitops_providers(id) ON DELETE CASCADE,
    feature TEXT NOT NULL,
    error_code TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (provider_id, feature)
);
