### Fixed

- `helm install --wait` no longer times out with `postgres.backup.enabled` on a StorageClass that binds on first use (`WaitForFirstConsumer`: kind and k3s local-path, EBS gp3, GKE standard-rwo, AKS managed-csi). The backup claim stayed `Pending` until the first nightly dump, and Helm waits for every claim to be bound; a one-shot Job now mounts the new claim at install and on each upgrade.
- On a multi-node cluster without `config.clusterName`, every GreenKube pod used its own node's name as the cluster name, so the API and the controller could disagree (GitOps bindings listed under another cluster) and a rescheduled controller started a second collection watermark and savings-ledger identity. The auto-detected name is now the same in every pod: the node name on a single-node cluster, a cluster-name node label (EKS, GKE, AKS, Cluster API), else `default`. Single-node clusters keep their current name.
- The end-to-end harness (`hack/e2e`) no longer drives a GreenKube or Gitea port-forward it did not open (for example another e2e run's, on another cluster, on the same port), runs without Argo CD (`E2E_ARGOCD=0`), and names the Gitea token scope a failed repository creation needs. `hack/e2e/kind-cilium/` adds a kind + Cilium (kube-proxy replacement, LB IPAM, Gateway API) environment.

### Changed

- The security model documents how to allow Kubernetes API egress under `monitoring.networkPolicy` on Cilium: an `ipBlock` for the API Service or its endpoints never matches there (the API server is the `kube-apiserver` entity), so the controller cannot read its Lease. A CiliumNetworkPolicy with `toEntities: [kube-apiserver]`, or Cilium's `policyCIDRMatchMode: nodes`, is needed.
