# Vertical Pod Autoscaler (`vpa`)

The `vpa` source turns the advice of the [Vertical Pod Autoscaler](https://github.com/kubernetes/autoscaler/tree/master/vertical-pod-autoscaler) recommender into GreenKube recommendations. It reads VPAs that only *recommend* (`updateMode: Off`): GreenKube never competes with a VPA that already applies its own changes. The VPAs that [Goldilocks](goldilocks.md) creates are such VPAs, so the same connector reads them in the same pass.

Code: `src/greenkube/core/optimization/providers/vpa.py` (source) and `src/greenkube/collectors/vpa_collector.py` (Kubernetes reads).

## How it works

1. **Detection.** The source is available when the `verticalpodautoscalers.autoscaling.k8s.io` CRD answers a list call. Without the CRD it is skipped cleanly; nothing to configure.
2. **Read.** Every VPA of every namespace (`autoscaling.k8s.io/v1`): its `targetRef`, labels and `status.recommendation.containerRecommendations[]` (`target`, `lowerBound`, `upperBound`, `uncappedTarget`).
3. **Match.** Each container recommendation is matched with GreenKube's own usage series of the target workload and container, which give the current request.
4. **Project.** The Impact Model prices the change of that one container and resource (cost, CO2e and [released capacity](../methodology/impact-model.md#released-capacity-wp-081)), like any other rightsizing.
5. **Signals and recommendations.** Signals go to consensus with the other sources; the v1 recommendations are built as described below.

## Signals

- One signal per VPA object, container and resource (`RIGHTSIZING_CPU` or `RIGHTSIZING_MEMORY`), **increases included**: consensus decides.
- `source_id: vpa` for user VPAs, `source_id: goldilocks` for VPAs labelled `creator=Fairwinds` and `source=goldilocks`.
- All of them carry `algorithm.name: vpa-recommender`, `source_ref: <namespace>/<vpa>` and **`engine_family: vpa`**.
- `raw` keeps the target, `lowerBound`, `upperBound`, `uncappedTarget` and the provenance (`user` or `goldilocks`).
- A target at 90% or more of its `upperBound` is flagged as volatile: `evidence.data.risk_hints.volatility = {"target_to_upper": 0.94, "volatile": true}`.

### One engine, one vote

A Goldilocks VPA and a user VPA on the same container are two signals of the **same engine**, the VPA recommender. Consensus counts one vote per `engine_family`, so the VPA recommender is never counted twice against, for example, KRR or GreenKube's own analyzer. See [Consensus across sources](../recommendation.md#consensus-across-sources).

## What is skipped, and why

| Skipped | Why | Where it shows |
|---|---|---|
| VPAs whose `updateMode` is `Auto`, `Recreate`, `Initial`, `InPlaceOrRecreate` or any value other than `Off`, and VPAs without `updateMode` (the VPA API defaults it to an active mode; counted as `Auto (default)`) | The VPA already writes the pods (or sets requests at pod creation): a GreenKube pull request would fight it. In `Auto`, `Recreate`, `InPlaceOrRecreate` or `InPlace` mode it also blocks automation plans for its workload ([approval and policy](../automation.md#approval-and-policy)). | Diagnostic `skipped_vpas`, counted by mode, on the `vpa` run |
| VPAs without a `targetRef` name, or without a recommendation yet | Nothing to compare. | Not counted |
| Containers GreenKube has no usage series for | No current request to compare with, no projection. | Not counted |
| v1 recommendations: increases, and targets equal to the current request | The v1 recommendation of this source is a decrease. Increases still reach consensus as signals. | Signals only |
| v1 recommendations: two VPAs that disagree on one container and resource | Ambiguous advice is dropped rather than guessed. | Both signals stay visible |

## Diagnostics

The run is recorded under `vpa` (`GET /api/v1/sources/vpa/runs`); Goldilocks signals are stored under `goldilocks` (`GET /api/v1/sources/goldilocks/signals`).

| Code | Meaning |
|---|---|
| `skipped_vpas` | VPAs not in recommendation mode, by `updateMode`. |
| `goldilocks_vpas` | VPAs created by Goldilocks, reported as source `goldilocks`. |
| `goldilocks_namespaces`, `goldilocks_namespace_without_vpas`, `goldilocks_vpas_outside_opt_in` | Goldilocks opt-in checks, see [Goldilocks](goldilocks.md). |

## Permissions

`get`/`list` on `verticalpodautoscalers` (and on namespaces, for the Goldilocks checks). The chart's ClusterRole grants both.

## Configuration

`config.recommendations.vpaEnabled` (`RECOMMENDATION_VPA_ENABLED`) or `sources.settings.vpa.enabled` (`true`, `false`, `auto`). The same switch covers Goldilocks.

## Limitations

- GreenKube uses the VPA `target`; `lowerBound` and `upperBound` are kept as evidence and for the volatility hint, not as alternative proposals.
- A VPA's advice is only as good as its history: the recommender's own window, not GreenKube's evidence window, decides the target.
