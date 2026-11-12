# Azure Advisor for AKS (`azure_advisor`)

Azure Advisor gives configuration-level advice about AKS clusters (enable the cluster autoscaler, pick an autoscaler profile, enable cost analysis, use spot node pools...). It has no per-workload numbers. GreenKube reads that advice as **advisory signals** about the cluster (roadmap G9): they never become pull requests.

## What it reads

`GET https://management.azure.com/subscriptions/{subscription}/providers/Microsoft.Advisor/recommendations?api-version=2023-01-01`, filtered on the configured categories and paged by `nextLink`.

| Advisor | Signal |
|---|---|
| `impactedField` `Microsoft.ContainerService/managedClusters`, `impactedValue` = `cluster_name` (case-insensitive), resource group = `resource_group` when set | the recommendation is about this cluster; others are counted (`other_cluster`, `other_resource`) and never used |
| `shortDescription.solution` (else `problem`) | `AdvisoryProposal` on the `Cluster` target |
| `category` | finding kind `AZURE_ADVISOR_<CATEGORY>` (`AZURE_ADVISOR_COST`...) and `evidence.data.category` |
| text about the autoscaler, node pools, spot or VM sizes | capability `node_pool`; otherwise `hygiene` |
| `impact` | `evidence.data.impact`; every advisory is `risk_class: high` |
| `extendedProperties.savingsAmount`, `annualSavingsAmount`, `savingsCurrency` | `source_estimate` (for comparison only; GreenKube never ranks on it) |
| `recommendationTypeId` | `algorithm.params.recommendation_type_id` (`algorithm.name = azure.advisor`) |

The test fixture (`tests/sources/fixtures/azure_advisor.json`) is **synthetic**, shaped after the documented REST API, and labelled as such.

## Permissions (Workload Identity)

No cluster permission is needed. On Azure:

1. Enable [Microsoft Entra Workload ID](https://learn.microsoft.com/azure/aks/workload-identity-overview) on the cluster (OIDC issuer and workload identity).
2. Create a user-assigned managed identity, give it the **Reader** role (or **Advisor Reviewer**) on the subscription or the cluster's resource group, and federate it with GreenKube's ServiceAccount (`system:serviceaccount:<namespace>:<service account>`).
3. Label the controller's pods (they run the scans) `azure.workload.identity/use: "true"` and annotate their ServiceAccount with `azure.workload.identity/client-id: <client id>`. The chart does not template pod labels or ServiceAccount annotations yet: create the ServiceAccount yourself (`serviceAccount.create: false`, `serviceAccount.name`) and add the pod label with a post-renderer (for example Kustomize) until it does.

The AKS webhook then mounts a federated token and sets `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and `AZURE_FEDERATED_TOKEN_FILE`; GreenKube exchanges the token for an Azure Resource Manager token at each collection. No secret is stored.

## Configuration

```yaml
sources:
  settings:
    azure_advisor:
      subscription_id: 00000000-0000-0000-0000-000000000000   # required
      cluster_name: aks-prod                                  # required: the AKS resource name
      resource_group: rg-prod                                 # optional, narrows the match
      categories: [Cost, HighAvailability, OperationalExcellence, Performance]   # default
```

The source is `unavailable` (never failing) until the subscription, the cluster name and the Workload Identity variables are present. It collects at most once per hour.

## Limitations

- Advice only: no proposal is PR-able. Configuration changes (autoscaler profile, add-ons) are made in Azure, not in Git manifests.
- Advisor's savings figures are kept for comparison; GreenKube's Impact Model does not price cluster-level configuration changes.
