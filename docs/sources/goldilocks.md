# VPA and Goldilocks (`vpa`, `goldilocks`)

The `vpa` source reads VerticalPodAutoscalers in recommendation mode (`updateMode: Off`). [Goldilocks](https://github.com/FairwindsOps/goldilocks) creates exactly such VPAs, so GreenKube reads them in the same pass and labels their advice `goldilocks` (roadmap G1).

## What it reads

- VPAs of every namespace (`autoscaling.k8s.io/v1`), their `targetRef`, labels and `status.recommendation.containerRecommendations[]`: `target`, `lowerBound`, `upperBound` and `uncappedTarget`. VPAs in `Auto`, `Recreate`, `Initial` or other active modes are skipped and counted (diagnostic `skipped_vpas`).
- Namespaces labelled `goldilocks.fairwinds.com/enabled=true`, for diagnostics only.

## Signals

- One signal per VPA object, container and resource (`RIGHTSIZING_CPU`/`RIGHTSIZING_MEMORY`), increases included.
- VPAs labelled `creator=Fairwinds` and `source=goldilocks` produce signals with `source_id: goldilocks`, every other VPA with `source_id: vpa`. Both carry `algorithm.name: vpa-recommender`, `source_ref: <namespace>/<vpa>` and **`engine_family: vpa`**: a Goldilocks VPA and a user VPA on the same container are two signals of the same engine, and consensus counts them as one vote.
- `raw` keeps the target, `lowerBound`, `upperBound`, `uncappedTarget` and the provenance (`goldilocks` or `user`).
- A target at 90% or more of its `upperBound` is flagged as volatile: `evidence.data.risk_hints.volatility = {"target_to_upper": 0.94, "volatile": true}`.
- The v1 recommendations are unchanged: decreases only, one per container and resource, none when two VPAs disagree.

Goldilocks signals are stored under `goldilocks` (`GET /api/v1/sources/goldilocks/signals`); the run is recorded under `vpa` (`GET /api/v1/sources/vpa/runs`), with the diagnostics:

| Code | Meaning |
|---|---|
| `skipped_vpas` | VPAs not in recommendation mode, by `updateMode`. |
| `goldilocks_vpas` | VPAs created by Goldilocks. |
| `goldilocks_namespaces` | Namespaces opted into Goldilocks. |
| `goldilocks_namespace_without_vpas` (warning) | Opted-in namespaces without any Goldilocks VPA in recommendation mode: check that the Goldilocks controller runs. |
| `goldilocks_vpas_outside_opt_in` | Goldilocks VPAs in namespaces without the opt-in label. |

## Permissions

The chart's ClusterRole already grants `get`/`list` on `verticalpodautoscalers` and namespaces. Nothing to configure for Goldilocks.

## Configuration

`config.recommendations.vpaEnabled` (`RECOMMENDATION_VPA_ENABLED`) or `sources.settings.vpa.enabled` (`true`, `false`, `auto`). There is no separate switch for Goldilocks: it is the same connector.

## Limitations

- Goldilocks' dashboard choices (its QoS presets) are not read: GreenKube uses the VPA `target`, as Goldilocks' "Burstable" view does.
- Namespace opt-in labels are read for diagnostics only; Goldilocks VPAs are used wherever they are.
