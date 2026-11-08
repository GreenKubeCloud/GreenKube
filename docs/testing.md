# Testing

GreenKube uses two layers of Python tests:

- Fast unit tests keep external services mocked where the behavior under test is not storage-specific.
- Real database contract tests exercise the same repository behavior against SQLite and PostgreSQL.

## Real Database Contract Tests

The contract tests live in [tests/integration/test_real_database_repositories.py](../tests/integration/test_real_database_repositories.py).
They create the application schema through `DatabaseManager.connect()`, run the bundled migrations, and then call the
real repository adapters directly.

The contract currently covers:

- schema creation and migration tracking
- carbon intensity history upserts and lookups
- combined metric writes, reads, namespace listing, and aggregations
- node SCD Type 2 snapshots
- recommendation lifecycle operations and savings summaries
- dashboard summary and time-series cache upserts
- the append-only savings ledger: idempotent accrual, watermarks, partial windows and legacy compressed rows

## SQLite

SQLite runs by default on every test run. Each test gets an isolated temporary database file via `tmp_path`, which means
the tests exercise the real SQLite driver, schema creation, migrations, and SQL queries without sharing state.

```bash
pytest tests/integration/test_real_database_repositories.py
```

When no PostgreSQL DSN is configured, the PostgreSQL parameter is skipped and the SQLite contract still runs.

## PostgreSQL

PostgreSQL tests run when `GREENKUBE_TEST_POSTGRES_DSN` is set. Each test creates a unique PostgreSQL schema, configures
GreenKube with `DB_SCHEMA`, and drops that schema after the test. This keeps tests isolated without needing to create or
drop whole databases.

For local development, start the bundled ephemeral PostgreSQL container:

```bash
docker compose -f docker-compose.test.yml up -d postgres-test
export GREENKUBE_TEST_POSTGRES_DSN="postgresql://greenkube:greenkube_password@localhost:5432/greenkube_test"
pytest tests/integration/test_real_database_repositories.py
docker compose -f docker-compose.test.yml down -v
```

The compose service uses the official `postgres:18.0-alpine3.22` image, exposes port `5432`, and stores data on `tmpfs`, so the
database is disposable and starts cleanly for local test sessions.

## CI/CD

The main GitHub Actions CI job starts a PostgreSQL service container and exports:

```bash
GREENKUBE_TEST_POSTGRES_DSN=postgresql://greenkube:greenkube_password@localhost:5432/greenkube_test
```

The regular `pytest --cov=greenkube ...` command then runs both SQLite and PostgreSQL variants of the contract tests.
Because every PostgreSQL test uses its own schema, tests can be added without coupling their data setup to global CI
database state.

## End-to-end loop on a live cluster

`make e2e` runs the whole loop on Minikube with Argo CD, Flux, Gitea and Prometheus: it builds the image from the
checkout, installs GreenKube with the quickstart values, then drives a sample workload from recommendation to pull
request, merge, GitOps sync, apply detection and measurement. The scripts live in `hack/e2e/`; the
[first pull request guide](get-started/first-pr.md) describes the prerequisites, settings and steps.

`tests/e2e/test_live_loop.py` wraps the same run and is skipped unless `GREENKUBE_E2E=1` (`make e2e-test`).
`tests/e2e/test_quickstart_assets.py` always runs: it checks that the quickstart values render a valid configuration
and that the sample repository and GitOps objects agree. `tests/e2e/test_harness_safety.py` also always runs: with
stub `kubectl`, `helm` and `curl` binaries, it checks that the harness scripts refuse any kubectl context other than
`E2E_KUBE_CONTEXT` and never update or delete objects without the harness's ownership marker.
