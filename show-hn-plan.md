# GreenKube — Show HN Readiness Plan

**Date:** February 28, 2026
**Version Audited:** 0.2.2
**Last Cleaning Pass:** 12 commits on `chore/project-cleaning` — 37 issues fixed, 333 tests passing.

This document tracks the **remaining** open issues and feature requests. Fixed issues have been removed — see git log for what was addressed.

---

## Table of Contents

1. [⚠️ Remaining Code Issues](#1-️-remaining-code-issues)
2. [🏗️ Architecture Debt (Deferred)](#2-️-architecture-debt-deferred)
3. [🧪 Testing Gaps](#3--testing-gaps)
4. [✨ Feature Requests for Show HN](#4--feature-requests-for-show-hn)
5. [🚀 Nice-to-Have Features (Post-Launch)](#5--nice-to-have-features-post-launch)
6. [🎯 Show HN Launch Checklist](#6--show-hn-launch-checklist)

---

## 1. ⚠️ Remaining Code Issues


### SEC-004: `config.py` `_get_secret()` reads from predictable path
**Severity:** Low
**Description:** Secrets are read from `/etc/greenkube/secrets/{KEY}`. If an attacker can create files in that path, they can inject arbitrary config. This is standard K8s secret mounting, so low risk, but worth noting.


---

## 2. 🏗️ Architecture Debt (Deferred)

These are larger refactors that were intentionally deferred. They don't block launch but should be addressed in a future cycle.


### ARCH-002: Global singleton `config` loaded at import time
**Severity:** Medium
**Description:** `config = Config()` at module level means configuration is frozen at import time. This makes multi-tenant or multi-config scenarios impossible. (Mitigated in tests by `Config.reload()`.)
**Fix:** Use dependency injection throughout. Pass `Config` instances explicitly to components.

### ARCH-003: Global singleton `db_manager` loaded at import time
**File:** `src/greenkube/core/db.py`
**Severity:** Medium
**Description:** `db_manager = DatabaseManager()` at module level creates a global singleton. Combined with `lru_cache` in factory functions, this creates a web of implicit global state.
**Fix:** Manage `db_manager` lifecycle explicitly through the application entry points.

---

## 3. 🧪 Testing Gaps

### TEST-003: No load/performance tests
**Severity:** Low
**Description:** No performance testing exists. For Show HN, you should know: How many pods can GreenKube handle? What's the API response time at scale?
**Fix:** Add basic benchmarks using `pytest-benchmark` or `locust`.

### TEST-005: No frontend tests
**Severity:** Medium
**Description:** The SvelteKit frontend has no test files (no `*.test.ts` or `*.spec.ts`).
**Fix:** Add at least component-level tests for critical dashboard pages using Vitest/Playwright.

---

## 4. ✨ Feature Requests for Show HN

### FEAT-002: Grafana dashboard export
**Priority:** 🟡 High
**Description:** Many HN readers already use Grafana. Ship a ready-made Grafana dashboard JSON that scrapes GreenKube's `/metrics` endpoint.
**Implementation:** Create `dashboards/greenkube-grafana.json` with panels for CO2, cost, recommendations...

### FEAT-003: Slack/webhook notifications for recommendations
**Priority:** 🟡 High
**Description:** Allow GreenKube to send alerts when new zombie pods or rightsizing opportunities are detected.
**Implementation:** Add a `NotificationService` with Slack webhook, generic webhook, and email adapters.

### FEAT-004: Cost savings summary badge/widget
**Priority:** 🟡 High
**Description:** An embeddable badge showing "GreenKube saved X kg CO2 and $Y this month" for README files and dashboards.
**Implementation:** Add a `/api/v1/badge` endpoint returning SVG.

### FEAT-005: Multi-cluster support
**Priority:** 🟠 Medium
**Description:** Allow GreenKube to aggregate data across clusters.
**Implementation:** Add a `cluster_name` field to all metrics, configurable via env var.

### FEAT-006: Prometheus remote-write receiver
**Priority:** 🟠 Medium
**Description:** Allow GreenKube to receive metrics via Prometheus remote-write protocol instead of scraping.
**Implementation:** Add a `/api/v1/write` endpoint compatible with the Prometheus remote-write spec.

### FEAT-007: CSRD/ESRS E1 report export (PDF)
**Priority:** 🟡 High
**Description:** Generate a formatted PDF report aligned with ESRS E1 disclosure requirements.
**Implementation:** Use `reportlab` or `weasyprint` to generate a branded PDF.

### FEAT-008: Trend analysis and forecasting
**Priority:** 🟠 Medium
**Description:** Show CO2 trends and predict future emissions. "At this rate, your annual emissions will be X kg CO2."
**Implementation:** Add linear regression or ARIMA forecasting to the timeseries endpoint.

### FEAT-009: Cost comparison with carbon offsets
**Priority:** 🟢 Low
**Description:** Show how much it would cost to offset the measured emissions.
**Implementation:** Add a configurable offset cost ($/tCO2) and compute it in the summary endpoint.

### FEAT-010: kubectl plugin
**Priority:** 🟠 Medium
**Description:** `kubectl greenkube report` would be more natural for K8s users.
**Implementation:** Package the CLI as a kubectl plugin (rename binary to `kubectl-greenkube`).

### FEAT-011: Live terminal dashboard (TUI)
**Priority:** 🟢 Low
**Description:** A rich TUI using `textual` that auto-refreshes metrics in the terminal.
**Implementation:** Use `textual` library for a live dashboard with tables and sparklines.

### FEAT-012: Auto-apply recommendations (with approval)
**Priority:** 🟠 Medium
**Description:** Automatically apply rightsizing recommendations by patching deployments, with a confirmation step or dry-run mode.
**Implementation:** Add a `greenkube apply-recommendation <id>` command.

### FEAT-013: Scope 1/2/3 breakdown
**Priority:** 🟡 High
**Description:** Properly categorize emissions into Scope 2 (electricity) and Scope 3 (embodied hardware). Essential for CSRD.
**Implementation:** The data is already there (`co2e_grams` vs `embodied_co2e_grams`). Expose it clearly in the API and dashboard.

### FEAT-014: Carbon budget / quotas per namespace
**Priority:** 🟠 Medium
**Description:** Allow teams to set carbon budgets per namespace and alert when exceeded.
**Implementation:** Add a `carbon_budgets` config section and a budget tracking module.

### FEAT-015: Comparison mode: before vs. after optimization
**Priority:** 🟠 Medium
**Description:** After implementing a recommendation, show the impact.
**Implementation:** Add `GET /api/v1/metrics/compare?before=7d&after=7d` endpoint.

### FEAT-016: GitHub Action for CI carbon reporting
**Priority:** 🟡 High
**Description:** A GitHub Action that adds carbon impact comments to PRs.
**Implementation:** Create a `greenkube-action` repo with a composite GitHub Action.

### FEAT-017: OpenTelemetry integration
**Priority:** 🟠 Medium
**Description:** Export GreenKube metrics via OTLP for observability platforms.
**Implementation:** Add an OTLP exporter using `opentelemetry-sdk`.

### FEAT-018: Dashboard: real-time WebSocket updates
**Priority:** 🟢 Low
**Description:** Add a `WebSocket /api/v1/ws/metrics` endpoint that pushes new metrics as they're collected.

### FEAT-019: Leaderboard / namespace ranking
**Priority:** 🟢 Low
**Description:** A leaderboard showing which namespaces/teams are the most and least carbon-efficient.
**Implementation:** Add `GET /api/v1/namespaces/ranking` sorted by CO2 efficiency.

### FEAT-020: Custom instance power profiles
**Priority:** 🟠 Medium
**Description:** Allow users to add custom power profiles for on-prem hardware or unsupported instance types.
**Implementation:** Add a `config.customInstanceProfiles` Helm value mapped to a ConfigMap.

---

## 5. 🚀 Nice-to-Have Features (Post-Launch)

- **kube-green integration** — auto-suspend workloads during off-peak
- **Carbon-aware autoscaler** — CronJob scaling based on off-peak recommendations
- **Terraform provider** — report carbon impact of infrastructure changes
- **FinOps integration** — Kubecost, Vantage, CloudZero data import
- **AI-powered recommendations** — LLM-based explanation of optimization strategies
- **Historical cost tracking** — OpenCost daily snapshots for trend analysis
- **Multi-region optimization** — suggest moving workloads to lower-carbon regions
- **PVC carbon accounting** — storage carbon footprint based on disk type/replication
- **Network carbon accounting** — data transfer emissions based on cross-region traffic

---

## 6. 🎯 Show HN Launch Checklist

### Must-Have (before posting)
- [ ] **Demo mode** (FEAT-001) — essential for HN try-it-now experience
- [ ] **Remove WebSocket claim** from README or implement it (FEAT-018)

### Should-Have (high impact for reception)
- [ ] **Grafana dashboard** (FEAT-002)
- [ ] **CSRD report export** (FEAT-007)
- [ ] **Scope 1/2/3 breakdown** (FEAT-013)
- [ ] **Slack notifications** (FEAT-003)
- [ ] **kubectl plugin** (FEAT-010)
- [ ] **GitHub Action** (FEAT-016)

### Nice-to-Have (cherry on top)
- [ ] **TUI dashboard** (FEAT-011)
- [ ] **Carbon budget quotas** (FEAT-014)
- [ ] **Multi-cluster support** (FEAT-005)
- [ ] **Comparison mode** (FEAT-015)

---

## Summary

| Category | Open | Deferred |
|----------|------|----------|
| Code Issues | 3 | — |
| Architecture Debt | — | 4 |
| Testing Gaps | 2 | — |
| Feature Requests | 20 | — |
| **Total** | **25** | **4** |

*37 issues were fixed in the `chore/project-cleaning` branch (12 commits, 52 files changed, 333 tests passing).*