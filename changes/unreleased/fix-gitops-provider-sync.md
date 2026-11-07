### Added

- Git providers expose `compare_trees`, `get_blob_ids`, `get_tree` and `is_ancestor` (GitHub compare, GitLab merge base, Gitea compare) plus the `commit_ancestry` capability (D1, D5).
- Per-operation Git provider call budget (200 requests per plan operation) and the metrics `greenkube_git_provider_requests_total`, `greenkube_git_provider_operation_calls` and `greenkube_git_provider_budget_exhausted_total` (D5, BUG-47).
- Sync reasons `sync_confirmed_descendant`, `revision_not_descendant` and `ancestry_unavailable` (D1).

### Changed

- Opening a plan PR compares the plan branch with its base commit by tree and blob IDs and reads only the planned paths: about 12–14 provider calls for a 5,000-file repository instead of ~6×(N+1) (D5, BUG-47).
- GitHub commits validate preconditions from one cached recursive tree and keep the original file mode; GitHub and Gitea branch creation tries the write first; Gitea stages the atomic commit with `new_branch` in the same call (D5).
- A synced plan records the observed controller revision as `sync_revision` and its confirmation reason in the plan audit trail (D1).
- Merged plans record which commit landed in their audit reason: `pull_request_merged`, `pull_request_merged_squash_commit` or `pull_request_merged_fast_forward_head` (D4).

### Fixed

- A controller that runs a later commit containing the merge (another PR, Renovate, image write-back) now confirms sync instead of failing with `revision_mismatch`/`sync_timeout`, so apply detection and savings run (D1, BUG-43).
- GitHub PRs closed without merging no longer keep their plan in `pr_open` forever because of GitHub's leftover test-merge SHA (D3, BUG-45).
- GitLab fast-forward and squash merges use the squash commit or the merge request head instead of failing sync with `merge_commit_sha_missing` (D4, BUG-46).
- Gitea branch creation reports a missing base branch instead of silently succeeding.
