-- 0087: resource bindings by group, kind and name (roadmap H0d, H6). Additive.
--
-- Discovery records which Argo CD Application or Flux Kustomization manages
-- each resource of the kinds a registered action kind targets (a LoadBalancer
-- Service or PersistentVolume to delete), with the controller's resolved Git
-- location and whether it prunes. Plans for non-workload targets resolve their
-- Git source here. Each refresh replaces the rows of its cluster. The empty
-- namespace is cluster scope.
CREATE TABLE IF NOT EXISTS resource_bindings (
    cluster TEXT NOT NULL,
    api_group TEXT NOT NULL,
    kind TEXT NOT NULL,
    namespace TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (cluster, api_group, kind, namespace, name)
);
