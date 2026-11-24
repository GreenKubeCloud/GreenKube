# Goldilocks (`goldilocks`)

[Goldilocks](https://github.com/FairwindsOps/goldilocks) creates one VerticalPodAutoscaler in recommendation mode (`updateMode: Off`) per workload of the namespaces you opt in. Its advice *is* the VPA recommender's advice, so GreenKube reads those VPAs through the [VPA source](vpa.md), in the same pass, and labels their signals `goldilocks` (roadmap G1). There is no separate connector to enable.

## What it reads

- The VPAs, as described in [Vertical Pod Autoscaler](vpa.md). A VPA labelled `creator=Fairwinds` and `source=goldilocks` is a Goldilocks VPA.
- Namespaces labelled `goldilocks.fairwinds.com/enabled=true`, for diagnostics only.

## Signals

- Goldilocks VPAs produce signals with `source_id: goldilocks`, every other VPA with `source_id: vpa`. Both carry `algorithm.name: vpa-recommender`, `source_ref: <namespace>/<vpa>` and **`engine_family: vpa`**: a Goldilocks VPA and a user VPA on the same container are two signals of the same engine, and consensus counts them as one vote.
- Everything else (increases in signals, volatility hint, v1 decreases only) is the [VPA source's behaviour](vpa.md#signals).

Goldilocks signals are stored under `goldilocks` (`GET /api/v1/sources/goldilocks/signals`); the run is recorded under `vpa` (`GET /api/v1/sources/vpa/runs`), with these Goldilocks diagnostics:

| Code | Meaning |
|---|---|
| `goldilocks_vpas` | VPAs created by Goldilocks. |
| `goldilocks_namespaces` | Namespaces opted into Goldilocks. |
| `goldilocks_namespace_without_vpas` (warning) | Opted-in namespaces without any Goldilocks VPA in recommendation mode: check that the Goldilocks controller runs. |
| `goldilocks_vpas_outside_opt_in` | Goldilocks VPAs in namespaces without the opt-in label. |

## Permissions

The chart's ClusterRole already grants `get`/`list` on `verticalpodautoscalers` and namespaces. Nothing to configure for Goldilocks.

## Configuration

`sources.settings.vpa.enabled` (`true`, `false`, `auto`; default `auto`: the source runs when the VPA CRD exists). The deprecated Helm value `config.recommendations.vpaEnabled` (`RECOMMENDATION_VPA_ENABLED`) still applies when `sources.settings.vpa.enabled` is not set. There is no separate switch for Goldilocks: it is the same connector.

## Limitations

- Goldilocks' dashboard choices (its QoS presets) are not read: GreenKube uses the VPA `target`, as Goldilocks' "Burstable" view does.
- Namespace opt-in labels are read for diagnostics only; Goldilocks VPAs are used wherever they are.
