# Kubecost (`kubecost`)

GreenKube reads Kubecost's savings APIs and turns them into signals (roadmap G3). Kubecost's own savings figures are kept for comparison only: GreenKube recomputes every impact with its Impact Model.

## What it reads

| Kubecost | Signal |
|---|---|
| Request sizing: `GET /model/savings/requestSizingV2` with `window`, `algorithmCPU`, `algorithmRAM`, `qCPU`, `qRAM`, `targetCPUUtilization`, `targetRAMUtilization`, `filter` | `ContainerResourcesProposal` per container and resource (`RIGHTSIZING_CPU`/`RIGHTSIZING_MEMORY`), decreases and increases; `monthlySavings` in `source_estimate`; the query parameters in `algorithm.params` |
| Abandoned workloads: `GET /model/savings/abandonedWorkloads?days=` | `DeletionProposal`, capability `cleanup`, kind `ABANDONED_WORKLOAD`, one per owner workload |
| Cluster / node-group sizing | `AdvisoryProposal` about the cluster, capability `node_pool`, kind `NODE_GROUP_SIZING`, `risk_class: high` |

**Abandoned workloads are review-only.** Kubecost's only evidence is low network traffic. The signal says so (`evidence.data.basis: network_traffic`, the traffic rates, `deletion_requires_review: true`), and GreenKube's deletion-safety rules still decide: the minimum unused duration (`DELETION_MIN_UNUSED_DAYS`), dependency evidence and `deletion_requires_review`. A Kubecost finding alone never deletes anything.

## Versions and paths

`health()` probes `GET /model/clusterInfo` and reads the major version. The paths come from a versioned map:

| Endpoint | 2.x | 3.x |
|---|---|---|
| version probe | `/model/clusterInfo` | `/model/clusterInfo` |
| request sizing | `/model/savings/requestSizingV2` | `/model/savings/requestSizingV2` |
| abandoned workloads | `/model/savings/abandonedWorkloads` | `/model/savings/abandonedWorkloads` |
| node-group sizing | `/model/savings/clusterSizingETL` | `/model/savings/nodeGroupSizing` |

Kubecost 3.x moved to the `finops-agent` with ClickHouse and renamed cluster sizing "Node Group Sizing"; validate the paths against your deployed version. An endpoint that answers `404` is reported as a `path_not_found` diagnostic and skipped (the rest of the run still counts); override any path with `paths`. An unknown version uses the 2.x map and the source reports `degraded`.

Answers wrapped as `{"code": 200, "data": …}` or `{"Recommendations": […]}` and bare arrays are all accepted. The test fixtures (`tests/sources/fixtures/kubecost/v2.json`, `v3.json`) follow the documented response shapes; check them against a recording of your version when you upgrade Kubecost.

## Permissions

Network access from the controller to the Kubecost API (no Kubernetes permission). Kubecost's own API needs no credentials in-cluster; GreenKube sends no secret.

## Configuration

`SOURCES_CONFIG["kubecost"]` (Helm `sources.settings.kubecost`):

| Key | Default | Meaning |
|---|---|---|
| `url` | `http://kubecost-cost-analyzer.kubecost.svc:9090` (probed in-cluster only) | Kubecost API base URL. |
| `version` | probed | `"2"` or `"3"`. |
| `window` | `7d` | Lookback in days (Kubecost recommends more than 2 days; hourly windows are not supported). |
| `algorithm_cpu`, `algorithm_ram` | `max` | `max` or `quantile` (`quantile` needs Kubecost's ContainerStats pipeline). |
| `q_cpu`, `q_ram` | unset | Quantiles for `quantile`. |
| `target_cpu_utilization`, `target_ram_utilization` | `0.7` | Headroom targets. |
| `filter` | unset | Kubecost filter expression. |
| `cluster_id` | unset | This cluster's Kubecost id. Required when Kubecost reports several clusters (federated or 3.x aggregator): without it the advice is dropped (`multiple_clusters`) rather than guessed. |
| `abandoned_workloads`, `node_group_sizing` | `true` | Turn either query off. |
| `paths` | `{}` | Path overrides: `version`, `request_sizing`, `abandoned_workloads`, `node_group_sizing`. |
| `timeout_seconds` | `30` | Per request. |

```yaml
sources:
  settings:
    kubecost: { url: "http://kubecost-cost-analyzer.kubecost:9090", window: "14d", cluster_id: "prod-eu" }
```

The source collects at most every 30 minutes (`min_interval_seconds`) because savings queries are heavy for Kubecost.

## Licensing

Kubecost's free tier is limited to clusters with up to $100K of spend per 30 days; beyond that, the savings APIs need a Kubecost license. GreenKube does not check your license: an endpoint that answers an error is reported in the run's diagnostics.

## Limitations

- Bare pods (no controller) are skipped (`no_controller`): they cannot become pull requests.
- Node-group sizing is advisory only: GreenKube does not change node groups.
