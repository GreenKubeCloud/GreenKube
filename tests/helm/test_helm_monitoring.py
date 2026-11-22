"""Tests for Helm chart ServiceMonitor and NetworkPolicy templates."""

import subprocess

import yaml

CHART_PATH = "helm-chart"


def helm_template(set_values: list[str] | None = None) -> list[dict]:
    """Render Helm chart and return all manifests as a list of dicts."""
    cmd = ["helm", "template", "greenkube", CHART_PATH, "--namespace", "greenkube"]
    for sv in set_values or []:
        cmd.extend(["--set", sv])
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=True)
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


def api_service_monitor(docs: list[dict]) -> dict | None:
    """Return the API ServiceMonitor (the controller and worker have their own)."""
    for doc in docs:
        if doc.get("kind") != "ServiceMonitor":
            continue
        labels = doc.get("spec", {}).get("selector", {}).get("matchLabels", {})
        if labels.get("app.kubernetes.io/component") == "api":
            return doc
    return None


class TestDefaultValues:
    """Tests that default values produce a working install without Prometheus Operator."""

    def test_servicemonitor_disabled_by_default(self):
        """ServiceMonitor must be disabled by default so fresh installs don't require CRDs."""
        docs = helm_template()
        sm = api_service_monitor(docs)
        assert sm is None, "ServiceMonitor should NOT be created with default values"

    def test_networkpolicy_disabled_by_default(self):
        """NetworkPolicy for Prometheus should be disabled by default."""
        docs = helm_template()
        np = find_manifest(docs, "NetworkPolicy", "api-netpol")
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


class TestNoCRDCheckHook:
    """The pre-install CRD check hook is gone (F11: no Helm hook except the test pod and the
    credentials bootstrap); a missing ServiceMonitor CRD makes Helm fail on its own."""

    def test_no_crd_check_job_when_servicemonitor_enabled(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        assert find_manifest(docs, "Job", "crd-check") is None


class TestServiceMonitor:
    """Tests for the ServiceMonitor Helm template."""

    def test_servicemonitor_created_when_enabled(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        sm = api_service_monitor(docs)
        assert sm is not None, "ServiceMonitor should be created when enabled"

    def test_servicemonitor_not_created_when_disabled(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=false"])
        sm = api_service_monitor(docs)
        assert sm is None, "ServiceMonitor should not be created when disabled"

    def test_servicemonitor_defaults_to_the_release_namespace(self):
        """Next to the scrape token Secret, which Prometheus Operator reads in the ServiceMonitor namespace."""
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        sm = api_service_monitor(docs)
        assert sm["metadata"]["namespace"] == "greenkube"  # type: ignore[index]

    def test_servicemonitor_has_kube_prometheus_label(self):
        """ServiceMonitor must have the label that the Prometheus Operator selects on."""
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        sm = api_service_monitor(docs)
        assert sm["metadata"]["labels"]["app.kubernetes.io/part-of"] == "kube-prometheus"  # type: ignore[index]

    def test_servicemonitor_targets_greenkube_namespace(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        sm = api_service_monitor(docs)
        ns_selector = sm["spec"]["namespaceSelector"]["matchNames"]  # type: ignore[index]
        assert "greenkube" in ns_selector

    def test_servicemonitor_selects_api_service(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        sm = api_service_monitor(docs)
        labels = sm["spec"]["selector"]["matchLabels"]  # type: ignore[index]
        assert labels.get("app.kubernetes.io/component") == "api"
        assert labels.get("app.kubernetes.io/name") == "greenkube"

    def test_servicemonitor_endpoint_path(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        sm = api_service_monitor(docs)
        ep = sm["spec"]["endpoints"][0]  # type: ignore[index]
        assert ep["path"] == "/prometheus/metrics"

    def test_servicemonitor_endpoint_port(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        sm = api_service_monitor(docs)
        ep = sm["spec"]["endpoints"][0]  # type: ignore[index]
        assert ep["port"] == "http"

    def test_servicemonitor_custom_interval(self):
        docs = helm_template(
            [
                "monitoring.serviceMonitor.enabled=true",
                "monitoring.serviceMonitor.interval=60s",
            ]
        )
        sm = api_service_monitor(docs)
        assert sm["spec"]["endpoints"][0]["interval"] == "60s"  # type: ignore[index]

    def test_servicemonitor_custom_namespace(self):
        docs = helm_template(
            [
                "monitoring.serviceMonitor.enabled=true",
                "monitoring.serviceMonitor.namespace=custom-monitoring",
            ]
        )
        sm = api_service_monitor(docs)
        assert sm["metadata"]["namespace"] == "custom-monitoring"  # type: ignore[index]

    def test_servicemonitor_restores_only_the_exported_cluster_label(self):
        """No API series has a namespace, pod or region label of its own any more (B.7): only cluster is restored."""
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        sm = api_service_monitor(docs)
        relabelings = sm["spec"]["endpoints"][0].get("metricRelabelings", [])  # type: ignore[index]
        restored = {r.get("targetLabel") for r in relabelings if r.get("sourceLabels", [""])[0].startswith("exported_")}
        assert restored == {"cluster"}
        assert not any("exported_namespace" in str(r) or "exported_pod" in str(r) for r in relabelings)

    def test_servicemonitor_defaults_an_empty_cluster_label(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        sm = api_service_monitor(docs)
        relabelings = sm["spec"]["endpoints"][0].get("metricRelabelings", [])  # type: ignore[index]
        assert {
            "sourceLabels": ["cluster"],
            "regex": "^$",
            "replacement": "default",
            "targetLabel": "cluster",
            "action": "replace",
        } in relabelings


class TestNetworkPolicy:
    """Tests for the NetworkPolicy allowing Prometheus scraping."""

    def test_networkpolicy_created_when_enabled(self):
        docs = helm_template(["monitoring.networkPolicy.enabled=true"])
        np = find_manifest(docs, "NetworkPolicy", "api-netpol")
        assert np is not None, "NetworkPolicy should be created when enabled"

    def test_networkpolicy_not_created_when_disabled(self):
        docs = helm_template(["monitoring.networkPolicy.enabled=false"])
        np = find_manifest(docs, "NetworkPolicy", "api-netpol")
        assert np is None, "NetworkPolicy should not be created when disabled"

    def test_networkpolicy_in_release_namespace(self):
        docs = helm_template(["monitoring.networkPolicy.enabled=true"])
        np = find_manifest(docs, "NetworkPolicy", "api-netpol")
        assert np["metadata"]["namespace"] == "greenkube"  # type: ignore[index]

    def test_networkpolicy_selects_api_pods(self):
        docs = helm_template(["monitoring.networkPolicy.enabled=true"])
        np = find_manifest(docs, "NetworkPolicy", "api-netpol")
        labels = np["spec"]["podSelector"]["matchLabels"]  # type: ignore[index]
        assert labels.get("app.kubernetes.io/name") == "greenkube"
        assert labels.get("app.kubernetes.io/component") == "api"

    def test_networkpolicy_allows_from_monitoring_namespace(self):
        docs = helm_template(["monitoring.networkPolicy.enabled=true"])
        np = find_manifest(docs, "NetworkPolicy", "api-netpol")
        ingress_from = np["spec"]["ingress"][0]["from"][0]  # type: ignore[index]
        ns_labels = ingress_from["namespaceSelector"]["matchLabels"]
        assert ns_labels["kubernetes.io/metadata.name"] == "monitoring"

    def test_networkpolicy_allows_from_prometheus_pods(self):
        docs = helm_template(["monitoring.networkPolicy.enabled=true"])
        np = find_manifest(docs, "NetworkPolicy", "api-netpol")
        ingress_from = np["spec"]["ingress"][0]["from"][0]  # type: ignore[index]
        pod_labels = ingress_from["podSelector"]["matchLabels"]
        assert pod_labels["app.kubernetes.io/name"] == "prometheus"

    def test_networkpolicy_port_matches_api_port(self):
        docs = helm_template(["monitoring.networkPolicy.enabled=true"])
        np = find_manifest(docs, "NetworkPolicy", "api-netpol")
        port = np["spec"]["ingress"][0]["ports"][0]  # type: ignore[index]
        assert port["port"] == 8000
        assert port["protocol"] == "TCP"

    def test_networkpolicy_custom_prometheus_namespace(self):
        docs = helm_template(
            [
                "monitoring.networkPolicy.enabled=true",
                "monitoring.networkPolicy.prometheusNamespace=observability",
            ]
        )
        np = find_manifest(docs, "NetworkPolicy", "api-netpol")
        ingress_from = np["spec"]["ingress"][0]["from"][0]  # type: ignore[index]
        ns_labels = ingress_from["namespaceSelector"]["matchLabels"]
        assert ns_labels["kubernetes.io/metadata.name"] == "observability"

    def test_networkpolicy_restricts_egress_to_dns_and_postgres(self):
        docs = helm_template(["monitoring.networkPolicy.enabled=true"])
        np = find_manifest(docs, "NetworkPolicy", "api-netpol")
        assert np["spec"]["policyTypes"] == ["Ingress", "Egress"]  # type: ignore[index]
        egress = np["spec"]["egress"]  # type: ignore[index]
        assert egress[0]["ports"][0]["port"] == 53
        assert egress[1]["ports"][0]["port"] == 5432


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
        assert role["metadata"]["namespace"] == "greenkube"  # type: ignore[index]

    def test_role_allows_services_pods(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        role = find_manifest(docs, "Role", "prometheus-k8s")
        core_rule = next(r for r in role["rules"] if "" in r["apiGroups"])  # type: ignore[index]
        assert "services" in core_rule["resources"]
        assert "pods" in core_rule["resources"]
        assert set(core_rule["verbs"]) == {"get", "list", "watch"}

    def test_role_allows_endpointslices(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        role = find_manifest(docs, "Role", "prometheus-k8s")
        discovery_rule = next(r for r in role["rules"] if "discovery.k8s.io" in r["apiGroups"])  # type: ignore[index]
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
        assert rb["roleRef"]["kind"] == "Role"  # type: ignore[index]
        assert rb["roleRef"]["name"] == "prometheus-k8s"  # type: ignore[index]

    def test_rolebinding_subject_is_prometheus_sa(self):
        docs = helm_template(["monitoring.serviceMonitor.enabled=true"])
        rb = find_manifest(docs, "RoleBinding", "prometheus-k8s")
        subject = rb["subjects"][0]  # type: ignore[index]
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
        assert rb["subjects"][0]["name"] == "custom-prom"  # type: ignore[index]


RELEASE_NAMESPACE = "greenkube"


def _hook_env(docs: list[dict]) -> dict[str, str]:
    job = find_manifest(docs, "Job", "credentials-bootstrap")
    if job is None:
        return {}
    env = job["spec"]["template"]["spec"]["containers"][0]["env"]
    return {item["name"]: item.get("value", "") for item in env}


def _available_secrets(docs: list[dict]) -> set[tuple[str, str]]:
    """(namespace, name) of every Secret the chart renders or its bootstrap hook creates."""
    available = {
        (doc["metadata"].get("namespace", RELEASE_NAMESPACE), doc["metadata"]["name"])
        for doc in docs
        if doc.get("kind") == "Secret"
    }
    env = _hook_env(docs)
    for key in ("GREENKUBE_SECRET_NAME", "GREENKUBE_INITIAL_ADMIN_SECRET"):
        if env.get(key):
            available.add((RELEASE_NAMESPACE, env[key]))
    if env.get("GREENKUBE_SCRAPE_TOKEN_SECRET"):
        available.add((env["GREENKUBE_SCRAPE_TOKEN_NAMESPACE"], env["GREENKUBE_SCRAPE_TOKEN_SECRET"]))
    return available


def _secret_references(node, found: set[str]) -> set[str]:
    """Names of the Secrets an object requires (volumes, env, ServiceMonitor credentials).

    Optional references (``optional: true``, e.g. the user-created Git credentials) are skipped.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key in {"secretKeyRef", "secretRef", "secret", "credentials"} and isinstance(value, dict):
                if value.get("name") and not value.get("optional"):
                    found.add(value["name"])
            elif key == "secretName" and isinstance(value, str) and value:
                found.add(value)
            _secret_references(value, found)
    elif isinstance(node, list):
        for item in node:
            _secret_references(item, found)
    return found


class TestSecretReferencesResolve:
    """Every Secret an object references must exist in that object's namespace.

    Prometheus Operator resolves a ServiceMonitor's bearer token Secret in the
    ServiceMonitor's own namespace and rejects the monitor otherwise.
    """

    MODES = {
        "default": [],
        "override": ["monitoring.serviceMonitor.namespace=monitoring"],
        "override-values-token": ["monitoring.serviceMonitor.namespace=monitoring", "secrets.metricsToken=tok"],
    }

    def _render(self, extra: list[str]) -> list[dict]:
        cmd = ["helm", "template", "greenkube", CHART_PATH, "--namespace", RELEASE_NAMESPACE]
        for value in ["monitoring.serviceMonitor.enabled=true", "worker.replicas=1", *extra]:
            cmd.extend(["--set", value])
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=True)
        return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]

    def _assert_resolved(self, docs: list[dict]) -> None:
        available = _available_secrets(docs)
        missing = []
        for doc in docs:
            namespace = doc["metadata"].get("namespace", RELEASE_NAMESPACE)
            for name in _secret_references(doc.get("spec", {}), set()):
                if (namespace, name) not in available:
                    missing.append(f"{doc['kind']}/{doc['metadata']['name']} -> {namespace}/{name}")
        assert not missing, f"Secrets referenced outside their namespace: {missing}"

    def test_default_mode_keeps_servicemonitors_next_to_their_secret(self):
        docs = self._render(self.MODES["default"])
        monitors = [doc for doc in docs if doc.get("kind") == "ServiceMonitor"]
        assert len(monitors) == 3
        assert {doc["metadata"]["namespace"] for doc in monitors} == {RELEASE_NAMESPACE}
        self._assert_resolved(docs)

    def test_override_mode_copies_the_scrape_token_next_to_the_servicemonitors(self):
        docs = self._render(self.MODES["override"])
        monitors = [doc for doc in docs if doc.get("kind") == "ServiceMonitor"]
        assert {doc["metadata"]["namespace"] for doc in monitors} == {"monitoring"}
        self._assert_resolved(docs)
        role = next(
            doc for doc in docs if doc.get("kind") == "Role" and doc["metadata"].get("namespace") == "monitoring"
        )
        assert role["metadata"]["name"].endswith("credentials-bootstrap")

    def test_override_mode_with_a_values_scrape_token(self):
        self._assert_resolved(self._render(self.MODES["override-values-token"]))
