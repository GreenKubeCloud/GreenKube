# GKE recommender (`gke`)

Google's Active Assist flags GKE workloads whose requests are too high or too low. GreenKube reads these **insights** from the Recommender API and turns them into signals (roadmap G7). The source is read-only: it never writes to the cluster or to Google Cloud.

## What it reads

| Recommender API | Signal |
|---|---|
| Insight types listed at runtime (`GET v1beta1/insightTypes`): every `google.container.*` type (today `google.container.DiagnosisInsight`) | the types read; set `insight_types` to pin them |
| Insights of those types (`GET v1/projects/{project}/locations/{location}/insightTypes/{type}/insights`), in the cluster's location and, for a zonal cluster, its region | one or more signals per insight |
| `insightSubtype` `WORKLOAD_OVERPROVISIONED` / `WORKLOAD_UNDERPROVISIONED`, `stateInfo.state` `ACTIVE` | only these are used; others are counted (`other_subtype`, `inactive`) |
| `targetResources` (`//container.googleapis.com/projects/P/locations/L/clusters/C/k8s/namespaces/NS/apps/deployments/NAME`) | `target` (Deployment, StatefulSet, DaemonSet, ReplicaSet, Job, CronJob, Rollout) in this GreenKube's cluster |
| container recommended requests in the insight `content` | `ContainerResourcesProposal`, kind `RIGHTSIZING_CPU` / `RIGHTSIZING_MEMORY`, decreases and increases |
| no readable values in `content` | `AdvisoryProposal`, kind `WORKLOAD_OVERPROVISIONED` / `WORKLOAD_UNDERPROVISIONED`: it stays a signal (never a pull request) |
| `observationPeriod` | `algorithm.window` and `observed_window` (`algorithm.name = gke.recommender`, `params.insight_type`, `params.insight_subtype`) |
| `severity`, `category`, `description`, `lastRefreshTime` | `evidence` |

**Why the insight type is discovered.** The insight type id is not stable in Google's public documentation, so the source lists the insight types each collection and reads the `google.container.*` ones.

**Insight content.** The content of the GKE workload insights is not publicly documented. The parser reads a container list (`containerRecommendations`, `containers` or `containerResources`, at the top of `content` or under `content.recommendation`) with the container name (`containerName` or `name`) and its recommended requests (`recommendedRequests`, `recommended` or `target`, with `cpu` and `memory` as Kubernetes quantities). Nothing is guessed: an insight without readable values becomes an advisory signal that says which way to go. The test fixture (`tests/sources/fixtures/gke_insights.json`) is **synthetic** and labelled as such: check it against a recording of your project before relying on the values.

**Attribution.** Insights about another cluster or project than this one are skipped and counted (`other_cluster`): advice is never attributed to a same-named workload elsewhere.

**Coverage gaps (always reported).** GKE gives no recommendation for workloads whose requests VPA manages, nor for the target metric of an HPA, and may wait up to three days for new workloads. Every batch carries the `gke_coverage` diagnostic so that a missing insight is never read as "nothing to change".

## How it runs

The source runs in the controller's scans when it is discovered: GreenKube runs in a GKE cluster and the metadata server answers. It collects at most once per hour (insights are refreshed about daily) and its signals reach recommendations only through consensus, where they can agree with VPA, Goldilocks, KRR or the native analyzers.

## Permissions (Workload Identity)

No cluster permission is needed beyond GreenKube's own. On Google Cloud:

1. Enable the Recommender API (`recommender.googleapis.com`) in the project.
2. Bind the ServiceAccount of GreenKube's controller (it runs the scans; `<release>-controller` by default, in the release namespace) to Google IAM. With Workload Identity Federation for GKE, grant the role to the KSA principal directly (no annotation needed):

```bash
PROJECT_ID=my-project
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --role=roles/recommender.viewer \
  --member="principal://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$PROJECT_ID.svc.id.goog/subject/ns/greenkube/sa/greenkube-controller"
```

   Or bind a Google service account (`roles/iam.workloadIdentityUser` on it for the KSA, `roles/recommender.viewer` on the project for it) and annotate the KSA with `iam.gke.io/gcp-service-account: <gsa>@<project>.iam.gserviceaccount.com`. The chart does not template ServiceAccount annotations yet: create the ServiceAccount yourself and set `serviceAccount.create: false` and `serviceAccount.name`, or prefer the direct principal binding above. The Recommender Viewer role reads insights only; a custom role with the insight type's `list` permission works too.

The source reads its project id, cluster name and location from the metadata server; the access token comes from the same server, so no key file is ever stored.

## Configuration

```yaml
sources:
  settings:
    gke:
      enabled: auto                     # default; false turns it off
      # Only needed when the metadata server does not report them:
      # project_id: my-project
      # cluster_name: prod-gke          # the GKE cluster name in targetResources
      # location: europe-west1-b
      # insight_types: ["google.container.DiagnosisInsight"]   # skip discovery
      # timeout_seconds: 30
```

## Limitations

- The Cloud Monitoring VPA metrics (`kubernetes.io/autoscaler/container/{cpu,memory}/per_replica_recommended_request_*`), the alternative input of the roadmap, are not read yet.
- Recommender quotas apply per project; the hourly rate limit keeps GreenKube far below them.
- Insights about workloads GreenKube does not observe are kept (consensus reports them with the other sources).
