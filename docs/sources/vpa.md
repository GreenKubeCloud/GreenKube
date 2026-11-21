# Vertical Pod Autoscaler (`vpa`)

The `vpa` source turns the advice of the [Vertical Pod Autoscaler](https://github.com/kubernetes/autoscaler/tree/master/vertical-pod-autoscaler) recommender into GreenKube recommendations. It reads VPAs that only *recommend* (`updateMode: Off`): GreenKube never competes with a VPA that already applies its own changes. The VPAs that [Goldilocks](goldilocks.md) creates are such VPAs, so the same connector reads them in the same pass.

Code: `src/greenkube/core/optimization/providers/vpa.py` (source) and `src/greenkube/collectors/vpa_collector.py` (Kubernetes reads).

## How it works

1. **Detection.** The source is available when the `verticalpodautoscalers.autoscaling.k8s.io` CRD answers a list call. Without the CRD it is skipped cleanly; nothing to configure.
2. **Read.** Every VPA of every namespace (`autoscaling.k8s.io/v1`): its `targetRef`, labels, `status.conditions` and `status.recommendation.containerRecommendations[]` (`target`, `lowerBound`, `upperBound`, `uncappedTarget`), and every `VerticalPodAutoscalerCheckpoint` (`totalSamplesCount`, `firstSampleStart`, `lastSampleStart` per VPA and container).
3. **Maturity.** A VPA votes only once mature (see [Maturity](#maturity)).
4. **Match.** Each container recommendation is matched with GreenKube's own usage series of the target workload and container, which give the current request.
5. **Project.** The Impact Model prices the change of that one container and resource (cost, CO2e and [released capacity](../methodology/impact-model.md#released-capacity-wp-081)), like any other rightsizing.
6. **Signals and recommendations.** Signals go to consensus with the other sources; the v1 recommendations are built as described below. VPA is a corroborating vote: **VPA alone never creates a recommendation**. A container needs a proposal from GreenKube's own measurement; without it the VPA advice stays a signal, visible on the Sources page (`GET /api/v1/sources/vpa/signals`).

## Signals

- One signal per VPA object, container and resource (`RIGHTSIZING_CPU` or `RIGHTSIZING_MEMORY`), **increases included**: consensus decides.
- `source_id: vpa` for user VPAs, `source_id: goldilocks` for VPAs labelled `creator=Fairwinds` and `source=goldilocks`.
- All of them carry `algorithm.name: vpa-recommender`, `source_ref: <namespace>/<vpa>` and **`engine_family: vpa`**.
- `raw` keeps the target, `lowerBound`, `upperBound`, `uncappedTarget`, the provenance (`user` or `goldilocks`), the number of containers the VPA recommends for (`containers`) and the maturity it was judged on (`maturity`: conditions, `total_samples`, `sample_hours`).
- A target at 90% or more of its `upperBound` is flagged as volatile: `evidence.data.risk_hints.volatility = {"target_to_upper": 0.94, "volatile": true}`.

### One engine, one vote

A Goldilocks VPA and a user VPA on the same container are two signals of the **same engine**, the VPA recommender. Consensus counts one vote per `engine_family`, so the VPA recommender is never counted twice against, for example, KRR or GreenKube's own analyzer. See [Consensus across sources](../recommendation.md#consensus-across-sources).

## Maturity

Minutes after a VPA is created, its recommender has a few samples and very wide bounds (the field report saw 3 to 28 samples and an `upperBound` of 15 cores), yet its `target` looks like normal advice. GreenKube ignores a VPA until it is mature:

- `LowConfidence=True` or `RecommendationProvided=False` in `status.conditions`: not mature;
- otherwise the sample history of the container's checkpoint must cover `sources.vpa.minSampleHours` (`VPA_MIN_SAMPLE_HOURS`, default `24`): the span from `firstSampleStart` to `lastSampleStart`, or `totalSamplesCount` at one sample a minute when the timestamps are missing; a container without a checkpoint has no samples when the recommender checkpoints the VPA's other containers or the VPA is younger than `minSampleHours`;
- `0` turns the check off.

An immature VPA yields neither a signal nor a recommendation and is counted in the `immature_vpas` diagnostic. When the checkpoints cannot be read (for example the ClusterRole lacks `verticalpodautoscalercheckpoints`) or the recommender keeps none for an older VPA (`--storage=prometheus`), maturity is unknown: the `vpa_maturity_unknown` warning names the count, and the VPA may still hold or raise a value but never pushes a decrease under the `priority`, `median` or `weighted` policies (`ignored_votes` reason `maturity_unknown`; under `safest` a lower value never wins anyway).

## Tool floors and small changes

The VPA recommender never proposes less than its per-pod minimum (`--pod-recommendation-min-memory-mb`, `--pod-recommendation-min-cpu-millicores`), split evenly between the pod's containers. The Fairwinds `vpa` chart sets 100 MB (the recommender reads it as MiB) and 15m. A target equal to that minimum says "the floor", not "the need", so it is **not counted as a vote** in the consensus: it can never win over GreenKube's measured value. In the field, seaweedfs-volume measured 42 Mi while VPA and KRR both proposed 100 Mi; the retained value is GreenKube's 42 Mi plus headroom.

The floors are set per engine family with `sources.toolFloors.vpa` (`SIGNAL_TOOL_FLOORS`, default `{cpu: 15m, memory: 100Mi}`); set them to the flags your recommender runs with. A VPA target that changes the current request by less than `sources.minChange` (5 % and 10m CPU or 16 MiB memory by default) is not a vote either. Both cases are kept in the recommendation's `consensus.ignored_votes` (`reason: tool_floor` or `below_min_change`).

## What is skipped, and why

| Skipped | Why | Where it shows |
|---|---|---|
| VPAs whose `updateMode` is `Auto`, `Recreate`, `Initial`, `InPlaceOrRecreate` or any value other than `Off`, and VPAs without `updateMode` (the VPA API defaults it to an active mode; counted as `Auto (default)`) | The VPA already writes the pods (or sets requests at pod creation): a GreenKube pull request would fight it. In `Auto`, `Recreate`, `InPlaceOrRecreate` or `InPlace` mode it also blocks automation plans for its workload ([approval and policy](../automation.md#approval-and-policy)). | Diagnostic `skipped_vpas`, counted by mode, on the `vpa` run |
| VPAs without a `targetRef` name, or without a recommendation yet | Nothing to compare. | Not counted |
| VPAs that are not mature yet | Their target rests on too few samples. | Diagnostic `immature_vpas` |
| Targets on the recommender's floor, or below the minimum change | Not a signal of need. | `consensus.ignored_votes` of the recommendation |
| Containers with no native GreenKube proposal | VPA corroborates, it never decides alone. | Signals only |
| Containers GreenKube has no usage series for | No current request to compare with, no projection. | Not counted |
| v1 recommendations: increases, and targets equal to the current request | The v1 recommendation of this source is a decrease. Increases still reach consensus as signals. | Signals only |
| v1 recommendations: two VPAs that disagree on one container and resource | Ambiguous advice is dropped rather than guessed. | Both signals stay visible |

## Diagnostics

The run is recorded under `vpa` (`GET /api/v1/sources/vpa/runs`); Goldilocks signals are stored under `goldilocks` (`GET /api/v1/sources/goldilocks/signals`).

| Code | Meaning |
|---|---|
| `skipped_vpas` | VPAs not in recommendation mode, by `updateMode`. |
| `immature_vpas` | VPAs ignored until mature (less than `VPA_MIN_SAMPLE_HOURS` of samples, or `LowConfidence`). |
| `vpa_maturity_unknown` (warning) | VPAs whose checkpoint could not be read: they still vote. |
| `goldilocks_vpas` | VPAs created by Goldilocks, reported as source `goldilocks`. |
| `goldilocks_namespaces`, `goldilocks_namespace_without_vpas`, `goldilocks_vpas_outside_opt_in` | Goldilocks opt-in checks, see [Goldilocks](goldilocks.md). |

## Permissions

`get`/`list` on `verticalpodautoscalers` and `verticalpodautoscalercheckpoints` (and on namespaces, for the Goldilocks checks). The chart's ClusterRole grants them.

## Configuration

`config.recommendations.vpaEnabled` (`RECOMMENDATION_VPA_ENABLED`) or `sources.settings.vpa.enabled` (`true`, `false`, `auto`). The same switch covers Goldilocks.

| Setting | Default | Meaning |
|---|---|---|
| `sources.vpa.minSampleHours` (`VPA_MIN_SAMPLE_HOURS`) | `24` | Hours of samples before a VPA votes; `0` disables the check. |
| `sources.toolFloors.vpa` (`SIGNAL_TOOL_FLOORS`) | `{cpu: 15m, memory: 100Mi}` | The recommender's per-pod minimum; a target on it is not a vote. |
| `sources.minChange.*` (`SIGNAL_MIN_CHANGE_*`) | 5 %, 10m, 16 MiB | Smallest change that counts as a vote. |

## Limitations

- GreenKube uses the VPA `target`; `lowerBound` and `upperBound` are kept as evidence and for the volatility hint, not as alternative proposals.
- A VPA's advice is only as good as its history: the recommender's own window, not GreenKube's evidence window, decides the target.
