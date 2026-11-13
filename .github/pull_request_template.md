## Summary

<!-- What does this change do, and why? Link the issue it closes. -->

Roadmap items / defects: <!-- e.g. N6, BUG-58 -->

## Type

- [ ] Bug fix (with a regression test that fails before the fix)
- [ ] Feature
- [ ] Refactor (no behavior change)
- [ ] Docs / CI / chart only

## Checklist

- [ ] Targets `dev`.
- [ ] Tests added or updated; `uv run pytest` passes, including the PostgreSQL variants (`make test-pg`) for storage changes.
- [ ] `uv run ruff check .`, `uv run ruff format --check .` and `uv run pyrefly check` pass.
- [ ] Database migrations, if any, are additive and exist for both SQLite and PostgreSQL.
- [ ] Chart changes: `helm lint ./helm-chart` and `helm unittest helm-chart` pass; new values are in `values.yaml`, `values.schema.json` and `docs/configuration.md`.
- [ ] Frontend changes: `npm test` and `npm run build` pass in `frontend/`.
- [ ] Docs updated and a `CHANGELOG.md` entry added under `[Unreleased]`.
- [ ] Workloads are still changed only through Git, and anything GreenKube cannot do is explained (fail closed).
