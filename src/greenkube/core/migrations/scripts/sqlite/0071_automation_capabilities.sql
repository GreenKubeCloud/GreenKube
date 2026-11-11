-- 0071: Always-on automation with capability activation (roadmap F12, F1, F2).
--
-- automation_capabilities holds the last result of the controller's
-- AutomationReadiness reconciler: one row per global capability (scope
-- 'global', subject = capability name) and one row per discovered repository
-- (scope 'repository', subject = repository URL). The API reads it for the
-- setup checklist and the repository suggestions, only the controller writes.
CREATE TABLE IF NOT EXISTS automation_capabilities (
    scope TEXT NOT NULL,
    subject TEXT NOT NULL,
    state TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, subject)
);

-- Repositories an administrator allowed from the suggestions (F2), in
-- addition to automation.repositoryAllowlist. The reconciler merges them into
-- the allowlist of the provider that serves the repository host.
CREATE TABLE IF NOT EXISTS automation_repository_allowlist (
    repo_url TEXT PRIMARY KEY,
    host TEXT NOT NULL,
    repository TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
