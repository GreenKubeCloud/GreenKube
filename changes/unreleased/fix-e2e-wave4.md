### Fixed

- **The Polaris job failed on every run (G4, e2e):** `polaris audit` 10.x lists ConfigMaps, ServiceAccounts, Roles, RoleBindings, ClusterRoles and ClusterRoleBindings for its built-in checks and exits on the first forbidden list, so the init container failed before any report was pushed. The job's ClusterRole now grants `get`/`list` on them (still no Secrets, no writes), and the upload strips a trailing newline from the API key Secret (a key stored with `--from-file` made `urllib` refuse the header). Checked on Minikube: the job pushed 726 hygiene findings.
