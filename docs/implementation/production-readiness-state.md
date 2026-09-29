# Production Readiness Integration State

This document is maintained by the integration agent. Workstream branches remain
isolated until their declared tests and the repository-wide integration gate pass.

## Current baseline

- Base commit: `a90f63f`
- Active wave: W3 control-plane hardening
- Baseline targeted tests: `uv run pytest -q tests/core/optimization tests/automation`
- Baseline result: passing

## Workstream register

| Workstream | Scope | Dependency | Status |
| --- | --- | --- | --- |
| W1-A | Persistence, unit of work and migrations | None | Integrated |
| W1-B | API edge security | None | Integrated |
| W1-C | Metric integrity and provenance | None | Integrated |
| W1-D | Shared time semantics | None | Integrated |
| W1-E | Git provider adapters | None | Integrated |
| W1-F | Helm and Kubernetes operations | None | Integrated |
| W1-G | Supply chain and CI | None | Integrated |
| W1-H | Authenticated frontend | W1-B integration | Integrated |
| W1-I | PostgreSQL upgrade procedure | W1-A and W1-G integration | Integrated |
| W2-A | Recommendation identity and fingerprints | W1 gate | Integrated |
| W2-B | Optimization runs and lifecycle | W1 gate | Integrated |
| W2-C | Savings ledger | W2-B | Integrated |
| W2-D | Verification and Karpenter controls | W2-B | Integrated |
| W2-E | Durable automation operations | W2-B | Integrated |
| W2-F | Versioned runtime configuration | W2-B | Integrated |
| W2-G | Stable cursor pagination | W2-A | Integrated |
| W2-H | Production Helm topology | W1-F | Integrated |
| W2-I | Frontend operations and configuration | W2-E, W2-F, W2-G | Integrated |
| W2-J | Composition root and routes | W2-A through W2-I | Integrated |
| W3-A | Repository bindings and GitOps discovery | W2-J | Integrated |
| W3-D | Verification health gates | W2-D | Integrated |
| W3-B | Progressive rollout orchestration | W3-A, W3-D | Integrated |
| W3-C | Automation execution | W3-A, W3-B, W3-D | Integrated |
| W3-E | Observability and SLOs | W3-A through W3-D | Integrated |
| W3-G | Resilience and recovery | W3-A through W3-E | Integrated |
| W3-F | E2E and GA qualification | W3-A through W3-E | Integrated |

## Integration gates

The foundation gate is not complete until migrations are serialized and
fail-fast, locked builds and CI checks pass, authentication and SSRF policies
are enforced, metric provenance and shared time semantics are available, the
chart is installable and observable, and the PostgreSQL upgrade procedure is
validated against test data.

The integration agent owns branch rebases, migration-number allocation,
repository-wide checks, image/chart builds, and Minikube validation.

Latest validated gates:

- Frontend tests: 172 passing; production build successful.
- W3-A targeted backend tests: 7 passing.
- W3-B targeted rollout tests: passing.
- W3-C targeted automation tests: 20 passing.
- W3-G resilience tests: 3 passing.
- W3-F GA acceptance and lifecycle tests: 30 passing.
- Ruff check, Ruff format check, and Pyrefly: passing.
- Helm lint, chart rendering, and Kubernetes server-side dry-run: passing
  against the local Minikube cluster.
- W3-E observability and verification telemetry tests: passing; SLO and
  alerting runbook is documented in `docs/observability-slos.md`.
- PostgreSQL integration tests remain skipped unless
  `GREENKUBE_TEST_POSTGRES_DSN` is provided.
