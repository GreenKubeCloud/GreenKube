"""Tests for Helm chart ServiceMonitor and NetworkPolicy templates."""

import subprocess

import yaml

CHART_PATH = "helm-chart"


def helm_template(set_values: list[str] | None = None) -> list[dict]:
    """Render Helm chart and return all manifests as a list of dicts."""
    cmd = ["helm", "template", "greenkube", CHART_PATH, "--namespace", "greenkube"]
    for sv in set_values or []:
        cmd.extend(["--set", sv])
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    docs = []
    for doc in result.stdout.split("---"):
        doc = doc.strip()
        if not doc:
            continue
        try:
            parsed = yaml.safe_load(doc)
            if parsed and isinstance(parsed, dict):
                docs.append(parsed)
        except yaml.YAMLError:
            # Some Helm-rendered fragments (e.g. Secret data) may not parse cleanly
            continue
    return docs


def find_manifest(docs: list[dict], kind: str, name_contains: str = "") -> dict | None:
    """Find a manifest by kind and optional name substring."""
    for doc in docs:
        if doc.get("kind") == kind:
            if not name_contains or name_contains in doc.get("metadata", {}).get("name", ""):
                return doc
    return None


class TestDefaultValues:
    """Tests that default values produce a working install without Prometheus Operator."""

    def test_servicemonitor_disabled_by_default(self):
        """ServiceMonitor must be disabled by default so fresh installs don't require CRDs."""
        docs = helm_template()
        sm = find_manifest(docs, "ServiceMonitor")
        assert sm is None, "ServiceMonitor should NOT be created with default values"

    def test_networkpolicy_disabled_by_default(self):
        """NetworkPolicy for Prometheus should be disabled by default."""
        docs = helm_template()
        np = find_manifest(docs, "NetworkPolicy", "allow-prometheus")
        assert np is None, "NetworkPolicy should NOT be created with default values"

    def test_prometheus_rbac_not_created_by_default(self):
        """Prometheus RBAC (Role/RoleBinding) should not be created by default."""
        docs = helm_template()
        role = find_manifest(docs, "Role", "prometheus-k8s")
        rb = find_manifest(docs, "RoleBinding", "prometheus-k8s")
        assert role is None, "Role should NOT be created with default values"
        assert rb is None, "RoleBinding should NOT be created with default values"

    def test_no_monitoring_coreos_resources_by_default(self):
        """No monitoring.coreos.com resources should be rendered with default values."""
        docs = helm_template()
        for doc in docs:
            api_version = doc.get("apiVersion", "")
            assert "monitoring.coreos.com" not in api_version, (
                f"Found monitoring.coreos.com resource ({doc.get('kind')}) in default render"
            )

    def test_crd_check_job_not_created_by_default(self):
        """Pre-install CRD check job should not be created when serviceMonitor is disabled."""
        docs = helm_template()
        job = find_manifest(docs, "Job", "crd-check")
        assert job is None, "CRD check job should NOT be created with default values"


class TestPreInstallCRDCheck:
    """Tests for the pre-install CRD validation hook."""

    def test_crd_check_created_when_servicemonitor_enabled(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        job = find_manifest(docs, "Job", "crd-check")
        assert job is not None, "CRD check job should be created when serviceMonitor is enabled"

    def test_crd_check_has_pre_install_hook(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        job = find_manifest(docs, "Job", "crd-check")
        annotations = job["metadata"]["annotations"]
        assert "pre-install" in annotations.get("helm.sh/hook", "")
        assert "pre-upgrade" in annotations.get("helm.sh/hook", "")

    def test_crd_check_runs_before_other_hooks(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        job = find_manifest(docs, "Job", "crd-check")
        annotations = job["metadata"]["annotations"]
        assert annotations.get("helm.sh/hook-weight") == "-10"

    def test_crd_check_not_created_when_servicemonitor_disabled(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=false"])
        job = find_manifest(docs, "Job", "crd-check")
        assert job is None


class TestServiceMonitor:
    """Tests for the ServiceMonitor Helm template."""

    def test_servicemonitor_created_when_enabled(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        sm = find_manifest(docs, "ServiceMonitor")
        assert sm is not None, "ServiceMonitor should be created when enabled"

    def test_servicemonitor_not_created_when_disabled(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=false"])
        sm = find_manifest(docs, "ServiceMonitor")
        assert sm is None, "ServiceMonitor should not be created when disabled"

    def test_servicemonitor_namespace_is_monitoring(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        sm = find_manifest(docs, "ServiceMonitor")
        assert sm["metadata"]["namespace"] == "monitoring"

    def test_servicemonitor_has_kube_prometheus_label(self):
        """ServiceMonitor must have the label that the Prometheus Operator selects on."""
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        sm = find_manifest(docs, "ServiceMonitor")
        assert sm["metadata"]["labels"]["app.kubernetes.io/part-of"] == "kube-prometheus"

    def test_servicemonitor_targets_greenkube_namespace(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        sm = find_manifest(docs, "ServiceMonitor")
        ns_selector = sm["spec"]["namespaceSelector"]["matchNames"]
        assert "greenkube" in ns_selector

    def test_servicemonitor_selects_api_service(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        sm = find_manifest(docs, "ServiceMonitor")
        labels = sm["spec"]["selector"]["matchLabels"]
        assert labels.get("app.kubernetes.io/component") == "api"
        assert labels.get("app.kubernetes.io/name") == "greenkube"

    def test_servicemonitor_endpoint_path(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        sm = find_manifest(docs, "ServiceMonitor")
        ep = sm["spec"]["endpoints"][0]
        assert ep["path"] == "/prometheus/metrics"

    def test_servicemonitor_endpoint_port(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        sm = find_manifest(docs, "ServiceMonitor")
        ep = sm["spec"]["endpoints"][0]
        assert ep["port"] == "http"

    def test_servicemonitor_custom_interval(self):
        docs = helm_template(
            [
                "monitoring.serviceMonitor.enabled=true",
                "monitoring.serviceMonitor.interval=60s",
            ]
        )
        sm = find_manifest(docs, "ServiceMonitor")
        assert sm["spec"]["endpoints"][0]["interval"] == "60s"

    def test_servicemonitor_custom_namespace(self):
        docs = helm_template(
            [
                "monitoring.serviceMonitor.enabled=true",
                "monitoring.serviceMonitor.namespace=custom-monitoring",
            ]
        )
        sm = find_manifest(docs, "ServiceMonitor")
        assert sm["metadata"]["namespace"] == "custom-monitoring"

    def test_servicemonitor_has_metric_relabelings(self):
        """ServiceMonitor must restore exported_namespace/exported_pod labels so that
        Grafana sees the real K8s namespace of the measured pod, not greenkube."""
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        sm = find_manifest(docs, "ServiceMonitor")
        relabelings = sm["spec"]["endpoints"][0].get("metricRelabelings", [])
        assert len(relabelings) >= 4, "ServiceMonitor should have at least 4 metricRelabelings"

    def test_servicemonitor_relabeling_restores_namespace(self):
        """exported_namespace must be copied back to namespace."""
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        sm = find_manifest(docs, "ServiceMonitor")
        relabelings = sm["spec"]["endpoints"][0].get("metricRelabelings", [])
        ns_rule = next(
            (r for r in relabelings if r.get("targetLabel") == "namespace"),
            None,
        )
        assert ns_rule is not None, "No relabeling rule restoring namespace label"
        assert "exported_namespace" in ns_rule.get("sourceLabels", [])

    def test_servicemonitor_relabeling_restores_pod(self):
        """exported_pod must be copied back to pod."""
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        sm = find_manifest(docs, "ServiceMonitor")
        relabelings = sm["spec"]["endpoints"][0].get("metricRelabelings", [])
        pod_rule = next(
            (r for r in relabelings if r.get("targetLabel") == "pod"),
            None,
        )
        assert pod_rule is not None, "No relabeling rule restoring pod label"
        assert "exported_pod" in pod_rule.get("sourceLabels", [])


class TestNetworkPolicy:
    """Tests for the NetworkPolicy allowing Prometheus scraping."""

    def test_networkpolicy_created_when_enabled(self):
        docs = helm_template(["monitoring.networkPolicy.enabled=true"])
        np = find_manifest(docs, "NetworkPolicy", "allow-prometheus")
        assert np is not None, "NetworkPolicy should be created when enabled"

    def test_networkpolicy_not_created_when_disabled(self):
        docs = helm_template(["monitoring.networkPolicy.enabled=false"])
        np = find_manifest(docs, "NetworkPolicy", "allow-prometheus")
        assert np is None, "NetworkPolicy should not be created when disabled"

    def test_networkpolicy_in_release_namespace(self):
        docs = helm_template(["monitoring.networkPolicy.enabled=true"])
        np = find_manifest(docs, "NetworkPolicy", "allow-prometheus")
        assert np["metadata"]["namespace"] == "greenkube"

    def test_networkpolicy_selects_app_pods(self):
        docs = helm_template(["monitoring.networkPolicy.enabled=true"])
        np = find_manifest(docs, "NetworkPolicy", "allow-prometheus")
        labels = np["spec"]["podSelector"]["matchLabels"]
        assert labels.get("app.kubernetes.io/name") == "greenkube"
        assert labels.get("app.kubernetes.io/component") == "app"

    def test_networkpolicy_allows_from_monitoring_namespace(self):
        docs = helm_template(["monitoring.networkPolicy.enabled=true"])
        np = find_manifest(docs, "NetworkPolicy", "allow-prometheus")
        ingress_from = np["spec"]["ingress"][0]["from"][0]
        ns_labels = ingress_from["namespaceSelector"]["matchLabels"]
        assert ns_labels["kubernetes.io/metadata.name"] == "monitoring"

    def test_networkpolicy_allows_from_prometheus_pods(self):
        docs = helm_template(["monitoring.networkPolicy.enabled=true"])
        np = find_manifest(docs, "NetworkPolicy", "allow-prometheus")
        ingress_from = np["spec"]["ingress"][0]["from"][0]
        pod_labels = ingress_from["podSelector"]["matchLabels"]
        assert pod_labels["app.kubernetes.io/name"] == "prometheus"

    def test_networkpolicy_port_matches_api_port(self):
        docs = helm_template(["monitoring.networkPolicy.enabled=true"])
        np = find_manifest(docs, "NetworkPolicy", "allow-prometheus")
        port = np["spec"]["ingress"][0]["ports"][0]
        assert port["port"] == 8000
        assert port["protocol"] == "TCP"

    def test_networkpolicy_custom_prometheus_namespace(self):
        docs = helm_template(
            [
                "monitoring.networkPolicy.enabled=true",
                "monitoring.networkPolicy.prometheusNamespace=observability",
            ]
        )
        np = find_manifest(docs, "NetworkPolicy", "allow-prometheus")
        ingress_from = np["spec"]["ingress"][0]["from"][0]
        ns_labels = ingress_from["namespaceSelector"]["matchLabels"]
        assert ns_labels["kubernetes.io/metadata.name"] == "observability"

    def test_networkpolicy_only_ingress(self):
        docs = helm_template(["monitoring.networkPolicy.enabled=true"])
        np = find_manifest(docs, "NetworkPolicy", "allow-prometheus")
        assert np["spec"]["policyTypes"] == ["Ingress"]


class TestPrometheusRBAC:
    """Tests for the Role and RoleBinding that let Prometheus discover endpoints."""

    def test_role_created_when_servicemonitor_enabled(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        role = find_manifest(docs, "Role", "prometheus-k8s")
        assert role is not None, "Role should be created when serviceMonitor is enabled"

    def test_role_not_created_when_servicemonitor_disabled(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=false"])
        role = find_manifest(docs, "Role", "prometheus-k8s")
        assert role is None, "Role should not be created when serviceMonitor is disabled"

    def test_role_in_release_namespace(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        role = find_manifest(docs, "Role", "prometheus-k8s")
        assert role["metadata"]["namespace"] == "greenkube"

    def test_role_allows_services_pods(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        role = find_manifest(docs, "Role", "prometheus-k8s")
        core_rule = next(r for r in role["rules"] if "" in r["apiGroups"])
        assert "services" in core_rule["resources"]
        assert "pods" in core_rule["resources"]
        assert set(core_rule["verbs"]) == {"get", "list", "watch"}

    def test_role_allows_endpointslices(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        role = find_manifest(docs, "Role", "prometheus-k8s")
        discovery_rule = next(r for r in role["rules"] if "discovery.k8s.io" in r["apiGroups"])
        assert "endpointslices" in discovery_rule["resources"]
        assert set(discovery_rule["verbs"]) == {"get", "list", "watch"}

    def test_rolebinding_created_when_servicemonitor_enabled(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        rb = find_manifest(docs, "RoleBinding", "prometheus-k8s")
        assert rb is not None, "RoleBinding should be created when serviceMonitor is enabled"

    def test_rolebinding_not_created_when_servicemonitor_disabled(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=false"])
        rb = find_manifest(docs, "RoleBinding", "prometheus-k8s")
        assert rb is None, "RoleBinding should not be created when serviceMonitor is disabled"

    def test_rolebinding_references_correct_role(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        rb = find_manifest(docs, "RoleBinding", "prometheus-k8s")
        assert rb["roleRef"]["kind"] == "Role"
        assert rb["roleRef"]["name"] == "prometheus-k8s"

    def test_rolebinding_subject_is_prometheus_sa(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        rb = find_manifest(docs, "RoleBinding", "prometheus-k8s")
        subject = rb["subjects"][0]
        assert subject["kind"] == "ServiceAccount"
        assert subject["name"] == "prometheus-k8s"
        assert subject["namespace"] == "monitoring"

    def test_rolebinding_custom_service_account(self):
        docs = helm_template(
            [
                "monitoring.serviceMonitor.enabled=true",
                "monitoring.serviceMonitor.prometheusServiceAccount=custom-prom",
            ]
        )
        rb = find_manifest(docs, "RoleBinding", "prometheus-k8s")
        assert rb["subjects"][0]["name"] == "custom-prom"
