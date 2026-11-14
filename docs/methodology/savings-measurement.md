# Savings measurement — methodology

**Strategy:** `request_rightsizing` version `2.0` (`greenkube.verify.strategy.RequestRightsizingStrategy`) · **Code:** `src/greenkube/verify/` · **Roadmap:** C1–C9, N9

GreenKube measures every applied change once, against a counterfactual: what the workload would have cost **without** the change. The method follows the spirit of IPMVP Option B (retrofit isolation with measurement of the affected system): a baseline period before the change, a reporting period after it, a model that adjusts the baseline to the reporting period's conditions, and an uncertainty statement.

This page is the reference for reviewing and auditing a measurement. When a rule below changes the numbers a measurement produces, the strategy version must change.

## Status

| Part | Status |
|---|---|
| Change records and baseline snapshots (C1) | **In use** for every change found by apply detection. |
| Measurement lifecycle and `/api/v2/changes` (C2) | **In use.** |
| Load drivers and hour-of-week counterfactual (C3, MVP) | **In use** as the fallback of the load regression. |
| Load-regression counterfactual (C3 v2) | **In use** when a load driver exists and the fit's diagnostics pass (strategy `2.0`). |
| Confounders, bootstrap interval, outcome rule (C4) | **In use**, including node-pool confounders (hardware, autoscaling, changes to neighbours). |
| One measurement per change, split across its recommendations (C5) | **In use.** |
| Append-only ledger entries, hash chain and restatement (C6) | **In use** ([section 12](#12-savings-ledger-v2-c6)); the v1 ledger is still written and read by the legacy views. |
| Ledger API and exports (C7) | **In use**: `/api/v2/ledger/entries`, `/summary`, `/export` (CSV and FOCUS-aligned). |
| Drift monitor (`realized → regressed`, reversal entries, C8) | **In use** ([section 10](#10-drift-monitor-c8)). |

Changes applied before this release, recommendations marked applied by hand (`PATCH /api/v1/recommendations/{id}/apply`, which apply detection never sees), and every change when `CHANGE_MEASUREMENT_ENABLED=false` are still measured by the interim verifier described in [recommendation.md](../recommendation.md#lifecycle-states).

## Vocabulary

| Term | Meaning |
|---|---|
| **Change** | One pod-template revision rolled out on one workload: a GitOps plan merged and synced, or a change detected in the cluster. Several recommendations (the CPU and memory requests of one or more containers) can belong to one change. |
| **Baseline window** | The period before the change used to predict the counterfactual. |
| **Post-window** | The period after the change in which the saving is measured. |
| **Counterfactual** | The predicted value of a quantity in a post-window hour had the change not been made. |
| **Saving** | Counterfactual minus actual, per hour. Signed: a negative saving is a cost increase. |
| **Measured** | A saving from this method. It is never added to a **projected** saving (the [Impact Model](impact-model.md) estimate made before the change). |

## 1. Recording the change (C1)

Apply detection reads every Deployment, StatefulSet and DaemonSet on each lifecycle pass. For each workload it records a **spec observation** whenever its UID, generation, pod-template digest or HPA digest differs from the previous one. The pod-template digest is the SHA-256 of the canonical JSON of the pod template, with empty fields removed and the `pod-template-hash` label ignored. When HPAs cannot be listed in a pass, the last known HPA digest is kept, so a failed listing is not recorded as an HPA change.

When a recommendation is detected as applied (its rollout has completed and the live requests match the proposal), GreenKube records one immutable **change** per plan and workload revision:

| Field | Source |
|---|---|
| `recommendation_ids` | Every recommendation applied by that revision (members detected in a later pass are appended). |
| `plan_id`, `pr_url`, `merge_sha`, `gitops_revision`, `synced_at` | The GitOps plan and its pull request, when the change came through a pull request. |
| `workload_uid`, `generation_before/after`, `spec_digest_before/after` | The live workload and the last observation of a different revision (or the plan's pre-apply baseline for the generation). `details.spec_before_source` says which. |
| `rollout_started_at` | The first observation of the new revision, else the sync time, else the completion time (`rollout_started_source`). |
| `rollout_completed_at` | Every replica updated and available (`rollout_completed_source`: the Deployment `Progressing` condition, otherwise the detection time). |
| `baseline_window_end` | The PR merge, otherwise the plan approval, otherwise the rollout completion. |

At the same time GreenKube freezes a **baseline snapshot**: the workload's hourly observations over the baseline window, the load-driver series available for that window, the unit prices observed in it, the dominant carbon-intensity source and the Impact Model version. The snapshot carries a `content_sha256` over its content; `GET /api/v2/changes/{id}` reports whether it still verifies. Measurements always use the frozen snapshot, never a re-read of the baseline period.

Evidence is never frozen from a failed read. When the metrics of the baseline window cannot be read (a database error or timeout), no snapshot is written and the change stays `applied`; the next lifecycle pass retries. When a Prometheus load-driver query fails (as opposed to returning no series), the snapshot waits for Prometheus for up to 24 hours after the change was recorded; after that it is frozen without that driver and lists it in `collector_versions.load_drivers_unavailable`.

## 2. Windows

| Window | Definition |
|---|---|
| Baseline | `[baseline_window_end − CHANGE_MEASUREMENT_PRE_WINDOW_DAYS, baseline_window_end)`, 7 days by default. It ends at the merge or approval, so it never contains the change. |
| Post | `[rollout_completed_at + CHANGE_MEASUREMENT_WARMUP_HOURS, + CHANGE_MEASUREMENT_POST_WINDOW_DAYS)`, 1 hour of warm-up and 7 days by default. |
| Interim health | `[post start, post start + VERIFICATION_WINDOW_HOURS)`, 72 hours by default. |

Seven days give every hour of the week one baseline observation. A shorter baseline leaves hours of the week unmatched, which lowers coverage (section 4).

## 3. Hourly observations

Rows of the workload (pod level, all containers) are reduced to one bucket per UTC clock hour:

- **Allocated cost** — only from rows whose cost provenance is trusted (`opencost`, `opencost_derived`, `kubecost`, `pricing_catalog`; or, for legacy rows, a positive cost not flagged estimated). A sample time counts only when every replica row at that time is trusted. The rate divides by the covered time, so raw 5-minute rows and hourly rollups mix correctly. A missing cost is missing, never zero: an OpenCost allocation whose `totalCost`, `cpuCost` or `ramCost` is NaN, infinite or absent (with no CPU and RAM cost to rebuild the total), or whose total is 0 for a pod that ran (`minutes > 0`) with non-zero requests, is stored with `cost_source = none` and the default placeholder cost, and a stored row with a zero total and non-zero requests is not trusted either. Such hours do not count; they never read as the whole cost saved. A zero that is only OpenCost's 5-decimal rounding (a small pod over a one-minute step) is not missing: its cost is rebuilt from its resource-hours at OpenCost's node unit prices and stored as `opencost_derived` (see [OpenCost precision](impact-model.md#opencost-precision-sub-precision-costs)).
- **CPU and RAM cost components** (`cost_cpu`, `cost_ram`) when every trusted row carries them.
- **Requested capacity** — requested CPU cores and memory GiB summed over replicas.
- **Replicas** — the mean number of pods covering the hour (pod-seconds over covered seconds).
- **CPU usage**, **energy**, **CO₂e** (only from rows with measured intensity and a profiled node), and the node **instance types** running the pods.
- **Embodied CO₂e** (`embodied_co2e_grams`, the hardware manufacturing emissions the Impact Model allocates by reservation). It does not depend on the grid intensity, so it needs no measured intensity. Snapshots frozen before it was measured have no value for it (`embodied_co2e_g` is absent from their hours and from their digest, which therefore still verifies).

An hour counts for a quantity when at least half of it is covered by qualifying data.

## 4. Counterfactual (C3)

### Load driver

The counterfactual is adjusted by a **load driver**, the hourly amount of work the workload was asked to do. GreenKube takes the first candidate whose coverage is at least `CHANGE_MEASUREMENT_MIN_COVERAGE` in both windows and whose baseline is not all zero:

1. requests per second from the service mesh or ingress: Istio `istio_requests_total{reporter="destination"}`, Linkerd `response_total{direction="inbound"}`, ingress-nginx `nginx_ingress_controller_requests` for a Service named after the workload, then Envoy `envoy_cluster_upstream_rq_total` for Contour-style clusters (`<namespace>_<service>_<port>`) of a Service named after the workload;
2. the PromQL expression in the workload annotation `greenkube.io/load-metric` (`$namespace` and `$workload` are substituted);
3. the workload's CPU usage from GreenKube's metrics. It is the weakest driver (a CPU change can move it) and is flagged `weak`.

Prometheus drivers are read as hourly means (`avg_over_time((<expr>)[1h:5m])` with a one-hour step). Without Prometheus only the CPU-usage driver is available.

### Load regression (C3 v2)

When a load driver exists, each quantity is first modelled by ordinary least squares on the baseline window (`greenkube.verify.regression`):

```
metric(t) = α[slot(t)] + β × load(t) + ε(t)
counterfactual(t) = α[slot(t)] + β × load(t)        for each post-window hour
```

`slot(t)` is the **hour of the week** when the baseline holds at least two observations of 90% of the 168 hours of the week (a 14-day baseline or more); otherwise the **hour of the day split into weekdays and weekends** (48 slots). With the default 7-day baseline every hour of the week is seen once, so hour-of-week dummies would absorb every observation and leave `β` unidentifiable. Because the slots partition the hours, the fit has a closed form: `β` is the slope of the within-slot demeaned metric on the within-slot demeaned load, and `α[s]` the slot mean minus `β` times the slot's mean load.

The fit is used only when its **diagnostics** pass, otherwise the quantity falls back to the hour-of-week model below (`methods[quantity] = hour_of_week_mean`, `diagnostics[quantity].fallback`):

| Diagnostic | Failure |
|---|---|
| Paired baseline hours (value and load) | fewer than 48 |
| Residual degrees of freedom (hours − slots − 1) | fewer than 24 |
| Within-slot load variation | none: `β` is not identifiable |
| Sign of `β` | negative with t < −2: the load does not explain the metric |
| Extrapolation | more than 20% of the post-window hours have a load outside the baseline range widened by 25% |

The measurement records, per quantity, the method (`ols_load_seasonal` or `hour_of_week_mean`) and the diagnostics: seasonality, observations, slots, degrees of freedom, R² and adjusted R², residual standard deviation, `β` with its standard error and t-statistic, the Durbin-Watson statistic of the residuals, the extrapolated share and the failures. `counterfactual_params.model` is the method of `allocated_cost`.

### Hour-of-week model (C3 MVP, fallback)

For each post-window hour `t` with hour of the week `h`:

```
counterfactual(t) = mean_baseline[h] × (load(t) / load_baseline[h]) ^ β
saving(t)         = counterfactual(t) − actual(t)
```

`β` is the **load elasticity** of the quantity, fitted on the baseline window as the least-squares slope of `log(quantity)` on `log(load)`, clamped to `[0, 1]`. It is 0 when fewer than 24 baseline hours pair a positive value with a positive load, or when the load hardly varies.

- An autoscaled workload, whose replicas follow its traffic, gets `β ≈ 1`: twice the traffic, twice the expected cost.
- A fixed-capacity workload, whose requests do not move with traffic, gets `β ≈ 0` and its counterfactual is not rescaled. Scaling it by the raw load ratio would credit any traffic growth as a saving (the defect behind BUG-27).

A post-window hour is **aligned** when it has a value and its hour of the week has a baseline mean (and, when `β > 0`, a load value). **Coverage** is the number of aligned hours divided by the number of hours in the post-window.

The fitted elasticities and the number of matched hours of the week are stored in the measurement as `counterfactual_params`. Without a load driver this model is used with `β = 0` (no diagnostics are recorded).

## 5. Quantities

The `request_rightsizing` strategy (version 2.2) measures, per hour:

**Per replica.** A request change moves what each replica is charged, not how many replicas run. Every quantity below is fitted *per replica* (each hour's value over its replica count) and the per-replica saving is multiplied by the mean replica count of the aligned post-window hours: `saving = Δ(per-replica rate) × post replica-hours`, which is `Σ Δrequest × replica-hours` for requested capacity. A replica count that drops for another reason (an HPA scaling 4 → 2 during the post-window) is therefore never credited to the change: it saves nothing per replica. It is reported by the `replica_shift` confounder (moderate). Hours without a replica count are left out. The interval and counterfactual are scaled by the same replica count, so the outcome rule decides exactly as it would per replica. Measurements decided by version 2.0 compared workload totals (stated by `strategy_version`).

**Both bases, the smaller claim (2.2).** Per-replica scaling alone fails in the mirror direction: a CPU-request cut that makes an HPA scale out (2 pods at $0.01/h → 4 pods at $0.005/h) halves the per-pod cost while the workload total stays $0.02/h, and per-replica scaling would credit $0.02/h. Every quantity is therefore fitted twice, per replica and on workload totals, and the estimate with the **smaller mean saving** is kept; it stays `realized` only when both bases are realized, otherwise it is `inconclusive` with the reason. A replica drop (the per-replica basis is smaller) and a replica rise the change caused (the workload total is smaller) are both left out, so neither direction is credited. The basis kept for each quantity is recorded in `counterfactual_params.replica_basis` (`per_replica` or `workload_total`). Measurements decided by version 2.1 used the per-replica basis alone.

| Quantity | Unit | Claimed | Role |
|---|---|---|---|
| `allocated_cost` | currency | yes | Decides the outcome. |
| `cost_cpu`, `cost_ram` | currency | no | Split the saving between the change's recommendations. |
| `core_hours`, `gib_hours` | core-hours, GiB-hours | no | Requested capacity freed. |
| `co2e_g` | gCO₂e | yes | CO₂e as the Impact Model allocates it: operational (idle and memory power follow the reservation) plus embodied (allocated by reservation) when both windows measured it. Claimed when the change is realized and its own interval shows a saving; compared with the projection of the same components only ([component matching](impact-model.md#co2e-components-and-component-matched-comparison)), listed in the estimate's `components` and in `counterfactual_params.co2e_components`. Split between the change's recommendations by their projections of those components. |
| `embodied_co2e_g` | gCO₂e | no | The embodied part alone (reported). |

Stored rows are allocated by the Impact Model: idle CPU power follows the CPU reservation, memory power the memory reservation and embodied emissions the CPU reservation, so a request change moves the measured CO₂e. A baseline snapshot frozen before embodied emissions were measured carries none; its measurement compares operational CO₂e with the operational projection, as before (`components = [operational_energy, idle_share]`). A window whose embodied CO₂e is zero everywhere (no hardware profile for its nodes) is treated the same way. Before Impact Model 2.0.0, `co2e_g` was reported only and recommendations stored a measured CO₂e of 0.

### Resource deletions (`resource_deletion` 1.1, H6)

Deleting an orphaned LoadBalancer Service or PersistentVolume releases the asset's whole allocated rate once apply detection has verified that the resource is absent. The rate is the mean trusted cost of the resource in the baseline window when GreenKube observed it (`pre_window_mean`, rate source `observed`), otherwise the rate frozen in the recommendation's projection (`allocated_rate_at_decision`), **only when that rate has a trusted provenance**: the OpenCost or Kubecost per-asset cost or a pricing-catalog asset price (`impact.assumptions.cost_source`). A configured default (`LOAD_BALANCER_COST_PER_MONTH`, `STORAGE_COST_PER_GIB_MONTH`, recorded as `custom_rate`), no cost (`none`), recommendations that disagree (`mixed`) or an unknown provenance are never booked as measured: the deletion is `inconclusive` with the reason, and its saving stays a projection. The rate source is recorded on the estimate (`rate_source`), in `counterfactual_params.rate_source` and in the measurement's `strategy_version` (`1.1:<method>:<rate source>`, e.g. `1.1:allocated_rate_at_decision:opencost`), which the savings ledger v2 carries as its entries' `method_version` (`resource_deletion/1.1:…`). A resource still observed after the change is `not_realized`, a deletion whose absence was never verified is `inconclusive`.

## 6. Uncertainty and outcome (C4)

### Interval

The hourly savings of the aligned post-window hours, in time order, are resampled with a **moving-block bootstrap**: blocks of 24 consecutive hours (one day, starting at any hour) are drawn with replacement until the series length is reached, and the mean is taken. `CHANGE_MEASUREMENT_BOOTSTRAP_SAMPLES` resamples (1000 by default) give the distribution of the mean; its 5th and 95th percentiles are the **90% confidence interval**. Day blocks keep the within-day correlation of the savings, which resampling single hours would ignore.

With the load regression, the interval also carries the uncertainty of the fit: each resample adds a **residual bootstrap** of the baseline. The baseline residuals, inflated by `√(n / dof)`, are resampled in the same one-day blocks; refitting on the fitted values plus those residuals moves the counterfactual of every aligned post-window hour, and the mean saving with it. The move is linear in the residuals (slot means move by their slot's mean residual, `β` by `Σ dx·e / Sxx`), so it is computed with one weight per baseline hour instead of a refit.

The generator is seeded from the change, the measurement sequence and the quantity, so the same evidence always produces the same interval.

### Outcome rule

The rule is applied once, at the end of the post-window (plus one hour of grace for late samples). There is no repeated testing. A decision is only written from complete reads: when the post-window metrics or the health gates cannot be read, nothing is recorded and the next pass retries; when a Prometheus load driver fails, the decision waits up to 24 hours for it, then is made without it and lists it in `counterfactual_params.load_drivers_unavailable` (`pre` and `post`).

| Condition (on `allocated_cost`) | Outcome |
|---|---|
| A severe confounder was found | `contaminated` |
| Coverage below `CHANGE_MEASUREMENT_MIN_COVERAGE` (0.8), or no trusted cost in a window | `inconclusive` |
| CI lower bound > 0 | `realized` |
| CI upper bound ≤ ε, with ε = `CHANGE_MEASUREMENT_EPSILON_RATIO` × mean counterfactual (2%) | `not_realized` |
| Otherwise | `inconclusive` |

Values are **signed and never floored**: a cost increase is reported as a negative saving with its interval.

**Projection accuracy** (measured annual saving ÷ projected annual saving) is reported for each claimed quantity. A measurement reads allocated cost and CO₂e, so it is compared with the **allocated** projection (`impact.allocated_cost_annual` and the allocated CO₂e of the measured components, recorded as `counterfactual_params.projection_basis = allocated`), never with the realization-scaled billable projection (`potential_savings_cost`, `co2e_expected_annual_g`): projected ≠ allocated ≠ realized. The measurement also records `counterfactual_params.realization_factor`, the allocated-weighted realization factor of the change's projections, from which the API derives the billed saving to expect (an expectation, not a measurement). It is not a gate: a saving can be realized at 30% of its projection. It feeds the track record of the recommendation's source.

### Confounders

| Confounder | Severity |
|---|---|
| Another change on the same workload landed after the merge and before the end of the post-window | severe |
| The pod template changed again during the post-window (a spec digest other than this change's) | severe |
| The workload was deleted and recreated during the post-window | severe |
| The workload's HPA changed after the rollout (replicas, so the allocated cost, moved without the change) | severe |
| The unit price of a resource whose per-replica requests the change did not touch moved more than 20% | severe |
| The node pool's CPU or memory unit price (all workloads of the pool) moved more than 20% (`pool_price_card_change`) | severe |
| The node instance types running the workload changed on more than 50% of its pod-hours | severe |
| The node pool's hardware changed: its CPU-capacity-weighted instance-type mix moved by more than 25% (`pool_instance_type_change`) | severe |
| The node pool's mean CPU capacity changed by more than 50% (`pool_capacity_change`) | severe |
| Another change on the same workload landed inside the baseline window | moderate |
| The workload's HPA changed inside the baseline window | moderate |
| Mean replicas deviate more than 25% from the replicas predicted by the same counterfactual | moderate |
| The unit price of a resource whose per-replica requests the change did not touch moved more than 5% | moderate |
| The node pool's CPU or memory unit price moved more than 5% (`pool_price_card_change`) | moderate |
| The node instance types running the workload changed (total-variation distance above 25% of pod-hours) | moderate |
| The node pool's mean CPU capacity changed by more than 25% (the cluster autoscaler, `pool_capacity_change`) | moderate |
| A change to another workload whose pods ran in the same node pool landed during the measurement (`pool_change`) | moderate |
| Nodes were added to or removed from the pool during the post-window without a capacity shift (`autoscaler_activity`) | mild |
| More than 50 changes landed during the measurement, so later ones were not checked for pool membership (`pool_check_truncated`) | mild |

A severe confounder's detail is the reason of the `contaminated` outcome.

**Node-pool checks.** The workload's pools are the `node_pool` of the nodes its pods ran on during the post-window. The node snapshots (SCD2) are sampled every 6 hours over both windows; samples without any node of the pools (before the snapshots began) are ignored. A change to another workload counts when it completed between the merge and the end of the post-window and its pods ran on one of the pools' nodes in the two hours after its rollout. Allocated cost is requests × unit price, so other workloads' changes and autoscaling only move the idle share of CO₂e allocated to this workload (moderate), while new hardware in the pool changes its unit prices and power profile (severe). Nodes without a pool label are not grouped, and the checks are skipped. A failed read of the node snapshots or of the changes delays the decision to the next pass.

**Pool price card.** At each pool sample the metrics of every pod are read for one hour, and the CPU (per core-hour) and memory (per GiB-hour) unit prices of the pods that ran on the pools' nodes are computed from trusted cost with CPU/RAM components, for each window. The measured workload's own pods are left out: an allocator that charges `max(request, usage)` moves the cost-per-request ratio of a workload resized below its usage although no price changed. A unit price belongs to the nodes, not to the requests, so this control works even when the change resized both resources of the workload (the workload-level check then has no untouched resource). A pool-wide finding replaces the workload-level finding of the same resource, so a price change is never penalized twice.

**Confidence** starts at the coverage and loses 0.15 per moderate confounder, 0.05 per mild one and 0.10 when the counterfactual has no load driver or only the CPU-usage driver; it is 0 when the measurement is contaminated.

## 7. Health

The same per-replica health gates as the interim verifier apply to the change's recommendations, grouped by container: per-pod p95 usage under the applied request × headroom, restarts, OOM kills, readiness and CPU throttling. A signal that cannot be read is reported `not_available`, never counted as a pass.

They are evaluated twice:

- once the interim health window has elapsed, so a harmful change is flagged within `VERIFICATION_WINDOW_HOURS` instead of at the end of the post-window. A failure sends every awaiting recommendation of the change to `rollback_review`. When the metrics cannot be read, the check is not recorded and is retried on the next pass;
- at the decision, over the whole post-window. The gates are stored in the measurement.

A health failure does not change the measurement outcome: the saving is still evidence about the change. It changes what happens to the recommendations.

### Reliability increases (H1)

A request increase is a reliability fix: its cost is measured like any request change (the estimates are negative savings, reported as a cost), and its success is whether the **OOM-kill rate dropped**.

- **Baseline:** the OOM kills the recommendation observed over its evidence window (`evidence.oom_events`) of the memory-request increases, summed over the containers whose memory request was raised, divided by the time the **workload** was observed before the change, not by the whole evidence window. The evidence window starts at the cluster's data start: a workload created 40 minutes before the scan, on a cluster with 8.4 hours of data, was never exposed for the hours before it, and dividing its kills by 8.4 hours would make its baseline rate about 12 times too low (leaning the verdict towards `inconclusive`).
  - **Exposure:** from `evidence.first_observed_at` (the target's first sample inside the evidence window) to `evidence.observation_window_end`, bounded by `evidence.observation_window_seconds`. The scan records `first_observed_at` from the target's usage summary: the first sample lies in its first observed hour, and the observed collection steps (`step_count` × the query step, counted back from the window end) place it within that hour, never outside it, so collection gaps or compressed rows that overcount steps shift it by less than an hour. Without a usage summary, the first raw sample of the target's series is used. It is never earlier than the window start.
  - **Fallback:** evidence stored before `first_observed_at` was recorded, or without a usable value, keeps the whole evidence window (the previous behaviour).
  - The source of the exposure is recorded as `baseline_exposure_source` in `counterfactual_params.reliability`: `workload_first_observed` or `evidence_window` (fallback). With several raised containers, the longest exposure (and its source) is used.
- **After:** the OOM kills over the post-change window (interim window, then the whole post-window), counted in the unit of the baseline: container restarts whose termination, at the 5-minute step of the restart, was `OOMKilled`, summed over pods (`WorkloadHealthObservation.oom_restart_count`). The `oom_kills` gate itself counts pods with a kill, so a pod crash-looping on OOM would look like one kill: it is never used for the rate. When the OOM restart count cannot be read, the outcome is `inconclusive` and the strict gates stay.
- **Test:** if the rate had not changed, the post-window share of all kills would follow a binomial law with the post-window share of the observed time. The drop is `realized` when the post-window count is improbably low (one-sided p ≤ 0.10), `not_realized` when the post-window rate is not lower, and `inconclusive` with too few kills or unknown counts. An increase made without OOM history (p99 close to the request) is `inconclusive` on this test and judged by its usage gates.
- **Gate:** for a container whose **memory** request was raised with OOM history, that container's `oom_kills` gate compares the rates instead of failing on any kill: a kill after an OOM-driven increase does not send it to `rollback_review` (rolling it back would bring the kills back) unless the rate did not drop. Every OOM kill also restarts its container, so those OOM restarts are left out of the `restarts` gate (restart counters are read per pod, so they leave the `restarts` gate of every container of the change); any other restart still counts against `verification.maxRestartDelta`. The rate gate is applied per container: every other container of the same change (a decrease, a CPU-only increase, which cannot cure OOM kills, or a container with a memory decrease) keeps the strict gate, so one container's increase never hides another container's OOM kills.
- The outcome is stored in `counterfactual_params.reliability` (metric, outcome, reason, counts, hours, rates per day, p-value, baseline exposure source) and added to the measurement's reasons. The savings outcome of the change is unchanged.

## 8. One measurement per change (C5)

A change is measured once, on its workload. The saving is split between its recommendations:

- **by cost component** when the workload's rows carry the CPU/RAM split: CPU recommendations share the `cost_cpu` saving and memory recommendations the `cost_ram` saving, each in proportion to the request reduction it made. A recommendation claims its component share only when that component's own interval shows a saving: in a `realized` change, a recommendation whose component was `not_realized` (for example memory requests that grew) or `inconclusive` gets that outcome instead of `verified`;
- otherwise **by projected share** of the `allocated_cost` saving: each recommendation's projected *allocated* annual saving (`impact.allocated_cost_annual`, `potential_savings_cost` for projections stored before the Impact Model) over the group's total, a projected increase weighing nothing (equal shares when nothing was projected).

**Component claims never exceed the net saving.** A bundle can cut CPU (`cost_cpu` −$100/yr) while it raises memory (`cost_ram` +$80/yr), or a component the change did not touch can grow. The component shares that save are scaled by `net ÷ (sum of the saving components)`, with `net` the change's measured `allocated_cost` saving, whenever the components add up to more than the net (and to zero when the change saved nothing net). The bundle above books the net $20/yr on the CPU recommendation, never the gross $100/yr; the memory recommendation keeps its signed, unclaimed estimate (`not_realized`, no accrual). Components that add up to at most the net are not scaled.

Shares within a basis sum to one, so a change is never claimed twice. Each recommendation receives one immutable row in `savings_measurements` (`change_id`, `group_id` = the change measurement), with its share of the estimates and the change's outcome. Its status follows:

| Change outcome | Recommendation |
|---|---|
| `realized` | `verified`; the ledger accrues its measured share (method `measured`). |
| `not_realized` | `applied` with `verification_status=failed`; signed measured values are kept. |
| `inconclusive`, `contaminated` | `applied` with `verification_status=inconclusive` (reasons prefixed `contaminated:` for a contaminated change). |
| a health gate failed | `rollback_review`. |

A recommendation that joins a change after it was measured gets an `inconclusive` row: it was not part of the split. A realized change is measured once, so a re-measurement requested by one of its recommendations does not start a new measurement: the recommendation gets a new row with the existing decision and the reason `re-measurement not started: change … is realized and is measured once`.

## 9. Lifecycle (C2)

```
applied → measuring → realized | not_realized | inconclusive | contaminated
realized → regressed | closed
not_realized | inconclusive | contaminated → measuring   (explicit re-measurement only)
```

The repository rejects any other transition. The two self-transitions are `measuring → measuring` (used for `health_checked`) and `closed → closed`, which ends the measured accrual of a superseded change (`superseded_ended`, or `workload_deleted`; see [section 10](#10-drift-monitor-c8)). Each transition is a compare-and-swap on the stored state and version, written in the same transaction as its event (`change_recorded`, `measurement_started`, `health_checked`, `measurement_decided`, `remeasure_started`, `superseded`, `regressed`, `spec_drift`, `workload_recreated`). `savings_reversed` is recorded without a state change. A re-measurement (`POST /api/v1/recommendations/{id}/remeasure` on any of the change's recommendations) starts a new post-window at the next hour and a new measurement that supersedes the previous one; the baseline stays the frozen snapshot. A newer change on the same workload closes a realized change (`superseded`, with `details.superseded_by`); the superseded change keeps accruing its own measured step and stays watched by the drift monitor.

A decision persisted before a crash is replayed: a change measurement and each recommendation's row are unique per measurement cycle, so a second pass reuses them instead of measuring again.

## 10. Drift monitor (C8)

The `savings-drift` controller loop (hourly, leader only, `greenkube.verify.drift`) watches every `realized` change.

**Drift.** The saving of a request rightsizing is the allocated cost freed per replica, so the monitor compares the workload's daily allocated cost **per replica-hour** with the baseline snapshot (`B`) and with the measured post-window (`A`):

```
lost_share(day) = (rate(day) − A) / (B − A)
```

Per-replica rates do not move with load-driven replica counts; they move when the requests creep back (a VPA in auto mode, an admission webhook, a LimitRange) or prices change. Only complete UTC days after the post-window with at least 18 hours of trusted cost count, over the last 7 days. When `lost_share ≥ CHANGE_DRIFT_THRESHOLD` (0.5) on each of the last `CHANGE_DRIFT_SUSTAINED_DAYS` (3) consecutive days, the change becomes `regressed` (event `regressed`, actor `drift-monitor`, payload: `B`, `A`, threshold, onset and the daily lost shares) and its `verified` recommendations become `regressed`, which stops their accrual and counts against their source's track record. A post-window without a per-replica saving is not monitored.

**Spec drift.** The usage drift is assessed first, so a revert in git whose usage has already drifted for the sustained period is a regression. Otherwise, when the latest spec observation after the rollout has a pod-template digest other than the change's `spec_digest_after`, or another workload UID, the change is closed (`realized → closed`, event `spec_drift` or `workload_recreated`): its measured saving no longer describes what runs. Its `verified` recommendations become `regressed` as well, so their measured accrual stops from the first observation of the differing run (`accrual_end` in the event payload). The monitor acts two hours after the observation, and a newer recorded GreenKube change on the workload closes the change as `superseded` instead; each change then keeps accruing its own measured step.

**Deletion.** The monitor asks the Kubernetes API whether a realized change's Deployment, StatefulSet or DaemonSet still exists. HTTP 404 records a `workload_absent` event; when the workload is still absent two hours later the change is closed (`realized → closed`, event `workload_deleted`, `accrual_end` = the first absent observation) and its `verified` recommendations become `regressed`, so their measured accrual stops there. A workload found again in between records `workload_present` and nothing closes; any other API answer (no client, forbidden, timeout) decides nothing.

**Superseded changes.** A change closed as `superseded` keeps accruing its own measured step: the newer change is measured against a baseline frozen after the earlier change (its 7 days before the merge), so the two steps add up and nothing is counted twice. A change whose newer change lands inside its baseline window has a `change_in_baseline` confounder. The superseded change is never left unwatched; on each pass the monitor:

1. ends its accrual when any newer change on the workload has ended (`regressed`, or closed by a spec drift, a revert, a recreation or a deletion): event `superseded_ended` (`closed → closed`), `accrual_end` = the earliest `accrual_end` of those changes. This fails closed: when the newest step is lost, the monitor cannot tell whether the earlier step still holds, so neither is credited any more until a new change is measured;
2. otherwise, when the newest change is `realized`, does nothing more: that change's own watch covers the workload and ends it through rule 1;
3. otherwise (the newest change is still measured, or was not realized, inconclusive or contaminated, so nobody else watches the workload) runs the **deletion** check on the superseded change itself (event `workload_deleted`, `closed → closed`) and the **spec drift** check against the newest change's template (`spec_digest_after`) and workload UID (event `superseded_ended`).

In every case the change's `verified` recommendations become `regressed` and what accrued after `accrual_end` is reversed like any other ending (below). Changes closed as superseded before this rule existed are recognized by their closing `superseded` event.

**Reversal entries.** The ending paths store where the measured saving ended (`details.accrual_end` on the change: the drift onset, which is the first day of the sustained run, or the first differing spec observation). The measured accruals written after it, or after the verification time if that is later, are reversed: one signed ledger row per recommendation, method `measured`, covering `[accrual end, end of the last accrued slot)` at the recommendation's measured annual rate. It is written two hours after the state change, once an accrual run in flight has finished, and the change records a `savings_reversed` event listing the rows. A later pass reverses only what no earlier `savings_reversed` entry covers, so an accrual written late gets an adjacent entry, never an overlapping one. A reversal within one accrual slot widens its period label by one second so it cannot take that slot's idempotency key; its amount uses the exact period. Ended changes are checked for 14 days after their state change. In the savings ledger v2 each reversal row becomes a `reversal` entry with reason `drift`, linked to the first measured accrual it cancels ([section 12](#12-savings-ledger-v2-c6)). The ledger counters report reversals as reversed (`greenkube_cost_savings_reversed_dollars_total`, `greenkube_co2e_savings_reversed_grams_total`); savings accrued before the accrual end are kept.

## 11. Auditing a measurement

1. `GET /api/v2/changes/{id}` returns the change, its state history and its baseline snapshot metadata. `baseline.content_verified` must be `true`.
2. `GET /api/v2/changes/{id}/measurements` returns each decided measurement with its windows, load driver, `counterfactual_params`, per-quantity estimates and intervals, confounders, confidence, health gates, `allocations` and the per-recommendation rows.
3. `evidence_sha256` covers every decided field of the measurement and `baseline_sha256` the snapshot it used. Recomputing the counterfactual from the snapshot's `hours` and `load_series`, the post-window rows and the parameters above must give the same estimates; the bootstrap seed is derived from `change_id`, `sequence` and the quantity.
4. Database triggers reject `UPDATE` and `DELETE` on `change_events`, `change_members`, `baseline_snapshots`, `change_measurements`, `change_state_events`, `savings_measurements` and `savings_ledger_entries`.
5. `GET /api/v2/ledger/entries?change_id={id}` lists the ledger entries of the change; `GET /api/v2/ledger/summary` reports the last hash-chain verification.

## 12. Savings ledger v2 (C6)

Every saving GreenKube reports ends up as an **entry** of an append-only, hash-chained ledger (`savings_ledger_entries`, `greenkube.verify.ledger`). An entry is one signed amount of one quantity earned during one period under one method:

| Column | Meaning |
|---|---|
| `entry_kind` | `accrual` (an amount earned), `reversal` (cancels `reverses_entry_id`), `restatement` (replaces the projected accrual `restates_entry_id` with the measured amount of the same period). |
| `book` | `accrued` mirrors the v1 accrual ledger and reconciles with `GET /api/v1/recommendations/savings`; `restatement` holds the restatements and the reversals of the projections they replace. |
| `reason` | `accrual`, `drift` (a drift-monitor reversal, [section 10](#10-drift-monitor-c8)), `supersession` (rollback review, revert or phantom repair), `restatement`. |
| `method` | `projected`, `measured_allocated` (this methodology, allocated cost), `realized_billed` (pool-level realization, C10, not written yet), `user_declared`. Never summed together. |
| `quantity`, `value`, `unit` | `cost` in the currency its recommendation was priced in (the projection's `impact.currency`; `IMPACT_PRICING_CURRENCY` when the recommendation has none), so a later currency change never relabels past accruals, or `co2e_g` in gCO₂e. Values are signed and never floored. The summary keeps one total per method, quantity and unit: amounts in different currencies are never added. |
| `period_start`, `period_end`, `slot_start` | The accrued period, on the fixed UTC hour grid of the accrual job. |
| `recommendation_ids`, `change_id`, `measurement_id`, `pr_url`, `method_version` | Provenance: the change's recommendations, the change and its pull request, the measurement and the strategy version (`request_rightsizing/2.0`) of measured amounts. |
| `cluster_uid`, `cluster_name` | The UID of the `kube-system` namespace (roadmap N9), stored next to `CLUSTER_NAME` in `cluster_identities` the first time it is read and never replaced; `name:<CLUSTER_NAME>` without Kubernetes access. Signal ids and recommendation fingerprints keep their own cluster key. |
| `seq`, `prev_hash`, `hash` | The entry's position in its cluster's chain and the chain link. |

**Append-only.** Database triggers reject `UPDATE` and `DELETE` (and `TRUNCATE` on PostgreSQL). A correction is always a new entry.

**Hash chain.** Each cluster's entries are numbered without gaps (`seq`) and chained: `hash = sha256(prev_hash || canonical_json(entry))`, where the canonical JSON covers every column except `hash` and `prev_hash` (sorted keys, timestamps as `YYYY-MM-DDTHH:MM:SS.ffffffZ`, floats as their shortest exact representation). Appends are serialized per cluster (an immediate transaction on SQLite, a transaction-scoped advisory lock on PostgreSQL), and every entry has a deterministic `entry_key`, so retries and concurrent writers never fork the chain or duplicate an entry.

**Verification.** The controller's `ledger-chain` loop (leader only, every `LEDGER_CHAIN_VERIFY_INTERVAL`, default 1 hour, and at start) walks each chain: positions must follow each other, every `prev_hash` must be the previous `hash`, every `hash` must match its recomputed value and every `entry_id` its key. It verifies incrementally from the last verified head and the whole chain once a day; a chain shorter than the head it verified before, or with another hash there, lost its tail. Failures increment `greenkube_ledger_chain_verification_failures_total{reason}` (`missing_entry`, `broken_link`, `hash_mismatch`, `identity_mismatch`, `truncated`, `head_rewritten`), set `greenkube_ledger_chain_ok{cluster_uid}` to 0 (the `GreenKubeLedgerChainVerificationFailed` alert of the chart's PrometheusRule fires on it, so a failure found by a pod's first pass alerts too, for as long as the chain stays broken), and are reported by `GET /api/v2/ledger/summary` (`chain`).

A failure is sticky. `ledger_chain_status` keeps the last head that passed (`verified_head_seq`) as the anchor of every later verification, so a lost tail keeps failing, also once new entries reuse its positions (`head_rewritten`). After the incident is investigated and the chain is restored or accepted, an operator resets the anchor by deleting the cluster's `ledger_chain_status` row (that table is not append-only); the next pass verifies the whole chain again.

**Dual write.** The savings attribution job keeps writing the v1 ledger (`recommendation_savings_ledger`), which the legacy views, the gauges and `GET /recommendations/savings` read. Each attribution run that reached the database is followed by a ledger pass, and the `ledger-chain` loop catches up whatever a failed pass left (`greenkube_ledger_write_errors_total`). A pass:

1. projects every v1 row after its watermark (`ledger_projection_watermarks`, advanced in the same transaction) into one entry per non-zero quantity. v1 `measured` becomes `measured_allocated`. A v1 drift reversal (an off-grid negative `measured` row) becomes a `reversal` with reason `drift`. Legacy compressed hourly rows are projected too. Rows are selected by an anti-join (v1 rows with a non-zero quantity and no entry yet), never by id alone: the attribution job and the drift monitor write in separate transactions, so a v1 id can commit long after higher ids were projected. A pass reads from 1,000 ids below the watermark; a full sweep from the first id runs at least once an hour per writer and on every `ledger-chain` tick, so a late row is projected at the latest on the next tick (`greenkube_ledger_late_rows_projected_total`). What a full sweep still finds unprojected (an invalid row, a backlog above the per-pass cap) is reported by `greenkube_ledger_unprojected_rows{source_table}` and alerted after 3 hours by `GreenKubeLedgerProjectionBacklog`;
2. reverses (reason `supersession`) every entry whose v1 row is flagged `superseded`, and the restatement entries attached to it;
3. restates (BUG-39): for each `verified` (or later `regressed`) recommendation, every projected accrual that ended at or before `verified_at` and is not reversed yet gets a `reversal` and a `restatement` at the measured annual rate over the same period (`measured_cost_saved`, `measured_co2e_saved_grams`), linked to the projection, the measurement and the change.

**Restatement limits.** A projection is restated once, at the measured annual rate of the recommendation when the pass runs (the `measurement_id` of the restatement names that measurement). A later re-measurement does not restate it again, a quantity whose projection was zero has no projected entry to restate, and the projections of `not_realized` and `inconclusive` changes stay projected in both books: the measurement decided that no saving can be claimed, not a measured amount to replace them with.

**Reading the books.** The `accrued` total of a method equals the v1 ledger of that method (all time, or prorated over a window exactly as the savings report does). The `restated` total (both books) is the best current statement: after a measurement, the projection of the measured period has moved to `measured_allocated`. A recommendation superseded after a restatement ends at zero in both books.

**Rollups** are read-side: the `savings_ledger_daily_rollup` view sums entries per UTC day, namespace, method, quantity, book and kind. Nothing is ever compressed or deleted.

**Export.** `GET /api/v2/ledger/export` streams the entries as CSV (one row per entry, with `window_share` and `window_value` for a window) or, with `format=focus`, as FOCUS-aligned rows (one per accrual group, cost and CO₂e together): `ChargeCategory` `Credit` for accruals and `Adjustment` (`ChargeClass` `Correction`) for reversals and restatements, `EffectiveCost` = −saving (avoided spend), `BilledCost` 0 except for `realized_billed`, and the `x-greenkube-*` extension columns (`method`, `measurement-id`, `pr-url`, `co2e-g`, `saving`, `book`, `hash`, …). The sum of `x-greenkube-saving` over `accrued` rows of a method reconciles with the savings report. A window needs both bounds (`start` and `end`, or `last`), as in the savings report. String cells are guarded against spreadsheet formula injection like the v1 CSV report (a leading `=`, `+`, `-` or `@` is prefixed with `'`; numeric columns are written as numbers), and `pr-url` is the web URL users open. The extension column names follow the roadmap (`x-greenkube-*`); FOCUS 1.0 prescribes an `x_` prefix for custom columns, so a FOCUS validator flags them until the names are aligned.

## Limitations

- **Request changes and asset deletions only.** `request_rightsizing` and `resource_deletion` are the only strategies. Replica, schedule and node-pool changes need their own strategies.
- **Allocated, not billed.** The measured saving is allocated cost. The bill only drops when nodes are removed; pool-level realization (C10) is not implemented.
- **Pool confounders need pool labels.** Node-pool checks, the pool price card included, use the `node_pool` of the node snapshots; clusters whose nodes carry no pool label get no pool-level check, and price changes are then seen per workload only (the unit price of an untouched resource).
- **Drift is per replica.** The drift monitor watches allocated cost per replica-hour, the quantity a request change moves; a regression that only shows in replica counts (an HPA retuned) is not a drift of this change, and CO₂e is not monitored on its own.
- **Reversals are whole.** A regression reverses the full measured rate from the onset, even when only part of the saving was lost (for example 60%). This is conservative: the remaining part is not credited until a new change is measured.
- **Deletion and supersession.** A deletion ends the measured accrual at the first absent observation, up to one monitor interval (1 hour) after the actual deletion; the accruals of that hour are kept. A change superseded by a newer recorded change is closed, but its recommendations keep accruing their measured step next to the new change's until a newer change ends or the workload is deleted or drifts; the earlier step is then ended too, even when only the newer step was lost (conservative).
- **A superseded change under a newer change that is not realized.** While the newest change on the workload is not realized (`not_realized`, `inconclusive`, closed), a superseded change is watched for deletion and for a pod-template drift away from the newest change, not for a usage drift (its own usage-drift assessment would misfire against the newer template); a slow erosion of its saving is then only seen once a newer change is measured. A newer change that partly undoes the superseded one (a CPU cut followed by a reliability increase) and is not realized does not end the superseded change's accrual either, and its cost step is not booked: the books can then overstate the workload's saving by that increase until the newer change is measured as `realized` or `regressed`.
- **Accelerated demo windows.** `CHANGE_MEASUREMENT_ACCELERATED` (quickstart only) accepts a baseline of 1 hour or more. Such a baseline holds no other hour of the week than its own, so the post-window rarely matches it and the outcome is usually `inconclusive`. It shows the pipeline end to end (`/changes`, `/ledger`), not a saving.
- **One baseline week.** With the default windows each hour of the week has a single baseline value; the load regression then uses weekday/weekend hours of the day instead. The interval reflects the noise, but an unusual baseline week (a holiday, an incident) biases the counterfactual. Use a longer `CHANGE_MEASUREMENT_PRE_WINDOW_DAYS` (14 days enables hour-of-week slots) for such workloads.
- **Load-driver heuristics.** The ingress-nginx and Envoy drivers assume a Service named after the workload, and the Envoy driver assumes Contour's cluster naming. Set `greenkube.io/load-metric` when that does not hold.
- **Spec observations depend on the lifecycle interval.** A spec change reverted between two detection passes is not seen.
