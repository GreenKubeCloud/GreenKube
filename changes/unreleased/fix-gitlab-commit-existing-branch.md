### Fixed

- GitLab: merge requests can be opened again. The atomic commit no longer sends `start_sha` for the existing plan branch, which GitLab refused ("A branch called ... already exists"), so every approved plan went `stale`. The branch head is checked before the commit, `last_commit_id` guards each edited file, and the parent of the new commit is checked after it. A response without `parent_ids` now fails closed as a conflict. The fake GitLab server used by the tests now behaves like GitLab: it refuses `start_sha` on an existing branch, tracks the last commit of each file, and rejects stale `last_commit_id`, creating a file that exists, and updating or deleting a missing file.
- Argo CD: an Application that reads a Helm chart from an upstream Git repository (`path:`) and takes all its values files from a single `$ref` source in another repository now binds to that values repository, not to the chart publisher's repository. A values file next to the chart, or a `$ref` source in the same repository (monorepo), keeps the chart source as the edit target.
- "Repositories found by discovery" no longer suggests a repository that only serves as a chart source (a Helm repository, or a Git chart repository whose values all live elsewhere).

### Changed

- Known limitation: a Helm chart read from another Git repository than its values cannot be planned yet. Its workload is `blocked` with the reason `cross-repository-git-chart` (shown in the API and on the Automation page) instead of being offered as actionable.
