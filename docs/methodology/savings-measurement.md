# Savings measurement — methodology

**Strategy:** `request_rightsizing` version `2.0` (`greenkube.verify.strategy.RequestRightsizingStrategy`) · **Code:** `src/greenkube/verify/` · **Roadmap:** C1–C5, C8, C9

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
| Append-only ledger entries, hash chain and restatement (C6) | Not yet. Measured savings reach the existing ledger through the recommendation (method `measured`). |
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

- **Allocated cost** — only from rows whose cost provenance is trusted (`opencost`, `kubecost`, `pricing_catalog`; or, for legacy rows, a positive cost not flagged estimated). A sample time counts only when every replica row at that time is trusted. The rate divides by the covered time, so raw 5-minute rows and hourly rollups mix correctly.
- **CPU and RAM cost components** (`cost_cpu`, `cost_ram`) when every trusted row carries them.
- **Requested capacity** — requested CPU cores and memory GiB summed over replicas.
- **Replicas**, **CPU usage**, **energy**, **CO₂e** (only from rows with measured intensity and a profiled node), and the node **instance types** running the pods.
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

The `request_rightsizing` strategy measures, per hour:

| Quantity | Unit | Claimed | Role |
|---|---|---|---|
| `allocated_cost` | currency | yes | Decides the outcome. |
| `cost_cpu`, `cost_ram` | currency | no | Split the saving between the change's recommendations. |
| `core_hours`, `gib_hours` | core-hours, GiB-hours | no | Requested capacity freed. |
| `co2e_g` | gCO₂e | yes | CO₂e as the Impact Model allocates it: operational (idle and memory power follow the reservation) plus embodied (allocated by reservation) when both windows measured it. Claimed when the change is realized and its own interval shows a saving; compared with the projection of the same components only ([component matching](impact-model.md#co2e-components-and-component-matched-comparison)), listed in the estimate's `components` and in `counterfactual_params.co2e_components`. Split between the change's recommendations by their projections of those components. |
| `embodied_co2e_g` | gCO₂e | no | The embodied part alone (reported). |

Stored rows are allocated by the Impact Model: idle CPU power follows the CPU reservation, memory power the memory reservation and embodied emissions the CPU reservation, so a request change moves the measured CO₂e. A baseline snapshot frozen before embodied emissions were measured carries none; its measurement compares operational CO₂e with the operational projection, as before (`components = [operational_energy, idle_share]`). A window whose embodied CO₂e is zero everywhere (no hardware profile for its nodes) is treated the same way. Before Impact Model 2.0.0, `co2e_g` was reported only and recommendations stored a measured CO₂e of 0.

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

**Projection accuracy** (measured annual saving ÷ projected annual saving) is reported for each claimed quantity. It is not a gate: a saving can be realized at 30% of its projection. It feeds the track record of the recommendation's source.

### Confounders

| Confounder | Severity |
|---|---|
| Another change on the same workload landed after the merge and before the end of the post-window | severe |
| The pod template changed again during the post-window (a spec digest other than this change's) | severe |
| The workload was deleted and recreated during the post-window | severe |
| The workload's HPA changed after the rollout (replicas, so the allocated cost, moved without the change) | severe |
| The unit price of a resource whose per-replica requests the change did not touch moved more than 20% | severe |
| The node instance types running the workload changed on more than 50% of its pod-hours | severe |
| The node pool's hardware changed: its CPU-capacity-weighted instance-type mix moved by more than 25% (`pool_instance_type_change`) | severe |
| The node pool's mean CPU capacity changed by more than 50% (`pool_capacity_change`) | severe |
| Another change on the same workload landed inside the baseline window | moderate |
| The workload's HPA changed inside the baseline window | moderate |
| Mean replicas deviate more than 25% from the replicas predicted by the same counterfactual | moderate |
| The unit price of a resource whose per-replica requests the change did not touch moved more than 5% | moderate |
| The node instance types running the workload changed (total-variation distance above 25% of pod-hours) | moderate |
| The node pool's mean CPU capacity changed by more than 25% (the cluster autoscaler, `pool_capacity_change`) | moderate |
| A change to another workload whose pods ran in the same node pool landed during the measurement (`pool_change`) | moderate |
| Nodes were added to or removed from the pool during the post-window without a capacity shift (`autoscaler_activity`) | mild |

A severe confounder's detail is the reason of the `contaminated` outcome.

**Node-pool checks.** The workload's pools are the `node_pool` of the nodes its pods ran on during the post-window. The node snapshots (SCD2) are sampled every 6 hours over both windows; samples without any node of the pools (before the snapshots began) are ignored. A change to another workload counts when it completed between the merge and the end of the post-window and its pods ran on one of the pools' nodes in the two hours after its rollout. Allocated cost is requests × unit price, so other workloads' changes and autoscaling only move the idle share of CO₂e allocated to this workload (moderate), while new hardware in the pool changes its unit prices and power profile (severe). Nodes without a pool label are not grouped, and the checks are skipped. A failed read of the node snapshots or of the changes delays the decision to the next pass.

**Confidence** starts at the coverage and loses 0.15 per moderate confounder, 0.05 per mild one and 0.10 when the counterfactual has no load driver or only the CPU-usage driver; it is 0 when the measurement is contaminated.

## 7. Health

The same per-replica health gates as the interim verifier apply to the change's recommendations, grouped by container: per-pod p95 usage under the applied request × headroom, restarts, OOM kills, readiness and CPU throttling. A signal that cannot be read is reported `not_available`, never counted as a pass.

They are evaluated twice:

- once the interim health window has elapsed, so a harmful change is flagged within `VERIFICATION_WINDOW_HOURS` instead of at the end of the post-window. A failure sends every awaiting recommendation of the change to `rollback_review`. When the metrics cannot be read, the check is not recorded and is retried on the next pass;
- at the decision, over the whole post-window. The gates are stored in the measurement.

A health failure does not change the measurement outcome: the saving is still evidence about the change. It changes what happens to the recommendations.

## 8. One measurement per change (C5)

A change is measured once, on its workload. The saving is split between its recommendations:

- **by cost component** when the workload's rows carry the CPU/RAM split: CPU recommendations share the `cost_cpu` saving and memory recommendations the `cost_ram` saving, each in proportion to the request reduction it made. A recommendation claims its component share only when that component's own interval shows a saving: in a `realized` change, a recommendation whose component was `not_realized` (for example memory requests that grew) or `inconclusive` gets that outcome instead of `verified`;
- otherwise **by projected share** of the `allocated_cost` saving (equal shares when nothing was projected).

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

The repository rejects any other transition (`measuring → measuring` is the only self-transition, used for `health_checked`). Each transition is a compare-and-swap on the stored state and version, written in the same transaction as its event (`change_recorded`, `measurement_started`, `health_checked`, `measurement_decided`, `remeasure_started`, `superseded`, `regressed`, `spec_drift`, `workload_recreated`). `savings_reversed` is recorded without a state change. A re-measurement (`POST /api/v1/recommendations/{id}/remeasure` on any of the change's recommendations) starts a new post-window at the next hour and a new measurement that supersedes the previous one; the baseline stays the frozen snapshot. A newer change on the same workload closes a realized change (`superseded`).

A decision persisted before a crash is replayed: a change measurement and each recommendation's row are unique per measurement cycle, so a second pass reuses them instead of measuring again.

## 10. Drift monitor (C8)

The `savings-drift` controller loop (hourly, leader only, `greenkube.verify.drift`) watches every `realized` change.

**Drift.** The saving of a request rightsizing is the allocated cost freed per replica, so the monitor compares the workload's daily allocated cost **per replica-hour** with the baseline snapshot (`B`) and with the measured post-window (`A`):

```
lost_share(day) = (rate(day) − A) / (B − A)
```

Per-replica rates do not move with load-driven replica counts; they move when the requests creep back (a VPA in auto mode, an admission webhook, a LimitRange) or prices change. Only complete UTC days after the post-window with at least 18 hours of trusted cost count, over the last 7 days. When `lost_share ≥ CHANGE_DRIFT_THRESHOLD` (0.5) on each of the last `CHANGE_DRIFT_SUSTAINED_DAYS` (3) consecutive days, the change becomes `regressed` (event `regressed`, actor `drift-monitor`, payload: `B`, `A`, threshold, onset and the daily lost shares) and its `verified` recommendations become `regressed`, which stops their accrual and counts against their source's track record. A post-window without a per-replica saving is not monitored.

**Reversal entries.** The measured accruals written from the onset (the first day of the sustained run, or the verification time if later) until accrual stopped are reversed: one signed ledger row per recommendation, method `measured`, covering `[onset, end of the last accrued slot)` at the recommendation's measured annual rate. It is written two hours after the regression, once an accrual run in flight has finished, and its period is its idempotency key, so it is written once; the change records a `savings_reversed` event listing the rows. The ledger counters report it as reversed (`greenkube_cost_savings_reversed_dollars_total`, `greenkube_co2e_savings_reversed_grams_total`); savings accrued before the onset are kept.

**Spec drift.** When the last spec observation after the rollout has a pod-template digest other than the change's `spec_digest_after`, or another workload UID, the change is closed (`realized → closed`, event `spec_drift` or `workload_recreated`): its measured saving no longer describes what runs. A newer recorded change on the workload closes it at record time (`superseded`).

## 11. Auditing a measurement

1. `GET /api/v2/changes/{id}` returns the change, its state history and its baseline snapshot metadata. `baseline.content_verified` must be `true`.
2. `GET /api/v2/changes/{id}/measurements` returns each decided measurement with its windows, load driver, `counterfactual_params`, per-quantity estimates and intervals, confounders, confidence, health gates, `allocations` and the per-recommendation rows.
3. `evidence_sha256` covers every decided field of the measurement and `baseline_sha256` the snapshot it used. Recomputing the counterfactual from the snapshot's `hours` and `load_series`, the post-window rows and the parameters above must give the same estimates; the bootstrap seed is derived from `change_id`, `sequence` and the quantity.
4. Database triggers reject `UPDATE` and `DELETE` on `change_events`, `change_members`, `baseline_snapshots`, `change_measurements`, `change_state_events` and `savings_measurements`.

## Limitations

- **Request changes only.** `request_rightsizing` is the only strategy. Replica, schedule, deletion and node-pool changes need their own strategies.
- **Allocated, not billed.** The measured saving is allocated cost. The bill only drops when nodes are removed; pool-level realization (C10) is not implemented.
- **Pool confounders need pool labels.** Node-pool checks use the `node_pool` of the node snapshots; clusters whose nodes carry no pool label get no pool-level check. Price changes are seen per workload only (the unit price of an untouched resource).
- **Drift is per replica.** The drift monitor watches allocated cost per replica-hour, the quantity a request change moves; a regression that only shows in replica counts (an HPA retuned) is not a drift of this change.
- **One baseline week.** With the default windows each hour of the week has a single baseline value; the load regression then uses weekday/weekend hours of the day instead. The interval reflects the noise, but an unusual baseline week (a holiday, an incident) biases the counterfactual. Use a longer `CHANGE_MEASUREMENT_PRE_WINDOW_DAYS` (14 days enables hour-of-week slots) for such workloads.
- **Load-driver heuristics.** The ingress-nginx and Envoy drivers assume a Service named after the workload, and the Envoy driver assumes Contour's cluster naming. Set `greenkube.io/load-metric` when that does not hold.
- **Spec observations depend on the lifecycle interval.** A spec change reverted between two detection passes is not seen.
