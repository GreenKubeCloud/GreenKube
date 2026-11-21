# GreenKube roadmap

> **Revision 3, updated 2026-11-21.** Baseline: local `dev` after the phase A merges (not pushed; origin `dev` is still `82b3bee1`).
> **Audience:** the maintainer, contributors and coding agents. Each task says what to do, where, and how to prove it is done.
> **Inputs:**
> - the data-quality audit, [`docs/reviews/data-quality-audit.md`](reviews/data-quality-audit.md): mechanism, evidence and fix of every finding cited here;
> - the first field test on a real cluster (kubeadm 1.34, 3 control planes and 4 workers, Cilium, Argo CD, self-hosted GitLab, OpenCost, then VPA and KRR), 2026-11-16: issues F-01 to F-13 below;
> - the maintainer's direction of 2026-11-20: focus the product on what creates value, and make GreenKube light;
> - the maintainer's decisions of 2026-11-20 on the scope table (section 9): connectors and advisory analyzers go, orphaned LoadBalancer and PersistentVolume detection stays, carbon stays as it is, PostgreSQL becomes the only database, and lightness must not cost useful granularity.

## 0. How to use this roadmap

- **Phases** run in order: A (make the core loop work on a real cluster), B (cut the scope), C (make it light), D (finish the audit work that still applies), E (prove and release). Inside a phase, tasks marked with the same **lane** touch the same files and run one after the other; different lanes can run in parallel.
- **Every task is test-first.** Write the regression or acceptance test from the task text, see it fail on `dev`, then change the code. A task is done when its acceptance passes, the gates of section 10 pass, and the docs and changelog fragment are updated.
- **Removal tasks** (phase B) follow one procedure, described in B.0. Never remove a database table, a setting or an API route without that procedure.
- Finding IDs (`COL-01`, `MEA-08`...) point to the audit. Field-test IDs (`F-01`...) point to section 4. The appendix lists every audit finding with its current status.

## 1. Where GreenKube stands

**Built and merged on `dev`** (all gates green, PostgreSQL included):
- **Collection integrity:** history built from the Kubernetes state at sample time, every collection step recorded (collected, empty, failed, gap), stable cluster identity, atomic window writes, double-precision measures.
- **Cost:** OpenCost outages kept pending and re-priced, never silently replaced.
- **Recommendation safety:** fail closed on unknown OOM or restart signals, no decrease right after an OOM fix, no oscillation, stale targets retired.
- **Measurement:** valid verdict statistics with a documented one-week limit, health gates against the baseline, constant prices, per-hour carbon normalisation.
- **Released capacity:** CPU cores and memory freed, projected and measured, with a capacity verdict and ledger accrual.
- **Ledger:** ledger v2 is the single source for every user-facing savings figure.
- **Phase A (section 4):** every field issue of the first field test is fixed and merged:
  - GitLab merge requests (F-01);
  - Argo CD multi-source and upstream-chart bindings (F-02, F-11, F-13);
  - projected savings after an upgrade (F-03);
  - VPA and KRR floors and maturity, no source-only recommendations, and an OOM floor under every policy (F-12);
  - API-key sign-in (F-09);
  - static pods, eligibility, singletons and the small items (F-04 to F-10);
  - the two stopped branches, rollups and reads (WP-05) and energy and carbon (WP-04).

**Field test (section 4):** the issues found on 2026-11-16 are fixed on `dev`, but not yet re-tested on the field cluster. Follow-ups found while fixing them are listed in A.9.

**The core promise has still never been observed end to end on a real cluster:** merge request merged, change measured, verdict decided, ledger entry written.

**Size and weight, measured on `dev`:**

| What | Value |
|---|---|
| Python source | 149,180 lines in `src/greenkube` (core 28k, automation 27k, storage 25k, gitops 19k, API 11k, verify 10k) |
| Tests | 133,403 lines |
| Frontend | 12,680 lines |
| Settings | 197 settings in `core/config.py` |
| Helm | 1,255 lines of `values.yaml`, 42 templates |
| Migrations | 96 per dialect, two SQL dialects maintained by hand (SQLite 6.4k lines, PostgreSQL 6.2k lines) |
| Default install | 4 pods (api, controller, renderer, PostgreSQL) plus optional worker, backup CronJob, KRR CronJob and Polaris CronJob |
| Default requests / limits | 325m CPU and 608 MiB requested, 2.5 CPU and 3 GiB of limits |

The data path stores one row per pod and per 5-minute step, plus one per container, then hourly copies kept forever (`METRICS_AGGREGATED_RETENTION_DAYS = -1`). This is an estimate, not a measurement: at 500 pods with about 1.5 containers each, that is about 360,000 raw rows a day and 30,000 hourly rows a day, kept forever.

## 2. Product focus (decision)

**What GreenKube is for:** proving what a Kubernetes optimization actually saved. The loop that delivers that value is short:

1. **Recommend** CPU and memory requests and limits, from GreenKube's own measurements, corroborated by VPA and KRR when present.
2. **Deliver** the change as a reviewed pull request or merge request in the GitOps repository (Argo CD or Flux; raw manifests, Kustomize, Helm).
3. **Measure** what the merged change realized (money, CO2e and released capacity), against a counterfactual, with an honest verdict.
4. **Account** for it in an append-only ledger, and compare realized with projected to calibrate projections.

Everything else is either a dimension of those numbers (cost, carbon, capacity), an operational necessity (auth, Helm, migrations), or out of scope for now.

**Why cut:**
- One maintainer cannot keep about 150k lines, 13 recommendation sources and analyzers, 3 delivery modes and 2 SQL dialects correct.
- The audit found defects in every peripheral area it looked at.
- The field test found that the peripheral sources (KRR, VPA floors) and analyzers (static pods as "Node" workloads) mostly add noise to the core list.

**Why lighten:** a tool that sells resource optimization must not be a noticeable consumer itself. Section 3 sets a budget.

### 2.1 Scope: keep, simplify, remove

| Area | Decision | Reason |
|---|---|---|
| Native CPU and memory rightsizing (requests up and down, limits, OOM increases) | **Keep** | The core recommendation. All the safety work of the audit lives here. |
| VPA source (includes Goldilocks) | **Keep, fix F-12** | The most common open-source recommender. Used as a corroborating vote, never alone. |
| KRR source | **Keep, fix F-12**; drop the chart CronJob, document a push example instead | Common, but its floors must not win. The CronJob is weight in the chart. |
| Push API (signal schema) | **Keep** | The open door for any other tool, at no maintenance cost per tool. |
| Kubecost, GKE recommender, Azure Advisor, Polaris, replay connectors | **Remove** (about 2,100 lines; decided) | Commercial or cloud-specific APIs that change often; Polaris gives hygiene findings, not sizes. Users can push signals instead. |
| Native analyzers: zombie pods, idle namespaces, off-peak scaling, autoscaling candidates, carbon-aware scheduling, node over/under-provisioning, Karpenter NodePool heuristic | **Remove** (about 1,300 lines; decided) | Advisory only, not deliverable as a PR, not measurable, noisy (audit REC-09/10/11/14/16, field F-04). |
| Orphaned LoadBalancer and PersistentVolume detection (analyzers, delete action, deletion safety and re-check, deletion measurement) | **Keep, harden** (decided) | Cheap to run, simple to detect, and an immediate saving. Hardening in B.2: no false positive on Gateway or ingress-controller LoadBalancers, an on-premises price for volumes (IMP-11), and a realized amount read from cost data, not restated from the projection (MEA-15). |
| Collectors: Karpenter, Cluster Autoscaler and HPA (realization factor), LB, PV and deletion candidates (orphan detection), Wattnet and DCGM (carbon) | **Keep** | Each still has a consumer. |
| Collected dimensions: network, disk I/O, ephemeral storage, PVC usage | **Remove** | Not used by rightsizing, measurement or orphan detection (which reads PV and claim status, not usage). Removing them cuts the PromQL queries per collection chunk and the row width (audit COL-13, COL-14, COL-19 become moot). |
| Cost: OpenCost, then bundled list prices, then configured rates | **Keep** | Unchanged. |
| Carbon | **Keep as it is** (decided) | CO2e stays a dimension of every number. The current model stays: Electricity Maps, Wattnet, Boavizta (embodied and per-instance power), DCGM GPU power. Only the audit fixes apply (A.0 `fix/energy-carbon`, open NRG findings in phase D). |
| Released capacity | **Keep** | The on-premises answer. |
| Change measurement and ledger v2 (hash chain, CSV export) | **Keep** | The product. |
| Legacy (v1) verifier and v1 ledger tables | **Remove** once their data is migrated (C.5) | Two systems for one job. |
| FOCUS export | **Remove** | No user, and its semantics were wrong (LED-10). CSV and the API stay. |
| Delivery modes: direct pull request, manual (diff and kubectl) | **Keep** | |
| Delivery modes "patch" and "ci" | **Remove** | Extra surface without known users. Manual mode covers "apply it yourself". |
| Git providers: GitHub (token and App), GitLab, Gitea | **Keep** | Gitea is small and used for end-to-end tests. |
| GitOps: Argo CD, Flux; raw, Kustomize, local and remote Helm, Flux HelmRelease | **Keep, fix F-02 and F-11** | This is where real repositories break (F-02, F-11); it deserves the effort. |
| Auth: sessions, API keys, OIDC, proxy headers, roles | **Keep, fix F-09** | Small and needed. |
| UI: Overview (savings statement), Recommendations, Changes, Ledger, Sources, Setup, Settings | **Keep** | |
| UI: Footprint pages (nodes, report) | **Keep** | With carbon kept as it is, cost and CO2e visibility per node and namespace stays useful. |
| UI: sustainability score, the large Grafana dashboard | **Remove** | The score has no defined method (API-06). Keep a small operations dashboard on the Prometheus metrics. |
| Demo | **Keep**, built on the same code path | Adoption needs it. It runs on PostgreSQL (C.4). |
| Storage: PostgreSQL and SQLite | **PostgreSQL only** (decided) | One dialect to keep correct; managed databases and HA. SQLite goes (C.4). |

**Expected effect** (an estimate to confirm while doing phase B):
- about 6,000 to 8,000 lines of source removed by phase B, plus their tests;
- about 6,000 lines of SQLite migrations and repositories removed by C.4, plus the dual-dialect tests;
- about 30 to 40 settings removed;
- the KRR and Polaris CronJobs and several loops removed.

## 3. Lightness (decision)

**Budget for a default install** on a 500-pod cluster, to be measured by a repeatable benchmark (C.1) and enforced in CI:

| Resource | Budget |
|---|---|
| Pods | 2 by default: GreenKube and PostgreSQL. The renderer runs only while rendering. |
| CPU | under 100m averaged over a day; requests 100m in total for GreenKube |
| Memory | under 300 MiB resident for GreenKube; PostgreSQL under 256 MiB |
| Database size | under 2 GB steady state at 500 pods with the 14-day raw window, plus under 1 GB per year of hourly history (to confirm with the first C.1 run) |
| Prometheus load | at most 6 range queries per collection cycle, step 5 minutes |
| Scan | one scan of 500 workloads under 30 s and under 150 MiB extra memory |

**Levers, in order of expected gain** (tasks in phase C):
1. **Store less, without losing useful detail** (C.3). Lightness must not come from aggregations too coarse to be useful: rightsizing needs per-container percentiles at the collection step, and measurement needs per-pod cost and CO2e. So the granularity stays, and the footprint comes from:
   - keeping raw 5-minute rows exactly as long as the rightsizing lookback needs them (14 days), so percentiles are computed on raw samples, not on hourly maxima (ROL-04);
   - keeping per-pod and per-container hourly rows with a bounded retention (default 400 days, enough for a year-over-year comparison), and workload-hour and node-hour rows for long-term history;
   - narrower rows (the dimensions of section 2.1 go, unused columns go) and PostgreSQL time partitioning, so retention drops a partition instead of deleting rows.
2. **Run less** (C.2): one GreenKube process by default (API and controller loops together, single replica). The renderer sandbox becomes an on-demand Kubernetes Job. Loop intervals are relaxed (discovery every 5 minutes, scans every 6 hours).
3. **Query less** (C.3): fewer PromQL queries per cycle (the removed dimensions go), and reads served by the coarsest table that still answers the question.
4. **One SQL dialect** (C.4).
5. **Process hygiene** (C.6): bounded memory per scan, lazy imports, one Uvicorn worker, a small connection pool, no unbounded caches.

## 4. Phase A: make the core loop work on a real cluster

Goal: on the field cluster (kubeadm, Cilium, Argo CD, self-hosted GitLab), open merge requests, merge them, and get measured verdicts and ledger entries.

**Status, 2026-11-21:**
- A.0 to A.7 are implemented and merged into `dev`, with every field issue F-01 to F-13 covered.
- Each branch went through an independent review, the reviewer's findings were fixed, and the gates passed on SQLite and PostgreSQL. Every review found real defects; all were fixed before merge.
- Nothing is confirmed on the field cluster yet: that is A.8.

| Task | What was done | Branch |
|---|---|---|
| A.0 | WP-05 (raw/hourly seam, dirty-hour rebuild with PostgreSQL locking, idle share kept and exposed, faithful hourly rows) and WP-04 (Boavizta embodied values, live intensity per zone, one power model, CPU outside pods, GPUs without DCGM, shared hosts in collection, recompute and projection) | `fix/rollups-reads`, `fix/energy-carbon` |
| A.1 (F-01) | GitLab commits no longer send `start_sha`; the fake GitLab server now refuses what GitLab refuses (stale `last_commit_id`, existing file, missing file); a commit without a parent list fails closed | `fix/gitlab-commit-existing-branch` |
| A.2 (F-11, F-13) | A Git chart whose values come from one `$ref` source in another repository is bound to the values repository and blocked as `cross-repository-git-chart` (planning such charts is not supported); repositories that only serve a chart are never suggested or auto-allowed | `fix/gitlab-commit-existing-branch` |
| A.3 (F-02), F-05 | Ties broken by Helm ownership labels or the `helm-values-mapping` annotation, with a plan-time check that no manifests directory declares the same workload (Jsonnet fails closed); one normalized repository identity; values files no longer wrapped | `fix/argo-multi-source-binding` |
| A.4 (F-03) | Each CPU/RAM cost component is priced over the hours that carry it, for samples and for node prices; zero-cost hours count as zero, so a projection never exceeds the observed cost; Impact Model 4.0.0 | `fix/projection-unsplit-cost` |
| A.5 (F-12) | Tool floors ignored as votes (configurable), immature VPAs ignored, changes below a minimum count as "keep", no recommendation from an external source alone, plugins cannot pose as native | `fix/signal-floors-maturity` |
| A.5 follow-up | An OOM-driven native memory increase is a floor under every consensus policy; malformed request signals are rejected at push time and dropped from consensus | `fix/oom-floor-consensus` |
| A.6 (F-09) | API-key sign-in in the UI with a signed, expiring cookie (never the key), per-key throttling, HTTPS required (or the chart's secure-cookie setting, or loopback), sign-out for key sessions | `fix/api-key-login` |
| A.7 (F-04, F-07, F-08) | Static pods excluded with a diagnostic; eligibility and plan creation share one policy gate function; replicas counted at the hour's latest step, so a rollout does not hide a singleton; per-replica p95 guard correct on the rollup path; batched sibling lookups | `fix/recommendation-field-issues` |
| A.7 (F-06, F-10) | Prometheus discovered before the first query; one version source with a check; NOTES per provider Secret; loop counters for startup passes; on-premises PUE; default-zone fallback logged once; a user cancel ends in `cancelled` with its reason | `fix/field-small-issues` |

### A.8 First measured verdicts on the field cluster
After A.1 to A.5, open merge requests for the eligible recommendations (metrics-server, seaweedfs, superset, opencost, datahub...), merge them, and wait for the baseline (7 days) and post-window (5 days).

**Record:**
- the verdicts, the projection accuracy and the ledger entries;
- the released capacity;
- any inconclusive reason.

Every surprise becomes an issue in this roadmap. **This is the release gate of phase E.**

### A.9 Follow-ups found during phase A (lane noted; do before or with phase D)
**Done and merged (2026-11-21), each reviewed and gated:**
- **Test hygiene:** a process-global realization store left bound to a closed database made tests reopen an `aiosqlite` connection that blocked interpreter exit. Fixed, with a session-end guard that fails the run and names the leaking test (`fix/test-thread-leak`).
- **Bearer key guessing:** failed API keys are throttled per client IP on the API and on the controller and worker metrics listeners, which stop accepting the bootstrap key once a scrape token exists; stale browser cookies never lock a browser out (`fix/bearer-throttle`).
- **Idle share and untrusted cost hours:** idle share and cluster totals in the export (opt-in `include_idle`), Prometheus gauges and the Footprint page; export chunks no longer drop a boundary sample; `untrusted_cost_hours` per measurement (`fix/idle-share-exposure`).
- **Shared-host history, GPU badge, upgrade hour:** recompute groups pre-0183 rows with the current snapshot ids (per node); the nodes page flags estimated or unmeasured GPU power; the 0.3.x upgrade hour leaves CPU/RAM components NULL instead of diluting them (`fix/impact-followups`).

**Still open:**
- **PostgreSQL compressor stall (lane: storage, in progress, high):** a compressor test can stall mid-run on PostgreSQL, timing-dependent; possibly a lock wait introduced by the dirty-hour locking (`fix/compressor-lock-hang`).
- **Closed database reopens silently (lane: storage):** a closed `DatabaseManager` reopens a connection on first use. Make use after close an error.
- **Hourly provenance (lane: storage, with C.3):** the hourly tables do not keep `embodied_source` or `gpu_source`.
- **Energy findings left (lane: impact):** NRG-14 (operators set server power: annotation, node profiles, measured tier), NRG-16 (Wattnet 15-minute points, proxy zones), NRG-17 (doc and code drift, PUE overrides).
- **Cross-repository Git charts (lane: gitops):** blocked with a clear reason; planning them (chart in one repository, values in another) is a feature to design.

## 5. Phase B: cut the scope

### B.0 Removal procedure (applies to every B task)
1. **Code:** remove the source module, its registry entries, API routes, UI pages and tests in one branch per area.
2. **Settings:**
   - removed settings are accepted and ignored for one release, with one warning at start naming them;
   - their Helm values are mapped to nothing with a NOTES warning;
   - the schema marks them deprecated.
3. **Data:**
   - stop writing first;
   - drop tables and columns in a later migration, after a release, with a backup note in `docs/upgrading.md`;
   - recommendations of removed types are retired with reason `type_removed` (a migration), never deleted silently;
   - the ledger is never rewritten.
4. **Docs:** update README, `docs/`, `docs/sources/`, both websites, and add a changelog "Removed" entry.
5. **Acceptance:**
   - the gates pass;
   - an upgrade test from the current `dev` data keeps the ledger and its chain valid;
   - grep finds no reference to the removed names outside the changelog and the upgrade notes.

### B.1 Remove the native advisory analyzers (lane: recommendations)
- **Analyzers:** zombie pods, idle namespaces, off-peak scaling, autoscaling candidates, carbon-aware scheduling, over/under-provisioned nodes, and the Karpenter NodePool heuristic (`core/optimization/analyzers/*`, `providers/karpenter.py`).
- **Recommendation types to retire:** `ZOMBIE_POD`, `IDLE_NAMESPACE`, `OFF_PEAK_SCALING`, `AUTOSCALING_CANDIDATE`, `CARBON_AWARE_SCHEDULING`, `OVERPROVISIONED_NODE`, `UNDERUTILIZED_NODE`.
- **Keep:** the orphaned LoadBalancer and PersistentVolume analyzers and everything they use (B.2). Keep the Karpenter, Cluster Autoscaler and HPA collectors: the realization factor reads them.

### B.2 Harden orphaned LoadBalancer and PersistentVolume detection (lane: recommendations, after B.1)
Kept because it is cheap, simple and gives an immediate saving. It is the highest-risk change kind, so it gets three fixes:
- **No false orphan:** a LoadBalancer Service owned by a Gateway, an ingress controller or a mesh (owner references, Gateway API `status.addresses`, well-known labels of Cilium, Istio, Envoy Gateway, ingress-nginx, Traefik) is never orphaned. Add the Cilium Gateway case seen earlier as a regression test.
- **Honest price:** a volume on non-cloud CSI or NFS storage is priced from the configured on-premises storage rate or shown as released capacity (GiB), never at the cloud default rate; OpenCost PV and LB costs are not annualized over the whole lookback (IMP-11).
- **Honest measurement:** the realized saving of a deletion is read from cost data before and after (OpenCost asset cost, or the configured rate times the observed lifetime), with its coverage; when no such data exists, the verdict is `inconclusive`, never the restated projection (MEA-15).
- **Acceptance:** the three cases above are tests; the deletion safety re-check still runs before every delete.

### B.3 Remove connectors and the chart CronJobs (lane: signals)
- **Connectors:** Kubecost, GKE recommender, Azure Advisor, Polaris, replay.
- **Chart:** the KRR and Polaris CronJob templates.
- **Keep:** the native, VPA (with Goldilocks provenance), KRR and push connectors.
- **Add:** a `docs/sources/krr.md` example that runs KRR and posts to the push API.
- **Simplify consensus** to one policy:
  - the native measured value decides the size;
  - external sources move confidence up or down;
  - a source alone never creates a recommendation (A.5).
- Remove the `safest`, `median`, `weighted` and `priority` options and their settings.

### B.4 Remove unused dimensions (lane: collection)
- **PromQL and columns:** network receive and transmit, disk read and write, ephemeral storage, PVC request and usage. Orphaned-volume detection reads PV and claim status, not usage, so it is not affected.
- **Collectors stay:** Karpenter, Cluster Autoscaler, HPA, LB, PV, deletion candidates, Wattnet, Boavizta, DCGM.
- **Acceptance:** the number of range queries per collection chunk drops accordingly (counted in a test with a recording Prometheus fake).

### B.6 Remove the legacy verifier, v1 ledger reads and FOCUS (lane: measurement, after C.5)
- Change measurement becomes the only verification path. The v1 savings tables are read only by the C.5 migration, then dropped.
- **Remove:**
  - the FOCUS export;
  - delivery modes `patch` and `ci` (`automation/delivery.py`, `cli/plan.py apply-patch` and `report-delivery`, the `plan_delivery` router).

### B.7 Remove the sustainability score and trim exposition (lane: UI and API)
- **Remove:**
  - the sustainability score (API-06);
  - the large Grafana dashboard.
- **Keep** the Footprint pages (nodes, report) and their export routes: carbon stays as it is.
- **Trim** `api/metrics_endpoint.py` to:
  - operational metrics: collection lag and coverage, loops, scans, measurement and ledger health;
  - the savings counters from ledger v2.
- **Keep** one small operations dashboard JSON.

## 6. Phase C: make it light

### C.1 Footprint benchmark (lane: ops; first task of the phase)
- **Harness:** `hack/footprint/`, running on kind:
  - deploys GreenKube with default values;
  - deploys a synthetic workload of N pods (100, 500, 2,000), using small idle pods or the kube-state-metrics and cAdvisor series of a fixture Prometheus;
  - runs for 2 hours.
- **It records:**
  - per-container CPU and memory (`container_cpu_usage_seconds_total`, `container_memory_working_set_bytes`);
  - database size growth per hour;
  - Prometheus queries per cycle;
  - scan duration and peak memory.
- **Output:** a JSON report compared with the budget of section 3, run nightly. The first run on `dev` sets the baseline numbers in this roadmap.

### C.2 One process by default; renderer on demand (lane: ops)
- **Default topology:** one Deployment running the API and the controller loops together (single replica, leader election kept for safety), plus PostgreSQL.
- **Optional:** API replicas and workers, for large installs only.
- **Renderer:** the sandbox runs as a short-lived Kubernetes Job per render, with the same isolation (no egress, read-only root, resource limits). No long-running renderer pod.
- **Intervals:** discovery every 5 minutes, scans every 6 hours, lifecycle every 5 minutes.
- **Acceptance:**
  - the C.1 benchmark shows the budget met for CPU and memory at 500 pods;
  - previews still render;
  - the NetworkPolicy tests still pass.

### C.3 Bounded storage at useful granularity (lane: storage and measurement; largest task, start with a design note)
Lightness must not come from aggregations too coarse to be useful. The granularity stays; the volume is bounded.
- **Tiers:**
  - **raw** (`combined_metrics`, `container_metrics`, `node_idle_metrics`, 5-minute step): kept for the rightsizing lookback, 14 days by default (`METRICS_RAW_RETENTION_DAYS`, never below the lookback; a start-up check refuses a lower value). Rightsizing percentiles are computed on these raw samples, not on hourly maxima (ROL-04);
  - **hourly per pod and per container** (the current hourly tables, with the WP-05 fixes): bounded retention, default 400 days, instead of forever;
  - **hourly per workload** (`workload_usage_hourly`) and **hourly per node** (new, keeps the idle share, ROL-07): long-term history, kept as long as the ledger needs them.
- **Footprint levers:**
  - PostgreSQL range partitioning by day (raw) and by month (hourly), so retention drops a partition instead of running large `DELETE`s, and vacuum stays cheap;
  - narrower rows: the dimensions removed by B.4 go, and the hourly tables keep only the columns a read path uses (STO-14);
  - each read is served by the coarsest tier that answers it (UI ranges over 14 days read hourly, the ledger reads workload-hours).
- **Measurement** (`verify/series.py`) reads per-pod hourly rows for the baseline and post windows, as today; the baseline snapshot freezes them. The audit findings marked "re-check after C3" in the appendix are re-checked and closed.
- **Acceptance:**
  - the rightsizing p95 on a fixture with a short spike equals the raw-sample p95, not the hourly maximum;
  - a measurement on fixture data gives the same verdict and values (within 1 %) before and after partitioning;
  - retention drops partitions (test on PostgreSQL);
  - database size at 500 pods is under the budget of section 3 in the C.1 benchmark.

### C.4 PostgreSQL only (lane: storage; decided)
- PostgreSQL is the production path, the one tested on the field cluster, and it allows managed databases and HA. Do C.4 before C.3, so partitioning is written for one dialect only.
- **What goes:** the SQLite repositories, the SQLite migrations, `aiosqlite`, the dialect switches and the dual-dialect tests. The test suite runs on PostgreSQL only (a disposable database per run, as the agents do today).
- **Upgrade:** a SQLite install gets a one-shot `greenkube migrate-to-postgres` command (copy every table, re-verify the ledger chain), documented in `docs/upgrading.md`; GreenKube refuses to start on a SQLite URL with a message pointing to it.
- **The demo** runs with `docker compose` (GreenKube plus PostgreSQL) or against a disposable PostgreSQL started by `greenkube demo`.
- **Acceptance:** grep finds no SQLite code path outside the migration command; the migration command keeps the ledger chain valid on a fixture.

### C.5 Migrate v1 ledger and legacy verification data (lane: measurement)
Convert the remaining v1-only records into ledger v2 entries with a `legacy` basis and their provenance. Re-verify the chain. Then stop reading v1 (enables B.6).

### C.6 Process hygiene (lane: ops)
- **Scans:** bounded memory, workloads streamed in batches.
- **Imports:** lazy for rarely used modules (renderers, OIDC).
- **Server and database:** one Uvicorn worker; asyncpg pool size 5.
- **Caches:** none unbounded (the timeseries and summary caches get size and age limits).
- **Python image:** `python -X importtime` audit, and drop unused dependencies (check `aiofiles`, `anyio`, `structlog`).
- **Acceptance:** the C.1 benchmark meets the memory budget with margin.

## 7. Phase D: finish the audit work that still applies

The appendix lists all 173 audit findings with their status:
- referenced by a commit on `dev`;
- on an unmerged branch;
- obsolete because of the scope cut;
- to re-check after C.3;
- open.

Open findings and the A.9 follow-ups are done in this order, each with its audit fix specification:
1. **WP-10 (API and exposition):** API-01, API-02, API-09, API-13, API-08, API-10, API-11, API-07 and API-14; after B.7 trims the exposition.
2. **Remaining WP-01 tasks:**
   - pod metadata gaps and the repair migration;
   - COL-05 rest, COL-11, COL-12, COL-15, COL-16, COL-20, COL-21.
3. **Remaining WP-06/07/08/09 tasks:**
   - MEA-13, MEA-16 rest, MEA-14 rest;
   - IMP-04, IMP-06, IMP-07, IMP-08 (the realized-versus-projected calibration loop: the fourth promise of the product, see E.2);
   - LED-05 rest, LED-06, LED-09, LED-11, LED-12.
4. **Unaudited dimensions,** in this order:
   - GitOps delivery: the field test found F-01, F-02, F-11 and F-13 there;
   - long-term operations;
   - sources and consensus, after B.3.
   
   Use the existing audit briefs in the maintainer's audit workspace.

## 8. Phase E: prove and release

### E.1 Release gate
- A.8 has produced decided verdicts on the field cluster, with:
  - a projection accuracy within 20 % on at least three changes;
  - ledger entries with `chain.ok`;
  - a backup and restore that keeps the chain valid.
- The C.1 benchmark meets the budget.
- All gates are green on the release branch.
- **The maintainer chooses the version number;** the chart version and UI banner follow it.

### E.2 Calibration loop (IMP-08, LED-07)
- Use measured outcomes to correct projections per recommendation kind and source:
  - a ratio realized / projected, with its uncertainty, shown on the Sources page;
  - applied to the next projections, with a minimum number of decided verdicts before it applies.
- This is the fourth promise of the product, and the first feature to build after the release.

## 9. Maintainer decisions

| # | Decision | Status |
|---|---|---|
| D1 | Scope table of section 2.1 | **Decided 2026-11-20:** connectors and advisory analyzers removed; carbon kept as it is (Wattnet, Boavizta and DCGM stay); collectors kept |
| D2 | One SQL dialect | **Decided 2026-11-20:** PostgreSQL only (C.4) |
| D3 | Deletion recommendations | **Decided 2026-11-20:** orphaned LoadBalancer and PersistentVolume detection kept and hardened (B.2) |
| D4 | KRR CronJob in the chart | Open; proposed: remove, document a push example |
| D5 | Footprint budget of section 3 | **Decided 2026-11-20:** lightness must not cost useful granularity (C.3); numbers revised after the first C.1 run |
| D6 | Push the two website repositories (local `main` commits not pushed; pushing may deploy) | Open; proposed: push after reading the diff |
| D7 | Footprint pages, FOCUS export, `patch` and `ci` delivery modes | Open; proposed: keep the Footprint pages, remove the other three |

## 10. Execution conventions

- Branches `fix/<slug>`, `feat/<slug>`, `docs/<slug>` from `dev`.
- One-line conventional commits in English, no body, no trailers. Merge with `--no-ff`.
- Every change goes through an independent review and the full gates (ruff, pyrefly, the test suite on SQLite until C.4 and on PostgreSQL, Helm, promtool, frontend) before it is merged.
- Each package has its own test database, created and dropped by the agent.
- Changelog fragments go in `changes/unreleased/`.

## Appendix: audit findings and their status

Status meanings:
- **addressed on dev (WP-x.y):** the task of the previous roadmap revision that covers the finding was implemented, independently reviewed and merged. Its acceptance tests are in the suite. It has not yet been confirmed on a real cluster.
- **partly addressed:** the note says what is missing.
- **obsolete:** the subject leaves the product (section 2.1).
- **open; re-check after C3:** to re-check once the storage tiers of C.3 land.
- **open:** to do in phase D.

The "Package" column keeps the work-package numbering of the previous revision (WP-01 to WP-10). That numbering is still in the git history of this file and in the audit.

| Package | ID | Sev | Finding | Status |
|---|---|---|---|---|
| WP-01 | COL-01 | high | Requests, owners and node zone/provider/PUE are read from the Kubernetes API at collection time, so rows of deleted p... | addressed on dev (WP-01.1) |
| WP-01 | RECON-05 | high | When a node is no longer in the Kubernetes API at collection time, its historical rows fall back to DEFAULT_ZONE, the... | addressed on dev (WP-01.1) |
| WP-01 | RECON-01 | high | Backfilled or re-collected rows take pod requests, owner and PVC data from the Kubernetes API as it is at collection ... | addressed on dev (WP-01.1) |
| WP-01 | COST-07 | medium | Historical windows are priced with the requests and nodes read from the live API at collection time: rows of pods tha... | addressed on dev (WP-01.1) |
| WP-01 | NRG-10 | medium | Zone and PUE are taken from the live node list only: pods of a node that left the cluster before its window is collec... | addressed on dev (WP-01.1) |
| WP-01 | NRG-11 | medium | Reservation shares use the requests of pods alive at collection time: rows of terminated pods (last steps after a rol... | addressed on dev (WP-01.1) |
| WP-01 | RECON-06 | high | Container rows are produced only for pods that still exist at collection time and, on runtimes without container-leve... | addressed on dev (WP-01.1) |
| WP-01 | MEA-12 | medium | Rows of dead or terminating pods that carry no owner and zero requests are attached to the workload by pod-name infer... | addressed on dev (WP-01.1) |
| WP-01 | COL-04 | high | Holes are not recorded anywhere: windows older than the backfill horizon only increment an in-memory counter, and a w... | addressed on dev (WP-01.2) |
| WP-01 | COL-03 | high | Rows are created only for pods that have a CPU sample at the step; memory, network and OpenCost cost of every other p... | addressed on dev (WP-01.2) |
| WP-01 | COL-08 | medium | When the carbon-intensity lookup raises, every pod row of the (step, node) group is skipped without log, metric or er... | addressed on dev (WP-01.2) |
| WP-01 | COL-07 | medium | A failed Kubernetes pod or node list is swallowed (collectors return an empty result), and the window is stored with ... | addressed on dev (WP-01.2) |
| WP-01 | RECON-04 | high | Windows older than COLLECTION_MAX_BACKFILL are abandoned although Prometheus still holds them, and the hole is record... | addressed on dev (WP-01.2) |
| WP-01 | RECON-12 | medium | Rows can be dropped without any trace while the window is committed: a failed intensity lookup skips every pod of a n... | addressed on dev (WP-01.2) |
| WP-01 | NRG-08 | medium | Lookup and Kubernetes API errors are swallowed inside run_range: an intensity lookup error drops every pod of the nod... | addressed on dev (WP-01.2) |
| WP-01 | COL-02 | high | The cluster key of the collection watermark falls back to the node name when the node list fails at start, so each co... | addressed on dev (WP-01.3) |
| WP-01 | STO-09 | medium | Metric rows carry no cluster identity and nothing prevents two installs from writing the same database schema; keys a... | addressed on dev (WP-01.3) |
| WP-01 | COST-04 | high | The collection watermark is keyed by the cluster name, which on the kind install fell back to the controller's node n... | addressed on dev (WP-01.3) |
| WP-01 | RECON-10 | medium | The collection watermark is keyed by the auto-detected cluster name, and a missing watermark row is treated as a firs... | addressed on dev (WP-01.3) |
| WP-01 | LED-02 | high | The v1 ledger, the accrual watermark and the v2 projection are keyed by CLUSTER_NAME, which falls back to the pod's n... | addressed on dev (WP-01.3) |
| WP-01 | COL-05 | high | On clusters whose cAdvisor exports only pod-level series (cri-dockerd: Minikube Docker driver, Docker Desktop, Mirant... | partly addressed on dev (WP-01.4): COL-17 and COL-09 done, COL-05 partly |
| WP-01 | COL-17 | medium | Container rows are all-or-nothing per pod: when one container of the pod spec has no usage sample at the step, no con... | partly addressed on dev (WP-01.4): COL-17 and COL-09 done, COL-05 partly |
| WP-01 | COL-09 | medium | The pod-cgroup fallback is used only when the container query returns nothing for the whole cluster, so in a cluster ... | partly addressed on dev (WP-01.4): COL-17 and COL-09 done, COL-05 partly |
| WP-01 | RECON-07 | high | Container usage filled from the Metrics API is stored with usage_source = prometheus and is clamped one-sidedly to th... | partly addressed on dev (WP-01.4): COL-17 and COL-09 done, COL-05 partly |
| WP-01 | COL-11 | medium | CPU is aggregated with max over all series of a container, so consecutive container instances inside one rate window ... | open |
| WP-01 | COL-12 | medium | Memory is stored as one instant value per step (the working set at the end of the step), so peaks between two evaluat... | open |
| WP-01 | COL-14 | medium | Network figures of hostNetwork pods are the node's traffic, repeated for every hostNetwork pod of the node, and the s... | obsolete: network no longer collected |
| WP-01 | COL-13 | medium | Persistent-volume request and usage per pod are joined by PVC name without the namespace, so same-named claims in dif... | obsolete: PVC usage no longer collected |
| WP-01 | COL-18 | low | Sum-based queries (network, disk, restarts, ephemeral storage) double when the same series is visible twice, and part... | open |
| WP-01 | COL-20 | low | restart_count stores the lifetime restart counter of the pod, and consumers read it as restarts of the observed period. | open |
| WP-01 | COL-19 | low | Ephemeral-storage usage is never collected on CRI runtimes, and network and disk are absent on cri-dockerd; the produ... | obsolete: network, disk and ephemeral storage no longer collected |
| WP-01 | RECON-15 | low | Against a non-deduplicated HA Prometheus pair the stored usage depends on which replica answers: row values differ by... | open |
| WP-01 | COL-15 | medium | Cloud provider detection classifies GKE nodes in us-* regions, and Linode, Oracle, IBM and Alibaba nodes, as AWS, and... | open |
| WP-01 | COL-16 | medium | Workload owners are derived by cutting the last dash-separated token of the ReplicaSet name and calling the result a ... | open |
| WP-01 | COL-06 | medium | A single failed instant query makes PrometheusCollector replace the configured Prometheus URL with a discovered one f... | open |
| WP-01 | COL-21 | low | Tests cannot detect PromQL semantic errors: the fake Prometheus takes pre-computed rates and the alternative label na... | open |
| WP-02 | NRG-06 | high | Energy, CO2e and cost are stored as float4 (REAL) in PostgreSQL and summed with SUM(real): totals drift by percents b... | addressed on dev (WP-02.1) |
| WP-02 | STO-11 | low | The PostgreSQL carbon recompute compares a float4 column with a float8 intensity (`IS DISTINCT FROM`), so every row w... | addressed on dev (WP-02.1) |
| WP-02 | STO-01 | high | Every metrics upsert overwrites all columns unconditionally and never removes rows the new pass did not produce, so a... | addressed on dev (WP-02.2) |
| WP-02 | STO-04 | medium | The SQLite metrics writer swallows per-row errors and still commits, so the window is reported written and the waterm... | addressed on dev (WP-02.3) |
| WP-02 | STO-03 | medium | A collection window is committed in four independent transactions (pod rows, container rows, idle rows, watermark) an... | addressed on dev (WP-02.3) |
| WP-02 | STO-05 | medium | combined_metrics.id and combined_metrics_hourly.id are 32-bit SERIAL and every upsert consumes a sequence value even ... | addressed on dev (WP-02.4) |
| WP-02 | STO-10 | medium | PostgreSQL time buckets and report years are computed with date_trunc/EXTRACT in the session time zone, which the poo... | addressed on dev (WP-02.4) |
| WP-02 | ROL-08 | medium | Day, week and month buckets are computed with date_trunc in the PostgreSQL session time zone, which the connection po... | addressed on dev (WP-02.4) |
| WP-02 | STO-08 | medium | Unknown requests, cost and energy are stored as 0 (model and schema defaults) instead of NULL, so SUM, AVG and MAX tr... | open |
| WP-02 | STO-18 | low | Neither CombinedMetric nor the metric tables reject NaN, infinite or negative values, and a single NaN stored in a Po... | open |
| WP-02 | STO-17 | low | CPU usage is stored as integer millicores (and hourly averages are cast to INTEGER), so pods using less than 0.5 m ar... | open |
| WP-02 | NRG-07 | high | Two writers flip node_snapshots_scd.embodied_emissions_kg between NULL and the value every cycle: the SCD table grows... | open |
| WP-02 | STO-06 | medium | Node snapshot versions (SCD2) are compared against float4 columns and written without a transaction or a uniqueness c... | open |
| WP-02 | STO-07 | medium | No read path uses calculation_version or model_version, the hourly tables do not keep model_version, and rows written... | open; re-check after C3 |
| WP-02 | STO-12 | low | SQLite stores timestamps as ISO text with variable fractional precision, so range bounds that fall in the same second... | obsolete: SQLite removed (C.4) |
| WP-02 | STO-13 | low | pod_count in summaries is COUNT(DISTINCT pod_name) without the namespace, so same-named pods of different namespaces ... | open |
| WP-02 | STO-15 | low | All pending migrations run in one transaction that takes ACCESS EXCLUSIVE locks on the metric tables and then rewrite... | open |
| WP-02 | STO-16 | low | `greenkube db copy` drops all metric history by default and the documentation says it is re-collected, although only ... | open |
| WP-03 | COST-01 | high | An OpenCost outage, empty answer or rejected request silently switches the window to list-price or default-rate cost,... | addressed on dev (WP-03.1) |
| WP-03 | RECON-09 | medium | An OpenCost outage or lag is stored as modelled custom_rate cost (+10.8% in the simulation), the window is marked com... | addressed on dev (WP-03.1) |
| WP-03 | COST-05 | medium | OpenCost allocations that carry no usable cost (minutes = 0, or sub-precision figures that cannot be rebuilt) are sto... | addressed on dev (WP-03.2) |
| WP-03 | RECON-11 | medium | OpenCost allocations with zero duration (minutes = 0, start = end) are stored as a trusted OpenCost cost of 0 for pod... | addressed on dev (WP-03.2) |
| WP-03 | COL-10 | medium | In multi-step (catch-up or backfill) windows each row of a pod gets OpenCost cost x step / running time, so pods that... | addressed on dev (WP-03.3) |
| WP-03 | RECON-08 | medium | The OpenCost cost of a pod is spread over its Prometheus rows with `step / (minutes x 60)`, so the stored cost depend... | addressed on dev (WP-03.3) |
| WP-03 | COST-02 | high | The pod cost is split between containers by CPU usage share instead of reservation, so container costs and the unit p... | addressed on dev (WP-03.4) |
| WP-03 | IMP-02 | high | Container rows receive the pod's CPU cost by CPU-usage share (not reservation share), so per-container request cuts a... | addressed on dev (WP-03.4) |
| WP-03 | COST-03 | high | collect_pv_costs and collect_lb_costs never return a cost with a real OpenCost (wrong query parameter and response fi... | addressed on dev (WP-03.5) |
| WP-03 | COST-06 | medium | The OpenCost credentials (OPENCOST_BEARER_TOKEN, OPENCOST_USERNAME, OPENCOST_PASSWORD) are sent only by the discovery... | addressed on dev (WP-03.6) |
| WP-03 | COST-10 | medium | The OpenCost query has no cluster filter and aggregates by namespace and pod only, so a multi-cluster OpenCost or Kub... | addressed on dev (WP-03.6) |
| WP-03 | COST-09 | medium | The bundled price catalogs cover a small part of the fleets (GCP empty, AWS 35 families, Azure 100 sizes, missing the... | open |
| WP-03 | COST-12 | low | Projections read every stored positive cost as an observed cost: default-rate rows get confidence 0.7 instead of 0.5 ... | open |
| WP-03 | COST-11 | low | Currencies are labels, not checked values: the USD default rates are relabelled with IMPACT_PRICING_CURRENCY, OpenCos... | open |
| WP-03 | COST-08 | medium | Cost provenance stops at the database: the API and UI show one cost figure that mixes OpenCost costs, list prices, ge... | open |
| WP-04 | NRG-01 | critical | The Boavizta response model does not match the real API (gwp.embedded/use are objects), so every lookup fails and eve... | addressed on dev (WP-04.1) |
| WP-04 | NRG-03 | high | The hourly carbon-intensity job never fetches DEFAULT_ZONE: clusters whose nodes carry no mappable zone/region label ... | addressed on dev (WP-04.2) |
| WP-04 | NRG-04 | high | The bundled zone-default intensity table is a one-day snapshot with 251 of 349 zones empty and malformed rows (ES, SE... | addressed on dev (WP-04.2) |
| WP-04 | NRG-05 | high | Projections use a different node power model from collection: for every node without a bundled instance profile the p... | addressed on dev (WP-04.3) |
| WP-04 | NRG-12 | medium | Node CPU utilisation is the sum of pod CPU only: kubelet, container runtime, kernel and other non-pod CPU are ignored... | addressed on dev (WP-04.4) |
| WP-04 | NRG-13 | medium | GPU energy is counted only when DCGM series exist; GPU nodes without the DCGM exporter are neither detected nor flagg... | addressed on dev (WP-04.4) |
| WP-04 | NRG-15 | medium | Nodes that share one physical host (kind, k3d, minikube multi-node, nested clusters) are each charged the full host c... | addressed on dev (WP-04.4) |
| WP-04 | NRG-14 | medium | On-prem and bare-metal nodes are modelled with hyperscaler per-vCPU averages and the operator cannot supply real serv... | open |
| WP-04 | NRG-16 | low | Wattnet's 15-minute intensities are stored but only the :00 point of each hour is ever used (default hour normalisati... | open |
| WP-04 | NRG-17 | low | Methodology docs and code disagree, and dead legacy estimator code with a different model is still shipped. | open |
| WP-05 | ROL-01 | high | Every reader that combines raw and hourly tables splits at wall-clock now - METRICS_COMPRESSION_AGE_HOURS instead of ... | addressed on dev (WP-05.1) |
| WP-05 | RECON-03 | high | Every aggregate that spans the raw/hourly boundary uses `now - METRICS_COMPRESSION_AGE_HOURS` instead of the compress... | addressed on dev (WP-05.1) |
| WP-05 | API-03 | high | Every API aggregate that spans more than 24 hours reads hourly rows up to wall-clock now - 24 h and raw rows after it... | addressed on dev (WP-05.1) |
| WP-05 | MEA-18 | low | read_combined_metrics_smart splits raw and hourly data at a cutoff that is not hour-aligned, so each measurement read... | addressed on dev (WP-05.1) |
| WP-05 | ROL-02 | high | Rows written or updated in an already compressed hour never reach combined_metrics_hourly / container_metrics_hourly ... | addressed on dev (WP-05.2) |
| WP-05 | STO-02 | medium | Hourly compression is not bounded by the collection watermark in production (the provider is never passed), so hours ... | addressed on dev (WP-05.2) |
| WP-05 | RECON-02 | high | Raw rows written into an already compressed hour (late backfill, greenkube start --last, greenkube recompute, repaire... | addressed on dev (WP-05.2) |
| WP-05 | NRG-02 | high | Cluster totals (API, UI Footprint page, yearly report) silently exclude the node __idle__ share; idle rows are write-... | addressed on dev (WP-05.3) |
| WP-05 | ROL-07 | medium | node_idle_metrics has no hourly aggregate and is deleted with raw retention, so node totals (pods + idle) cannot be r... | addressed on dev (WP-05.3) |
| WP-05 | NRG-09 | medium | The intensity consolidation recompute updates pod and container rows but not node_idle_metrics, so idle rows keep the... | addressed on dev (WP-05.3) |
| WP-05 | COST-13 | low | Idle cost is always priced with the model, does not reconcile with OpenCost pod costs (pods + idle differ from the no... | addressed on dev (WP-05.3) |
| WP-05 | API-04 | high | No API total, chart, breakdown, report or Prometheus series includes the node idle share (node_idle_metrics is read b... | addressed on dev (WP-05.3) |
| WP-05 | RECON-13 | low | Node idle rows have no hourly aggregate and are deleted at raw retention, and the carbon-intensity recompute skips th... | addressed on dev (WP-05.3) |
| WP-05 | ROL-03 | high | Change measurements built from compressed hourly rows overstate cost rate, replicas and core-hours for every hour in ... | addressed on dev (WP-05.4) |
| WP-05 | MEA-05 | high | One untrusted cost sample makes the whole pod-hour untrusted once the hour is compressed (cost_source becomes 'mixed'... | addressed on dev (WP-05.4) |
| WP-05 | MEA-04 | high | Hour buckets built from hourly rollup rows (all production data older than 24 h) take the longest single pod as the c... | addressed on dev (WP-05.4) |
| WP-05 | ROL-04 | medium | Lookback usage percentiles used for rightsizing are nearest-rank percentiles of hourly percentiles, and with a 5-minu... | open; re-check after C3 |
| WP-05 | REC-07 | medium | Lookback 'p95/p99' are nearest-rank percentiles of hourly percentiles (themselves nearly hourly maxima), so the value... | open |
| WP-05 | ROL-11 | low | A usage-rollup hour rebuilt from compressed rows uses each pod's hourly maximum as its only point, so p50-p99 of that... | open; re-check after C3 |
| WP-05 | ROL-06 | medium | `greenkube recompute` rewrites raw rows only: hourly aggregates and (on PostgreSQL) the usage rollup keep the old mod... | open |
| WP-05 | ROL-05 | medium | Compressed hours read back with a larger PROMETHEUS_QUERY_RANGE_STEP are counted as covering sample_count x new step,... | open; re-check after C3 |
| WP-05 | ROL-09 | medium | With METRICS_AGGREGATED_RETENTION_DAYS = -1 (default) three per-hour tables grow forever, including workload_usage_ho... | open; re-check after C3 |
| WP-05 | ROL-10 | medium | Report/export aggregation averages CPU and memory usage with one vote per row, so a compressed hourly row weighs like... | open; re-check after C3 |
| WP-05 | ROL-12 | low | Hourly compression silently drops or approximates per-row information: estimation reasons, model version, intensity t... | open; re-check after C3 |
| WP-05 | ROL-13 | low | Dashboard caches can serve stale data without saying so: the time-series cache keeps old buckets when a window become... | open; re-check after C3 |
| WP-05 | RECON-14 | low | aggregate_timeseries returns CPU and memory as sums of samples: the raw part of a bucket is steps-per-bucket times la... | open; re-check after C3 |
| WP-05 | STO-14 | low | The hourly tables drop columns of the raw rows (ephemeral storage, grid_intensity_timestamp, model_version, period) a... | open; re-check after C3 |
| WP-06 | REC-04 | high | Rightsizing fails open when reliability signals are unknown: with oom_kill_count/restarts_in_window = None (no kube-s... | addressed on dev (WP-06.1) |
| WP-06 | REC-01 | high | Once a rightsizing recommendation of a container is in status 'applied', every later recommendation of the same type ... | addressed on dev (WP-06.2) |
| WP-06 | REC-06 | medium | Ignoring a recommendation does not survive the next scan: upsert_recommendations only matches rows in status 'active'... | addressed on dev (WP-06.2) |
| WP-06 | REC-03 | high | The decrease target is not checked against the engine's own increase trigger: a decrease to max(p95,(avg+max)/2)x1.2 ... | addressed on dev (WP-06.3) |
| WP-06 | IMP-13 | low | The 5% revision hysteresis is a one-step relative band without memory: proposals that alternate between two nearby va... | addressed on dev (WP-06.3) |
| WP-06 | REC-02 | high | OOM kills are counted over the whole lookback regardless of request/limit changes, so after an OOM fix is applied the... | addressed on dev (WP-06.4) |
| WP-06 | MEA-11 | medium | The OOM-kill baseline rate of a reliability increase divides kills counted over the whole scan window by an exposure ... | addressed on dev (WP-06.4) |
| WP-06 | REC-05 | high | Recommendations are generated for workloads and containers that no longer exist: any target with one hour of usage in... | addressed on dev (WP-06.5) |
| WP-06 | IMP-01 | high | Revision hysteresis freezes a recommendation's projected savings (impact, potential_savings_*, evidence) at the value... | open |
| WP-06 | REC-12 | medium | Revision hysteresis freezes the whole record (evidence, sample count, coverage, description statistics, projected sav... | open |
| WP-06 | IMP-09 | medium | Minimum-request clamping is applied after the projection, so a clamped recommendation stores one value and the projec... | open |
| WP-06 | REC-08 | medium | The decrease coverage gate measures the span between the first and last observed hour, not how much of it was observe... | open |
| WP-06 | REC-13 | medium | Evidence coverage_ratio divides replica-samples by single-series expected steps, so a workload with N replicas report... | open |
| WP-06 | REC-15 | medium | Native rightsizing ignores HorizontalPodAutoscalers: a CPU/memory request change on an HPA-scaled workload moves the ... | open |
| WP-06 | REC-17 | low | The minimum-request floor is applied to increases after the step cap, so containers with tiny requests get 'reliabili... | open |
| WP-06 | REC-09 | medium | Static (mirror) control-plane pods are merged into one fake workload 'Node/<node name>' and receive rightsizing and '... | addressed on dev (F-04) |
| WP-06 | REC-10 | medium | Zombie and idle-namespace detection compare an absolute energy total over a variable window (1000 J) instead of a rat... | obsolete: zombie and idle-namespace analyzers removed |
| WP-06 | REC-11 | medium | Off-peak 'scale to zero N h/day' recommendations are issued from a single day of data (6 samples in total suffice): e... | obsolete: off-peak analyzer removed |
| WP-06 | REC-14 | medium | AUTOSCALING_CANDIDATE is driven by relative noise on near-zero CPU: 3 samples suffice, there is no minimum absolute u... | obsolete: autoscaling-candidate analyzer removed |
| WP-06 | REC-16 | low | OVERPROVISIONED_NODE compares only pod usage with capacity: it ignores requests, has no coverage requirement, flags c... | obsolete: node analyzer removed |
| WP-06 | IMP-10 | medium | The native node analyzer emits OVERPROVISIONED_NODE without any projection (and for control-plane nodes), although no... | obsolete: node analyzer removed |
| WP-07 | MEA-01 | high | A change verdict compares one baseline week with one post week, and the block bootstrap only sees within-week noise, ... | addressed on dev (WP-07.1) |
| WP-07 | MEA-09 | medium | A saving is declared 'realized' as soon as its interval is above zero, without having to exceed the noise floor, whil... | addressed on dev (WP-07.1) |
| WP-07 | MEA-07 | high | Health gates of the final decision have no baseline comparison and a zero restart tolerance over the whole 7-day post... | addressed on dev (WP-07.2) |
| WP-07 | MEA-02 | high | Price moves between the two windows are booked as savings (or erase them) whenever the change resized both CPU and me... | addressed on dev (WP-07.3) |
| WP-07 | MEA-03 | high | The measured CO2e saving is the raw difference of two weeks' emissions with no normalisation for grid carbon intensit... | addressed on dev (WP-07.3) |
| WP-07 | MEA-08 | high | Any pod-template change that leaves the requests untouched (an image deployment, an env change, a rollout restart) co... | addressed on dev (WP-07.4) |
| WP-07 | MEA-10 | medium | The accelerated end-to-end settings can never produce a decided verdict, so the statistics, attribution, drift and le... | addressed on dev (WP-07.5) |
| WP-07 | MEA-16 | medium | Two verification systems with different rules write the same 'verified' / 'not_realized' labels: recommendations appl... | partly addressed on dev (WP-07.6): interim verifier on an interval; separate interim totals not done |
| WP-07 | MEA-14 | medium | A reliability increase that works is still reported as 'not_realized' (recommendation verification 'failed'), because... | partly addressed on dev (WP-07.6): interim verifier on an interval; separate interim totals not done |
| WP-07 | MEA-13 | medium | Hours are rated per covered time and hours without rows are dropped, so a workload that runs part of each hour (batch... | open |
| WP-07 | MEA-15 | medium | A deletion is 'measured' by restating its projection: the pre-window cost of a Service or PersistentVolume cannot be ... | open |
| WP-07 | MEA-17 | low | The baseline snapshot is frozen in the detection pass, before the last baseline samples are written and before provis... | open |
| WP-08 | IMP-03 | high | On clusters without a detected autoscaler every rightsizing projection is 0 cost and 0 CO2e, reliability increases ar... | addressed on dev (WP-08.2) |
| WP-08 | IMP-05 | medium | Workload deletions (zombie pods, idle namespaces) are projected with a realization factor of 1 even on fixed nodes, s... | obsolete: zombie and idle-namespace analyzers removed |
| WP-08 | IMP-12 | low | The realization factor silently defaults to 1.0 until the controller process has completed a first autoscaler read (o... | addressed on dev (WP-08.2) |
| WP-08 | API-12 | medium | On clusters without a node autoscaler every rightsizing recommendation has potential_savings_cost = 0 and potential_s... | addressed on dev (WP-08.2) |
| WP-08 | MEA-06 | high | Without OpenCost or Kubecost no change can ever get a cost verdict: rows priced with configured rates (custom_rate) o... | open |
| WP-08 | IMP-04 | medium | Projections are annualized over the span since the target's first observed hour, so collection gaps and the hour-floo... | open |
| WP-08 | IMP-06 | medium | The 'projected' savings of applied changes and the user-declared bound use the realization-scaled (billable) projecti... | open |
| WP-08 | IMP-07 | medium | The UI sums the projected savings of overlapping recommendations (namespace deletion, workload deletion, off-peak, ri... | open |
| WP-08 | IMP-08 | medium | There is no feedback loop from measured outcomes to projections: realization factors, the incident cost and the annua... | open |
| WP-08 | LED-07 | medium | The 'compare realized to projected to refine the projection model' loop is not implemented: only a binary realized/no... | open |
| WP-08 | IMP-11 | low | Orphaned PersistentVolumes on non-cloud CSI or NFS storage are projected at the cloud default rate as an expected bil... | open |
| WP-09 | LED-01 | high | The ledger accrues 'projected' savings on the expected-billable basis (allocated x realization factor, 0 on fixed-cap... | addressed on dev (WP-09.1) |
| WP-09 | LED-04 | medium | The numbers users actually see (GET /recommendations/savings, overview hero, Prometheus counters and dashboard gauges... | addressed on dev (WP-09.2) |
| WP-09 | LED-03 | medium | Projected savings accrued before a NOT REALIZED or INCONCLUSIVE verdict stay in the ledger for ever (never reversed, ... | addressed on dev (WP-09.3) |
| WP-09 | LED-08 | medium | Ledger corrections on rollback are a one-shot best-effort side effect: a failed supersede call is never retried, late... | addressed on dev (WP-09.3) |
| WP-09 | LED-05 | medium | The hash chain is not tamper-evident against anyone who can write to the database: there is no secret or external anc... | partly addressed (fix/ledger-remaining): HMAC chain key from a chart-generated Secret, keyed-head gauge; no external checkpoint, no separate PostgreSQL role |
| WP-09 | LED-06 | medium | A forward clock jump makes the attributor accrue up to 168 future hourly slots per run and then freezes all accrual u... | addressed (fix/ledger-remaining): clock-skew refusal on PostgreSQL, per-recommendation cursors, watermark-ahead gauge; no repair command |
| WP-09 | LED-09 | medium | The ledger grows without bound at one v1 row and two v2 entries per accruing recommendation per hour, with full-table... | partly addressed (fix/ledger-remaining): materialized totals for all-time reads, checked by the full verification; no daily grain, partitioning or archival |
| WP-09 | LED-10 | medium | The FOCUS export books projected and user-declared amounts as negative EffectiveCost 'Credit' rows, so summing the st... | obsolete: FOCUS export removed |
| WP-09 | LED-11 | low | Ledger entries cannot be reproduced from their own provenance: price_card_version, intensity_g_per_kwh and intensity_... | partly addressed (fix/ledger-remaining): annual rate, currency and accrual-time measurement; price card and intensity still empty |
| WP-09 | LED-12 | low | Minor ledger inconsistencies: drift reversals with mixed signs are projected as accruals, zero-length periods are dro... | addressed (fix/ledger-remaining): (a) row_kind, (b) one-sided windows, (c) per-recommendation cursor, (d) reversal from stored rows |
| WP-10 | API-01 | high | An aggregated report export (aggregate=true) is aggregated per 7-day read chunk, so the file holds several rows for t... | open |
| WP-10 | API-05 | medium | The report export loses the rows stamped exactly on the first 7-day chunk boundary (a whole hour of every pod for dat... | open |
| WP-10 | API-16 | low | CSV/JSON export details: negative numbers are written as text with a leading apostrophe, a failure in the middle of a... | open |
| WP-10 | API-02 | high | The Prometheus exposition publishes the cost, CO2e and energy of ONE collection step as unit-less gauges named *_tota... | open |
| WP-10 | API-09 | medium | The exposition is rebuilt inside each scrape by clearing shared gauges and refilling them across awaits, so overlappi... | open |
| WP-10 | API-15 | low | Prometheus exposition naming and labels: gauges named *_total, a currency unit hard-coded as dollars, measured values... | open |
| WP-10 | API-13 | medium | Read endpoints carry no coverage or freshness information: no collected-until time, no hole indicator, no estimated s... | open |
| WP-10 | API-08 | medium | When no metric row is younger than 20 minutes, /prometheus/metrics publishes cluster cost, CO2e, energy and pod count... | open |
| WP-10 | API-11 | medium | The Footprint page mixes KPI cards read from an hourly cache with breakdowns computed live, and the cache keeps rows ... | open |
| WP-10 | API-10 | medium | pod_count is the number of distinct pod names seen in the whole window, but the UI shows it as 'Active Pods' and divi... | open |
| WP-10 | API-07 | medium | GET /api/v1/metrics pages with LIMIT/OFFSET ordered by timestamp only, and its cursor mode reads a different data set... | open |
| WP-10 | API-14 | low | The duration suffix 'm' means months: last=5m returns 150 days of data, and the parser's own docstring presents '5m' ... | open |
| WP-10 | API-06 | medium | The sustainability score is computed from one row per pod without node or HPA data: three of its seven dimensions are... | obsolete: sustainability score removed |

