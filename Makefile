# GreenKube developer entry points.
#
# End-to-end loop on a live cluster (Minikube with Argo CD, Flux, Gitea and
# Prometheus). See docs/get-started/first-pr.md and hack/e2e/lib.sh for the
# settings (all overridable from the environment). Every target refuses to run
# unless kubectl points at E2E_KUBE_CONTEXT (default: minikube).

.PHONY: quickstart e2e e2e-status e2e-clean e2e-test test-pg

## Install GreenKube with the demo values next to the sample app, then stop:
## open the first pull request yourself. Deletes nothing.
quickstart:
	hack/e2e/build-image.sh
	hack/e2e/setup-repo.sh
	hack/e2e/setup-gitops.sh
	hack/e2e/install.sh

## Build the image in Minikube, install GreenKube and drive the full loop.
## Starts by removing what a previous run created (E2E_FRESH=0 keeps it).
e2e:
	hack/e2e/run.sh

## Print the evidence of the last run and the live state of the environment.
e2e-status:
	hack/e2e/status.sh

## Remove everything the e2e harness created (objects it labelled only).
e2e-clean:
	hack/e2e/cleanup.sh

## Run the pytest wrapper (requires GREENKUBE_E2E=1 and a prepared cluster).
e2e-test:
	GREENKUBE_E2E=1 uv run pytest -q -p no:cacheprovider tests/e2e

# --- Tests against a live PostgreSQL (roadmap O8) -------------------------------
# Starts the throwaway PostgreSQL of docker-compose.test.yml (tmpfs, no volume),
# runs pytest with GREENKUBE_TEST_POSTGRES_DSN pointing at it, fails if any
# PostgreSQL test was skipped, then removes the container (TEST_PG_KEEP=1 keeps
# it). Override the host port, the tests or the pytest flags:
#   make test-pg TEST_PG_PORT=55432 TESTS="tests/storage tests/integration"
TEST_PG_PORT ?= 5432
TESTS ?= tests
PYTEST_ARGS ?= -q -p no:cacheprovider
TEST_PG_KEEP ?= 0
TEST_PG_COMPOSE = GREENKUBE_TEST_PG_PORT=$(TEST_PG_PORT) docker compose -f docker-compose.test.yml
TEST_PG_DSN = postgresql://greenkube:greenkube_password@127.0.0.1:$(TEST_PG_PORT)/greenkube_test

## Run the test suite against SQLite and a disposable PostgreSQL container.
test-pg: SHELL := /bin/bash
test-pg:
	$(TEST_PG_COMPOSE) up -d --wait postgres-test
	@set -o pipefail; status=0; \
	GREENKUBE_TEST_POSTGRES_DSN="$(TEST_PG_DSN)" env -u VIRTUAL_ENV uv run pytest $(PYTEST_ARGS) -rs $(TESTS) \
		2>&1 | tee .test-pg.log || status=$$?; \
	if [ "$$status" -eq 0 ]; then .github/scripts/check-postgres-ran.sh .test-pg.log || status=$$?; fi; \
	rm -f .test-pg.log; \
	if [ "$(TEST_PG_KEEP)" != "1" ]; then $(TEST_PG_COMPOSE) down -v; fi; \
	exit $$status
