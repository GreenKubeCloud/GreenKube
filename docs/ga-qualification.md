# GA qualification and GitOps runbook

This document is the release acceptance record for the optimization engine. It
describes the as-built path and the checks required before promoting a release.

## As-built control loop

1. Metrics are collected and native analyzers persist an active recommendation.
2. The PR operation resolves the repository binding, patches the manifest, and
   records an auditable pull request (`pr_open`).
3. A merged provider change is applied with `application_method=pr_merge`, which
   freezes the verification baseline.
4. The verifier evaluates cost, carbon, traffic, and Kubernetes health during
   the observation window. A successful evaluation becomes `verified` and
   supplies measured values to the savings ledger.
5. The ledger writes idempotent period records. A failed health gate enters
   `rollback_review`; attribution stops and previous rows are superseded rather
   than deleted.

The credential-free acceptance test is
`tests/integration/test_ga_acceptance.py`. It uses the production repositories,
automation service, lifecycle, and savings attributor with a deterministic Git
provider. Provider webhooks, Kubernetes probes, and PostgreSQL are validated by
their targeted suites and are not replaced by this test.

## Release acceptance checklist

Run from the repository root:

```bash
./scripts/validate_ga.sh
```

The gate must be green for:

- Python formatting, linting, type checking, and integration acceptance tests.
- Full Python test suite with coverage threshold.
- Frontend tests and production build.
- Helm lint and chart unit tests (when Helm and the unittest plugin are installed).
- A clean working tree after validation.

For production promotion, additionally run the PostgreSQL contract tests with
`GREENKUBE_TEST_POSTGRES_DSN`, render the production Helm profile, and execute
the Minikube smoke test from a cluster with Prometheus available.

## Incident and rollback runbook

1. Stop promotion if the verifier reports `rollback_review`, `inconclusive`, or
   a failed health gate. Do not manually mark the recommendation `verified`.
2. Inspect the recommendation event trail and the pull request URL before
   changing the workload.
3. Revert the merged Git commit through the provider, then record the resulting
   state as `reverted` using the normal lifecycle service.
4. Confirm that the savings API no longer attributes periods after rollback and
   that superseded ledger rows remain available for audit.
5. Re-run the acceptance test and the relevant provider, verifier, and database
   contract suites before reopening promotion.
