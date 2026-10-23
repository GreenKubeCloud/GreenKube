-- 0034: quarantine unexecutable legacy operations and link legacy PRs only
-- when recommendation, repository, provider and plan state identify one owner.

CREATE TABLE IF NOT EXISTS automation_state_migration_audit (
    migration_version INTEGER NOT NULL,
    entity_type TEXT NOT NULL CHECK (
        entity_type IN ('operation', 'pull_request', 'recommendation')
    ),
    entity_id TEXT NOT NULL CHECK (length(trim(entity_id)) > 0),
    recommendation_id INTEGER,
    disposition TEXT NOT NULL CHECK (
        disposition IN ('backfilled', 'quarantined', 'archived', 'released')
    ),
    reason_code TEXT NOT NULL CHECK (length(trim(reason_code)) > 0),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    previous_status TEXT,
    resulting_status TEXT,
    previous_error TEXT,
    previous_updated_at TIMESTAMPTZ,
    previous_plan_id TEXT,
    previous_provider_id TEXT,
    legacy_provider TEXT,
    legacy_repo TEXT,
    resolved_plan_id TEXT,
    resolved_provider_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (migration_version, entity_type, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_automation_state_migration_audit_recommendation
    ON automation_state_migration_audit (recommendation_id, migration_version);
CREATE INDEX IF NOT EXISTS idx_automation_state_migration_audit_disposition
    ON automation_state_migration_audit (entity_type, disposition, migration_version);

INSERT INTO automation_state_migration_audit (
    migration_version, entity_type, entity_id, recommendation_id,
    disposition, reason_code, reason, previous_status, resulting_status,
    previous_error, previous_updated_at
)
SELECT
    34,
    'operation',
    CAST(id AS TEXT),
    recommendation_id,
    CASE
        WHEN lower(status) IN ('succeeded', 'failed') THEN 'archived'
        ELSE 'quarantined'
    END,
    CASE
        WHEN lower(status) IN ('queued', 'running') THEN 'legacy_operation_requires_replanning'
        WHEN lower(status) IN ('succeeded', 'failed') THEN 'legacy_terminal_operation_retained'
        ELSE 'legacy_operation_status_unsupported'
    END,
    CASE
        WHEN lower(status) IN ('queued', 'running') THEN
            'Legacy queued or running operation has no immutable plan-bound snapshot and was not replayed. The original row remains in automation_operations for audit and must be resubmitted through the reviewed plan workflow.'
        WHEN lower(status) IN ('succeeded', 'failed') THEN
            'Terminal legacy operation remains in automation_operations as historical data and was not requeued.'
        ELSE
            'Legacy operation has a status unsupported by the plan-bound worker and was quarantined for operator review. The original row remains in automation_operations.'
    END,
    status,
    CASE WHEN lower(status) IN ('succeeded', 'failed') THEN status ELSE 'failed' END,
    error,
    updated_at
FROM automation_operations
ON CONFLICT (migration_version, entity_type, entity_id) DO NOTHING;

UPDATE automation_operations
SET status = 'failed',
    error = CASE
        WHEN error IS NULL OR trim(error) = '' THEN
            'Legacy operation quarantined by migration 0034, see automation_state_migration_audit.'
        ELSE
            error || ' | legacy operation quarantined by migration 0034, see automation_state_migration_audit.'
    END,
    updated_at = NOW()
WHERE lower(status) NOT IN ('succeeded', 'failed');

WITH source_url_parts AS (
    SELECT
        ws.id AS source_id,
        ws.provider_id,
        CASE
            WHEN ws.repo_url ~* '^[a-z][a-z0-9+.-]*://'
            THEN regexp_replace(ws.repo_url, '^[a-z][a-z0-9+.-]*://[^/]+/', '', 'i')
            WHEN ws.repo_url ~ '^[^/@:]+@[^/:]+:'
            THEN regexp_replace(ws.repo_url, '^[^:]+:', '')
            ELSE NULL
        END AS raw_repo_path,
        CASE
            WHEN ws.repo_url ~* '^[a-z][a-z0-9+.-]*://'
            THEN lower(regexp_replace(split_part(split_part(ws.repo_url, '://', 2), '/', 1), '^.*@', ''))
            WHEN ws.repo_url ~ '^[^/@:]+@[^/:]+:'
            THEN lower(split_part(split_part(ws.repo_url, '@', 2), ':', 1))
            ELSE NULL
        END AS repo_host
    FROM workload_sources ws
),
source_repositories AS (
    SELECT
        source_id,
        provider_id,
        lower(regexp_replace(rtrim(raw_repo_path, '/'), '\.git$', '', 'i')) AS repo_identity,
        repo_host
    FROM source_url_parts
    WHERE raw_repo_path IS NOT NULL
      AND trim(raw_repo_path) <> ''
      AND raw_repo_path NOT LIKE '%?%'
      AND raw_repo_path NOT LIKE '%#%'
),
pull_request_url_hosts AS (
    SELECT
        pr.id AS pr_id,
        CASE
            WHEN pr.pr_url ~* '^[a-z][a-z0-9+.-]*://[^/]+/'
            THEN lower(regexp_replace(split_part(split_part(pr.pr_url, '://', 2), '/', 1), '^.*@', ''))
            ELSE NULL
        END AS pr_host
    FROM recommendation_pull_requests pr
),
provider_candidates AS (
    SELECT DISTINCT
        pr.id AS pr_id,
        gp.id AS provider_id
    FROM recommendation_pull_requests pr
    JOIN source_repositories src
      ON src.repo_identity = lower(trim(pr.repo))
    JOIN gitops_providers gp
      ON gp.id = src.provider_id
    JOIN pull_request_url_hosts url
      ON url.pr_id = pr.id
    WHERE lower(trim(gp.provider_type)) = lower(trim(pr.provider))
      AND (pr.provider_id IS NULL OR trim(pr.provider_id) = '' OR pr.provider_id = gp.id)
      AND (
          (
              pr.pr_url IS NOT NULL
              AND trim(pr.pr_url) <> ''
              AND src.repo_host IS NOT NULL
              AND url.pr_host = src.repo_host
          )
          OR (
              (pr.pr_url IS NULL OR trim(pr.pr_url) = '')
              AND (
                  SELECT COUNT(*)
                  FROM gitops_providers provider_count
                  WHERE lower(trim(provider_count.provider_type)) = lower(trim(pr.provider))
              ) = 1
          )
      )
),
provider_resolution AS (
    SELECT
        pr_id,
        COUNT(*) AS candidate_count,
        MIN(provider_id) AS provider_id
    FROM provider_candidates
    GROUP BY pr_id
),
plan_candidates AS (
    SELECT DISTINCT
        pr.id AS pr_id,
        ap.plan_id,
        ap.provider_id
    FROM recommendation_pull_requests pr
    JOIN automation_plan_recommendations plan_recommendation
      ON plan_recommendation.recommendation_id = pr.recommendation_id
    JOIN automation_plans ap
      ON ap.plan_id = plan_recommendation.plan_id
    JOIN source_repositories src
      ON src.source_id = ap.source_id
     AND src.provider_id = ap.provider_id
     AND src.repo_identity = lower(trim(pr.repo))
    JOIN provider_candidates candidate_provider
      ON candidate_provider.pr_id = pr.id
     AND candidate_provider.provider_id = ap.provider_id
    WHERE (pr.plan_id IS NULL OR trim(pr.plan_id) = '' OR pr.plan_id = ap.plan_id)
      AND (
          (lower(trim(pr.status)) = 'open' AND ap.status = 'pr_open')
          OR (lower(trim(pr.status)) = 'closed' AND ap.status = 'closed')
          OR (
              lower(trim(pr.status)) = 'merged'
              AND ap.status IN ('merged', 'synced', 'applied', 'sync_failed')
          )
      )
),
plan_resolution AS (
    SELECT
        pr_id,
        COUNT(*) AS candidate_count,
        MIN(plan_id) AS plan_id,
        MIN(provider_id) AS provider_id
    FROM plan_candidates
    GROUP BY pr_id
),
resolution_counts AS (
    SELECT
        pr.id,
        pr.recommendation_id,
        pr.provider,
        pr.repo,
        pr.status,
        pr.error,
        pr.updated_at,
        pr.plan_id,
        pr.provider_id,
        COALESCE(provider_match.candidate_count, 0) AS provider_count,
        provider_match.provider_id AS candidate_provider_id,
        COALESCE(plan_match.candidate_count, 0) AS plan_count,
        plan_match.plan_id AS candidate_plan_id
    FROM recommendation_pull_requests pr
    LEFT JOIN provider_resolution provider_match
      ON provider_match.pr_id = pr.id
    LEFT JOIN plan_resolution plan_match
      ON plan_match.pr_id = pr.id
    WHERE (pr.plan_id IS NULL OR trim(pr.plan_id) = '')
       OR (pr.provider_id IS NULL OR trim(pr.provider_id) = '')
),
resolved_candidates AS (
    SELECT
        *,
        CASE WHEN provider_count = 1 THEN candidate_provider_id ELSE NULL END AS resolved_provider_id,
        CASE
            WHEN provider_count = 1 AND plan_count = 1 THEN candidate_plan_id
            ELSE NULL
        END AS resolved_plan_id,
        CASE
            WHEN provider_count = 1 AND plan_count = 1 THEN 'unique_plan_provider_match'
            WHEN NULLIF(trim(plan_id), '') IS NOT NULL AND plan_count <> 1
                THEN 'existing_plan_link_not_confirmed'
            WHEN NULLIF(trim(provider_id), '') IS NOT NULL AND provider_count <> 1
                THEN 'existing_provider_link_not_confirmed'
            WHEN provider_count = 0 THEN 'provider_not_safely_inferred'
            WHEN provider_count > 1 THEN 'ambiguous_provider'
            WHEN plan_count = 0 THEN 'plan_not_safely_inferred'
            WHEN plan_count > 1 THEN 'ambiguous_plan'
            ELSE 'incomplete_legacy_link'
        END AS reason_code
    FROM resolution_counts
),
pr_migration_decisions AS (
    SELECT
        *,
        CASE
            WHEN resolved_plan_id IS NOT NULL AND resolved_provider_id IS NOT NULL THEN 'backfilled'
            ELSE 'quarantined'
        END AS disposition,
        CASE
            WHEN resolved_plan_id IS NOT NULL AND resolved_provider_id IS NOT NULL THEN
                'The unique plan, provider, recommendation and exact repository identity matched the legacy pull request.'
            WHEN reason_code = 'ambiguous_provider' THEN
                'Multiple configured providers match this repository and provider type. Provider ownership was not guessed.'
            WHEN reason_code = 'ambiguous_plan' THEN
                'Multiple plans match this recommendation, provider and repository. Plan ownership was not guessed.'
            WHEN reason_code = 'provider_not_safely_inferred' THEN
                'No unique configured provider could be proven from the repository identity, provider type and pull-request host.'
            WHEN reason_code = 'plan_not_safely_inferred' THEN
                'No unique lifecycle-compatible plan is linked to this recommendation and repository.'
            WHEN reason_code = 'existing_plan_link_not_confirmed' THEN
                'The existing plan link could not be confirmed against the recommendation, provider, repository and PR lifecycle.'
            WHEN reason_code = 'existing_provider_link_not_confirmed' THEN
                'The existing provider link could not be confirmed against the provider type and repository identity.'
            ELSE
                'The legacy pull request does not have a unique, verifiable plan and provider owner.'
        END AS reason
    FROM resolved_candidates
)
INSERT INTO automation_state_migration_audit (
    migration_version, entity_type, entity_id, recommendation_id,
    disposition, reason_code, reason, previous_status, resulting_status,
    previous_error, previous_updated_at, previous_plan_id, previous_provider_id,
    legacy_provider, legacy_repo, resolved_plan_id, resolved_provider_id
)
SELECT
    34,
    'pull_request',
    CAST(id AS TEXT),
    recommendation_id,
    disposition,
    reason_code,
    reason,
    status,
    CASE
        WHEN disposition = 'quarantined' AND lower(trim(status)) IN ('pending', 'open') THEN 'error'
        ELSE status
    END,
    error,
    updated_at,
    plan_id,
    provider_id,
    provider,
    repo,
    resolved_plan_id,
    resolved_provider_id
FROM pr_migration_decisions
ON CONFLICT (migration_version, entity_type, entity_id) DO NOTHING;

UPDATE recommendation_pull_requests
SET plan_id = COALESCE(
        NULLIF(recommendation_pull_requests.plan_id, ''),
        (
            SELECT audit.resolved_plan_id
            FROM automation_state_migration_audit audit
            WHERE audit.migration_version = 34
              AND audit.entity_type = 'pull_request'
              AND audit.entity_id = CAST(recommendation_pull_requests.id AS TEXT)
        )
    ),
    provider_id = COALESCE(
        NULLIF(recommendation_pull_requests.provider_id, ''),
        (
            SELECT audit.resolved_provider_id
            FROM automation_state_migration_audit audit
            WHERE audit.migration_version = 34
              AND audit.entity_type = 'pull_request'
              AND audit.entity_id = CAST(recommendation_pull_requests.id AS TEXT)
        )
    )
WHERE EXISTS (
    SELECT 1
    FROM automation_state_migration_audit audit
    WHERE audit.migration_version = 34
      AND audit.entity_type = 'pull_request'
      AND audit.entity_id = CAST(recommendation_pull_requests.id AS TEXT)
      AND (audit.resolved_plan_id IS NOT NULL OR audit.resolved_provider_id IS NOT NULL)
);

UPDATE recommendation_pull_requests
SET status = 'error',
    error = CASE
        WHEN error IS NULL OR trim(error) = '' THEN
            'Legacy pull request quarantined by migration 0034 (' ||
            (SELECT audit.reason_code
             FROM automation_state_migration_audit audit
             WHERE audit.migration_version = 34
               AND audit.entity_type = 'pull_request'
               AND audit.entity_id = CAST(recommendation_pull_requests.id AS TEXT)) ||
            '): ' ||
            (SELECT audit.reason
             FROM automation_state_migration_audit audit
             WHERE audit.migration_version = 34
               AND audit.entity_type = 'pull_request'
               AND audit.entity_id = CAST(recommendation_pull_requests.id AS TEXT))
        ELSE
            error || ' | legacy pull request quarantined by migration 0034, see automation_state_migration_audit.'
    END,
    updated_at = NOW()
WHERE lower(trim(status)) IN ('pending', 'open')
  AND EXISTS (
      SELECT 1
      FROM automation_state_migration_audit audit
      WHERE audit.migration_version = 34
        AND audit.entity_type = 'pull_request'
        AND audit.entity_id = CAST(recommendation_pull_requests.id AS TEXT)
        AND audit.disposition = 'quarantined'
        AND lower(trim(audit.previous_status)) IN ('pending', 'open')
  );

INSERT INTO automation_state_migration_audit (
    migration_version, entity_type, entity_id, recommendation_id,
    disposition, reason_code, reason, previous_status, resulting_status
)
SELECT
    34,
    'recommendation',
    CAST(recommendation.id AS TEXT),
    recommendation.id,
    'released',
    'legacy_pr_quarantine_released_recommendation',
    'Legacy PR state was quarantined and no active PR or busy plan remains. Action status was released for reviewed replanning.',
    recommendation.action_status,
    'failed'
FROM recommendation_history recommendation
WHERE recommendation.action_status = 'pr_open'
  AND EXISTS (
      SELECT 1
      FROM automation_state_migration_audit audit
      WHERE audit.migration_version = 34
        AND audit.entity_type = 'pull_request'
        AND audit.recommendation_id = recommendation.id
        AND audit.disposition = 'quarantined'
  )
  AND NOT EXISTS (
      SELECT 1
      FROM recommendation_pull_requests pr
      WHERE pr.recommendation_id = recommendation.id
        AND lower(trim(pr.status)) IN ('pending', 'open')
  )
  AND NOT EXISTS (
      SELECT 1
      FROM automation_plan_recommendations link
      JOIN automation_plans plan ON plan.plan_id = link.plan_id
      WHERE link.recommendation_id = recommendation.id
        AND plan.status IN ('applying', 'pr_open', 'merged', 'synced', 'applied', 'sync_failed')
  )
ON CONFLICT (migration_version, entity_type, entity_id) DO NOTHING;

UPDATE recommendation_history
SET action_status = 'failed'
WHERE action_status = 'pr_open'
  AND EXISTS (
      SELECT 1
      FROM automation_state_migration_audit audit
      WHERE audit.migration_version = 34
        AND audit.entity_type = 'recommendation'
        AND audit.entity_id = CAST(recommendation_history.id AS TEXT)
        AND audit.disposition = 'released'
  );
