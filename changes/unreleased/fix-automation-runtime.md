### Added
- **Plan cancel endpoint (D6):** `DELETE /api/v1/automation/plans/{plan_id}` cancels an unapproved preview. A draft or validated plan becomes `superseded` (`204`); an unknown plan returns `404` and any other status `409`.
- **`automation.policy.missingEvidence` (D13):** `fail` rejects a plan when discovery could not collect rollout, controller-health, HPA, VPA or PDB evidence; `warn` allows it with a warning in `policy_result.warnings`. The chart defaults to `fail` for `profile: production` and `warn` for `standalone`.
- **VPA policy gate (D13):** a VPA in `Auto`, `Recreate`, `InPlaceOrRecreate` or `InPlace` mode that targets the workload blocks the plan as a conflicting writer. `Initial` mode passes with a warning.
- **Kustomize rollback (D14):** rollback previews for Kustomize sources render through the sandboxed renderer instead of failing with `rollback_unsupported` (BUG-54).
- **Supervised automation loops (D9):** worker loops retry failed iterations with jittered backoff and error codes, and crashed loop tasks restart with backoff. `GET /api/v1/automation/status` reports `worker.inline_loop` (state, heartbeat age, restarts, last error code).
- **Plan revision tracking (D7):** plans record `committed_on_revision` separately from the planned `base_revision`.
- **SQLite transaction helper (D8):** `DatabaseManager.transaction()` commits on success and always rolls back on error, using a savepoint inside an enclosing unit of work. A lint test rejects bare `BEGIN` statements outside the transaction helpers.

### Changed
- **Preview supersede (D6):** previewing the same workload again supersedes its draft or validated plans instead of returning `409 WORKLOAD_PLAN_ALREADY_EXISTS` for 30 minutes (BUG-48).
- **Moved branch heads (D7):** approval and the worker accept a branch head that moved after planning when every touched file is unchanged; the commit is created on the moved head. Only a change to a touched file requires a new preview (BUG-49).
- **HPA gate per resource (D13):** an HPA blocks only the resource it scales on. A CPU-utilization HPA no longer blocks memory-only changes; a `ContainerResource` metric blocks only its container (BUG-55).
- **Readiness (D9):** `GET /api/v1/health` returns `503` (`degraded`) and automation status reports `ready: false` while the inline worker loop is down or its heartbeat is stale (BUG-51).
- **Unrelated pull-request webhooks (D11):** verified events for pull requests that GreenKube does not track are stored with the terminal status `ignored`, answered with `202`, never retried, and pruned by retention (BUG-53).
- **Query shapes (D15):** migration `0044` adds indexes for plan listings by status, source and workload identity, re-creates the sync-queue partial index to match its query, and tracks branch-cleanup completion on pull-request rows. Plan listings hydrate files and recommendation links in batched queries and accept a `limit`; the expiry sweep reads only due plans in bounded batches; policy cooldowns read workload history in one query (BUG-57).

### Fixed
- **Retention (D2):** pruning rendered file contents keeps a `file_plan_summary` (paths, original blob SHAs, content digests, diff statistics, validation, digest), so `GET /api/v1/automation/plans/{plan_id}` no longer returns `500` and new plans for the workload no longer fail with `policy_evidence_missing`. Retention never deletes `recommendation_events` (BUG-44).
- **SQLite connection poisoning (D8):** a compare-and-set conflict on a pull-request update no longer leaves the shared SQLite connection inside a transaction that broke every later write (BUG-50).
- **Lease loss after a PR was opened (D10):** a worker that reclaims an operation adopts the open PR on the plan's deterministic branch (`pr_open`) instead of marking the plan `stale` (BUG-52).
- **Render warnings (D12):** plan previews return the renderer's warnings (bounded and with credentials redacted) instead of `render-warning` placeholders (BUG-56).
- **`token_configured` (D12):** automation status reports `true` only when an enabled provider's credential Secret key resolves to a non-empty value, and reports the result per provider (BUG-56).
- **Plan insert race (D12):** `save_plan` inserts with compare-and-set, so a concurrent request for the same deterministic plan never overwrites a plan that already advanced (BUG-56).
- **Policy truthfulness (D13):** missing rollout or HPA evidence no longer passes silently as `not_available` (BUG-55).
