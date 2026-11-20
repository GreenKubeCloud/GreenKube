### Fixed

- GitLab: merge requests can be opened again. The atomic commit no longer sends `start_sha` for the existing plan branch, which GitLab refused ("A branch called ... already exists"), so every approved plan went `stale`. The branch head is checked before the commit, `last_commit_id` guards each edited file and the parent of the new commit is checked after it. The fake GitLab server used by the tests now refuses `start_sha` on an existing branch, as GitLab does.
- Argo CD: an Application that reads a Helm chart from an upstream Git repository (`path:`) and takes all its values files from a single `$ref` source now binds to that values source, not to the chart publisher's repository. A values file next to the chart keeps the chart source as the edit target.
- "Repositories found by discovery" no longer suggests a repository that only serves as a chart source (a Helm repository, or a Git chart repository whose values all live elsewhere).
