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

`make test-pg` does it in one command (roadmap O8): it starts the throwaway PostgreSQL of `docker-compose.test.yml`,
runs pytest with `GREENKUBE_TEST_POSTGRES_DSN` pointing at it, fails if any PostgreSQL test was skipped, and removes the
container. `TEST_PG_PORT` changes the host port (default `5432`), `TESTS` the test paths (default `tests`),
`PYTEST_ARGS` the flags, and `TEST_PG_KEEP=1` keeps the container:

```bash
make test-pg
make test-pg TEST_PG_PORT=55432 TESTS="tests/storage tests/integration tests/api"
```

To drive it by hand, start the bundled ephemeral PostgreSQL container:

```bash
docker compose -f docker-compose.test.yml up -d postgres-test
export GREENKUBE_TEST_POSTGRES_DSN="postgresql://greenkube:greenkube_password@localhost:5432/greenkube_test"
pytest tests/integration/test_real_database_repositories.py
docker compose -f docker-compose.test.yml down -v
```

The compose service uses the official `postgres:18.0-alpine3.22` image, exposes port `${GREENKUBE_TEST_PG_PORT:-5432}`, and stores data on `tmpfs`
(`/var/lib/postgresql`, the data root of the PostgreSQL 18 images), so the
database is disposable and starts cleanly for local test sessions.

## CI/CD

The main GitHub Actions CI job starts a PostgreSQL service container and exports:

```bash
GREENKUBE_TEST_POSTGRES_DSN=postgresql://greenkube:greenkube_password@localhost:5432/greenkube_test
```

The regular `pytest --cov=greenkube ...` command then runs both SQLite and PostgreSQL variants of the contract tests. The job runs pytest with
`-rs` and `.github/scripts/check-postgres-ran.sh` fails it when any test was skipped for a missing
`GREENKUBE_TEST_POSTGRES_DSN`, so PostgreSQL coverage cannot silently disappear (O8).
Because every PostgreSQL test uses its own schema, tests can be added without coupling their data setup to global CI
database state.

The chart jobs run `helm lint`, `helm unittest` and `.github/scripts/promtool-check.sh` (the PrometheusRule alerts
unit-tested with `promtool`). The `psa-restricted` job creates a kind cluster and runs
`.github/scripts/psa-restricted-check.sh`: several renders of the chart go through `kubectl apply --dry-run=server` in a
namespace that enforces Pod Security "restricted", and any "would violate PodSecurity" warning fails the job. Run it
locally against any disposable cluster with `PSA_KUBE_CONTEXT=<context> .github/scripts/psa-restricted-check.sh`
(contexts named `kind-*` need no variable). The `actionlint` job checks the workflows; every action is pinned by
commit SHA.

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

### Nightly in CI

`.github/workflows/e2e-nightly.yml` runs `make e2e` every night, on demand (`workflow_dispatch`, with an option to
skip the Flux variant), on pull requests that touch `src/greenkube/`, `helm-chart/`, `frontend/`, the Dockerfile,
`hack/e2e/` or the quickstart assets, and from the release workflow (`workflow_call`): a release tag publishes
nothing unless the loop passes. GitHub schedules only the workflow file of the default branch (`main`), so the
nightly run checks out the integration branch `dev` (`E2E_NIGHTLY_REF`); the file must be present on `main` for
the schedule to fire.

After the verdict, the loop checks the v0.4 exit criterion: for each recommendation, the latest decided
measurement of its change (`GET /api/v2/changes/{id}/measurements`) must give a measured allocated cost within
`E2E_PROJECTION_TOLERANCE` (20%) of the projected one. `steps.jsonl` records the ratio (`projection_accuracy`); an
inconclusive or contaminated measurement is recorded as a `warning` instead of a pass or a failure. `.github/e2e/bootstrap-minikube.sh` first installs the prerequisites on a fresh Minikube at pinned
versions (metrics-server, kube-prometheus-stack with 30-second scrapes, Argo CD, Flux, Gitea with an admin user
`greenkube` and an API token). The evidence directory (`steps.jsonl` and the saved API responses), pod logs, events
and the GitOps objects are uploaded as the `e2e-evidence` artifact on every run, and the job summary lists each step.
The bootstrap script refuses to run outside GitHub Actions unless `E2E_BOOTSTRAP_ALLOW=1`.

## Upgrade from the last release

`make upgrade-test` (`hack/upgrade/upgrade-test.sh`) installs the 0.3.1 chart and image on a throwaway kind
cluster with its own kubeconfig, seeds the database, upgrades it to the working tree with
`helm upgrade --reset-then-reuse-values --wait` and checks readiness, the schema version, the seeded row counts and
the API key ([Upgrading](upgrading.md#checking-an-upgrade-on-kind)). `.github/workflows/upgrade-test.yml` runs it on
pull requests that touch migrations or the chart, and the release workflow requires it.
