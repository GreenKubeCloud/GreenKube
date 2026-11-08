### Added
- **Deprecation headers on legacy automation routes (M4):** `POST /recommendations/{id}/apply-pr`, `GET /recommendations/{id}/apply-pr/eligibility` and `GET /automation/operations/{operation_id}` keep working this release but answer with `Deprecation: @1796947200` (2026-12-11), `Sunset: Fri, 29 Jan 2027 00:00:00 GMT` and `Link: </api/v1/automation/plans>; rel="successor-version"`, errors included, and OpenAPI marks them `deprecated`.

### Changed
- **One helper per concept (M6):** a single UTC normalizer (`greenkube.utils.time.ensure_utc`, which now also treats a timezone without a UTC offset as UTC) replaces twelve module-local `_as_utc` helpers. Git object IDs, `sha256:` digests and `…sha1:`/`…sha256:` revision parsing live in `greenkube.gitops.sha`; the raw, Kustomize and Helm planners share one plan digest (`gitops/planning/digest.py`, digests unchanged); `canonical_repository_identity` is `sanitize_repository_url`; the plan, GitOps and compatibility routers share `public_repository_url`; the supported renderer set is `PLANNABLE_RENDERERS`; a duplicated plan-worker method is gone.
- API repository URLs in plan responses now keep SCP-style `host:path` remotes and lowercase hosts, like the GitOps routes (credentials are still removed; overlong or control-character URLs are still `redacted`).
- `pyrefly` moved to the `dev` dependency group, so the runtime image no longer installs a type checker; `rich` is no longer a direct dependency (Typer still provides it).
- The automation worker's `--queue` option is hidden and deprecated; `plans` stays its only accepted value.

### Removed
- **Instant collection path (M4):** `DataProcessor.run()`, `CollectionOrchestrator`, `CostNormalizer`, `PrometheusResourceMapper` (the `PodResourceMaps` dataclass stays), `PrometheusCollector.collect()` and its instant-query parsers, `MetricAssembler.assemble()`/`prefetch_intensities()`, and the unused `container_restarts`/`node_instance_labels` PromQL helpers. `run_range()` is the only collection path.
- **Unused settings:** `LOW_NODE_CPU_THRESHOLD` (Helm `config.lowNodeCpuThreshold`) and `PROMETHEUS_QUERY_RANGE_MAX_SAMPLES` (Helm `config.prometheus.queryRangeMaxSamples`).
- **Runtime configuration persistence:** `ConfigService.load()`/`clear_persisted()`/`snapshot()`, the configuration repositories (SQLite and PostgreSQL) and `models/configuration.py`. Migration `0059` drops the never-used `configuration_overrides` table from migration 0020. Settings-page changes still apply in memory and to the release Secret as before.
- `RecommendationStatus.PLAN_CREATED`, `PLAN_STATUS_CHANGED` and `PLAN_ACTION_STATUS_CHANGED`: event names, not statuses (BUG-24).
- `automation/rollout/` (`RolloutReconciler`), `models/rollout.py` and `docs/rollouts.md`, which nothing used.
- `ControllerKind.EXPLICIT_REPOSITORY` and `ControllerKind.ANNOTATION`, which no discovery source produced.
- `ProviderGitBlobResolver`, the single-file `update_file` path of every Git provider, the worker `queue` parameter, the reader for unescaped Flux post-renderer selectors (no released version wrote them), and the plan-service aliases (`create`/`read`/`approve`/`stale`/`expire`/`supersede`, `PlanService`, `PlanServiceError`, `create_plan_for_recommendations`, `get_plan_service`). `RuntimePlanService.prepare_plan` is now the single entry point.
- **Helm:** the `automation.providers[].tokenSecret` and `repositories` aliases. Rendering now fails with the replacement keys (`secretName`/`secretKey`/`secretNamespace`, `repositoryAllowlist`).
- Storage methods without callers: `delete_binding`, `get_by_idempotency_key`, `list_recommendation_ids_for_plan`, `list_recommendations_by_action_status`, `upsert_worker_heartbeat` and `list_worker_heartbeats`.

### Fixed
- Stale and contradictory documentation (L7): the optional ignore reason, `data/` paths for region mappings and instance profiles, the removed instant path, the CO₂e formula (grams, no division by 1000), the "mobile-first" claim, the PostgreSQL test image, the Secret that holds the API key, Argo CD remote-chart support, and the deprecated eligibility endpoint in the automation guide.
