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

**Attribution.** An insight is this cluster's only when its target names this project (by id, or by number as read from the metadata server's `project/numeric-project-id`), this cluster's exact location and its name. Anything else is skipped and counted (`other_cluster`): advice is never attributed to a same-named workload elsewhere. In particular a zonal and a regional cluster may share a name in one project, and the region's insights are read for a zonal cluster, so an insight under the region about a same-named regional cluster is skipped.

**Coverage gaps (always reported).** GKE gives no recommendation for workloads whose requests VPA manages, nor for the target metric of an HPA, and may wait up to three days for new workloads. Every batch carries the `gke_coverage` diagnostic so that a missing insight is never read as "nothing to change".

## How it runs

The source runs in the controller's scans when it is discovered: GreenKube runs in a GKE cluster and the metadata server answers. It collects at most once per hour (insights are refreshed about daily) and its signals reach recommendations only through consensus, where they can agree with VPA, Goldilocks, KRR or the native analyzers.

## Permissions (Workload Identity)

No cluster permission is needed beyond GreenKube's own. On Google Cloud:

1. Enable the Recommender API (`recommender.googleapis.com`) in the project.
2. Bind the ServiceAccount of GreenKube's controller (it runs the scans; `<release>-controller` by default, in the release namespace) to Google IAM. It needs **two roles** on the project:
   - `roles/recommender.viewer` reads the insights;
   - `roles/serviceusage.serviceUsageConsumer` lets the source name the project as its quota project. Every Recommender call (insight type discovery and `insights.list`) sends `X-Goog-User-Project: <project>`, and Google answers `403 PERMISSION_DENIED` ("Caller does not have required permission to use project") to a caller without `serviceusage.services.use` on that project.

   With Workload Identity Federation for GKE, grant both roles to the KSA principal directly (no annotation needed):

```bash
PROJECT_ID=my-project
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
MEMBER="principal://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$PROJECT_ID.svc.id.goog/subject/ns/greenkube/sa/greenkube-controller"
for ROLE in roles/recommender.viewer roles/serviceusage.serviceUsageConsumer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --role="$ROLE" --member="$MEMBER"
done
```

   Or bind a Google service account (`roles/iam.workloadIdentityUser` on it for the KSA; `roles/recommender.viewer` and `roles/serviceusage.serviceUsageConsumer` on the project for it) and annotate the KSA with `iam.gke.io/gcp-service-account: <gsa>@<project>.iam.gserviceaccount.com`. The chart does not template ServiceAccount annotations yet: create the ServiceAccount yourself and set `serviceAccount.create: false` and `serviceAccount.name`, or prefer the direct principal binding above. Both roles are read-only. A custom role works too: it needs the insight type's `list` permission and `serviceusage.services.use`.

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

## Network policy

With `monitoring.networkPolicy.enabled`, the controller's egress is limited to DNS, PostgreSQL and the renderer. Allow the metadata server and Google APIs through `monitoring.networkPolicy.additionalEgress`, for example:

```yaml
monitoring:
  networkPolicy:
    additionalEgress:
      - to:
          - ipBlock: {cidr: 169.254.169.254/32}   # GKE metadata server (Workload Identity tokens)
        ports:
          - {protocol: TCP, port: 80}
      - to:
          - ipBlock: {cidr: 169.254.169.252/32}   # GKE metadata server on Dataplane V2
        ports:
          - {protocol: TCP, port: 988}
      - to:
          - ipBlock: {cidr: 0.0.0.0/0}            # recommender.googleapis.com (or your Private Google Access range)
        ports:
          - {protocol: TCP, port: 443}
```

Without these rules the source reports `unavailable` (metadata server unreachable) or fails its run; it never writes anything.

## Limitations

- The Cloud Monitoring VPA metrics (`kubernetes.io/autoscaler/container/{cpu,memory}/per_replica_recommended_request_*`), the alternative input of the roadmap, are not read yet.
- Recommender quotas apply per project; the hourly rate limit keeps GreenKube far below them.
- Insights about workloads GreenKube does not observe are kept (consensus reports them with the other sources).
