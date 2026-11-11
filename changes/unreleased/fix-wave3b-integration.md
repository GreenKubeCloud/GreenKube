### Added

- **Server-side plan filter for changes (J1):** `GET /api/v2/changes?plan_id=<plan>` returns the changes of one GitOps plan (migration `0078` indexes `change_events(plan_id)`, SQLite and PostgreSQL). The `/ledger?change=<plan_id>` and `/changes/{plan_id}` pages use it instead of scanning up to 1,000 recent changes.
- **Newer advice on open pull requests (J5, E7):** the PR status comment now also appears on an **open** plan PR when newer advice is queued behind its plan, so "Newer advice available" shows before the merge. Each open plan is checked at most every 15 minutes, on the same cycle and 25-writes cap as merged PRs; an open PR without newer advice gets no comment.
- **`renderer_egress_unenforced` on plan previews (F6):** while the readiness reconciler reports the renderer's egress canary unenforced, every Kustomize and Helm plan carries the warning in `file_plan.warnings`, bound by the plan digest. Raw-manifest plans never carry it.
- **Source provenance (G1, E6):** `consensus.source_ids` and the v2 field `source_ids` list every source id behind a recommendation, the chosen one first (`goldilocks`, `krr`, `kubecost`, `push:<client>`); `GET /api/v2/recommendations?source=goldilocks` matches them. The recommendation detail page shows them as badges.
- **Drift monitor Helm keys (C8):** `config.recommendations.changeMeasurement.driftThreshold` (`CHANGE_DRIFT_THRESHOLD`, default 0.5) and `driftSustainedDays` (`CHANGE_DRIFT_SUSTAINED_DAYS`, default 3), validated by the values schema.
- **Recommendation detail from `/api/v2` (K3, E7):** the page reads `GET /api/v2/recommendations/{id}` for the consensus votes, agreement, contested reasons, the current revision, queued newer advice and the revision history (falls back to the v1 record on older servers).
- **UI contract fixtures:** `tests/api/test_ui_contract_fixtures.py` generates the JSON of `GET /api/v1/setup/status` (incomplete, host mismatch, paused, complete), the repository suggestions, the allowlist answer and `GET /api/v2/recommendations/{id}` from the FastAPI app into `frontend/tests/fixtures/api`; the vitest suites use exactly these files.

### Changed

- **`automation.policy.missingEvidence` defaults to `fail` everywhere (D13):** with the profiles gone, Python fails closed too when `GITOPS_POLICY_JSON` leaves it unset (it meant `warn`); the chart's default now comes from the `greenkube.values` helper.
- **PR content through the action framework (H0 acceptance):** the PR Changes table, the rollback lines, the automated-rollback promise and the `optimization/<capability>` label come from `ActionKindSpec.pr_section()` (new structured `PrFieldChange` rows), `pr_label`, `pr_subject` and `supports_rollback()`; no `isinstance(op, SetContainerResources)` is left outside the `container_resources` spec. Container-resources PR bodies are byte-identical (golden files).
- **Push signals are capped in consensus (G6, E6):** one counted vote per push client whatever engine families it declares; a pushed signal never stands for another source's engine family; pushed signals alone never produce a deletion.
- **Setup UI follows the backend contract (F1, F2, F12):** the in-app actions are exactly the ids the server emits (`allowlist_repositories`, `open_sources`; a backend test keeps both lists equal), capability reasons such as `credentials_host_ambiguous` read as text, only `active` counts as active, and delivery paused by `pauseDelivery` reads "paused" on the setup page and in the banner.

### Fixed

- **Goldilocks and per-VPA-object signals never reached consensus (G1 x E6):** a built-in report's container signals that back no v1 recommendation (a Goldilocks VPA next to a user VPA, two VPAs that disagree, a VPA increase) were dropped before consensus, so a disputed container got no VPA vote at all. They now vote, once per engine family.
- **Consensus ignored the signal's `engine_family` (G1, G6):** only the algorithm name and parameters were read, so a pushed signal's declared engine was ignored.
- **Repository allowlist snippet never shown (F2):** the setup page read `snippet`, the API returns `values_snippet`.
- **Setup fix action `open_sources` rendered as plain text (F1):** the UI mapped ids the server never emits (`view_sources`, `allow_repositories`...).
