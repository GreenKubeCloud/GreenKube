# tests/collectors/test_lb_collector.py
"""
Tests for the LoadBalancerCollector: a LoadBalancer Service is reported only
with evidence that it is unused.

Regression (user-reported): on a kubeadm cluster GreenKube recommended deleting
the Cilium Gateway API LoadBalancer, the cluster's entry point, because its
legacy Endpoints object was empty. The fixtures are kubectl-shaped manifests
(``tests/collectors/fixtures/deletion_evidence``).
"""

import copy
from unittest.mock import AsyncMock

import pytest

from greenkube.collectors.k8s_object_reader import (
    STATUS_ABSENT,
    STATUS_UNREADABLE,
    KubernetesObjectReader,
    StaticObjectReader,
)
from greenkube.collectors.lb_collector import LoadBalancerCollector, OrphanedLoadBalancer, enrich_orphaned_lb_costs

from .deletion_fixtures import load_objects, reader_for  # pyrefly: ignore[missing-import]


async def scan(reader):
    return await LoadBalancerCollector(reader=reader).scan()


def _service(namespace, name, selector, ip="198.51.100.7", annotations=None, labels=None):
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "uid": f"uid-{name}",
            "annotations": annotations or {},
            "labels": labels or {},
        },
        "spec": {
            "ports": [{"port": 443, "protocol": "TCP", "targetPort": 8443}],
            "selector": selector,
            "type": "LoadBalancer",
        },
        "status": {"loadBalancer": {"ingress": [{"ip": ip}]}},
    }


class TestRegressionCiliumGateway:
    """The user's case: a Cilium Gateway API LoadBalancer is never reported."""

    @pytest.mark.asyncio
    async def test_cilium_gateway_service_is_never_a_candidate(self):
        result = await scan(reader_for("cilium_gateway.json"))

        assert result.complete is True
        assert result.candidates == []
        reason = result.skipped["gateway/cilium-gateway-cluster-gateway"]
        assert "no selector" in reason

    @pytest.mark.asyncio
    async def test_cilium_gateway_is_protected_even_with_a_selector(self):
        """Ownership by the Gateway alone is enough (a selector does not make it unused)."""
        objects = load_objects("cilium_gateway.json")
        service = objects["services"][0]
        service["spec"]["selector"] = {"io.cilium.gateway/owning-gateway": "cluster-gateway"}
        result = await scan(StaticObjectReader(objects))
        assert result.candidates == []
        assert "owned by Gateway cluster-gateway" in result.skipped["gateway/cilium-gateway-cluster-gateway"]

    @pytest.mark.asyncio
    async def test_cilium_gateway_is_protected_by_its_labels_without_owner(self):
        objects = load_objects("cilium_gateway.json")
        service = objects["services"][0]
        service["spec"]["selector"] = {"app": "nothing"}
        service["metadata"].pop("ownerReferences")
        result = await scan(StaticObjectReader(objects))
        assert result.candidates == []
        assert "gateway, ingress or mesh controller" in result.skipped["gateway/cilium-gateway-cluster-gateway"]

    @pytest.mark.asyncio
    async def test_a_gateway_address_protects_a_service_without_any_marker(self):
        """A Service whose external IP is a Gateway address carries the Gateway's traffic."""
        objects = load_objects("cilium_gateway.json")
        bare = _service("gateway", "edge-lb", {"app": "edge"}, ip="192.168.10.240")
        objects["services"] = [bare]
        result = await scan(StaticObjectReader(objects))
        assert result.candidates == []
        assert "Gateway gateway/cluster-gateway uses its address" in result.skipped["gateway/edge-lb"]


class TestControllerAndEndpointSliceServices:
    @pytest.mark.asyncio
    async def test_istio_ingress_gateway_is_never_a_candidate(self):
        """The ingress gateway is protected by its mesh labels even with its pods scaled to zero."""
        result = await scan(reader_for("istio_ingressgateway.json"))
        assert result.candidates == []
        assert "istio" in result.skipped["istio-ingress/istio-ingressgateway"]

    @pytest.mark.asyncio
    async def test_endpointslice_only_service_is_in_use(self):
        """No legacy Endpoints object (dual-stack, mirroring): the EndpointSlices show the backends."""
        result = await scan(reader_for("endpointslice_only.json"))
        assert result.candidates == []
        assert result.skipped["shop/storefront"] == "2 endpoint(s) in 1 EndpointSlice(s)"

    @pytest.mark.asyncio
    async def test_not_ready_legacy_endpoints_count_as_in_use(self):
        service = _service("shop", "checkout", {"app": "checkout"})
        endpoints = {
            "kind": "Endpoints",
            "metadata": {"name": "checkout", "namespace": "shop"},
            "subsets": [{"notReadyAddresses": [{"ip": "10.244.1.9"}], "ports": [{"port": 8443}]}],
        }
        result = await scan(StaticObjectReader({"services": [service], "endpoints": [endpoints]}))
        assert result.candidates == []
        assert result.skipped["shop/checkout"] == "1 endpoint(s) in its Endpoints object"


class TestTrueOrphan:
    @pytest.mark.asyncio
    async def test_true_orphan_is_a_candidate_with_evidence(self):
        result = await scan(reader_for("true_orphan.json"))

        assert result.complete is True
        assert len(result.candidates) == 1
        candidate = result.candidates[0]
        assert isinstance(candidate, OrphanedLoadBalancer)
        assert (candidate.namespace, candidate.name) == ("legacy", "old-api")
        assert candidate.external_ip == "203.0.113.10"
        assert candidate.ports == "80/TCP"
        assert candidate.uid == "7e3d9a1c-0b2f-4c6d-8e5a-1f9b3c7d2e80"
        assert candidate.first_seen_at is None  # set by first-seen persistence only
        evidence = " | ".join(candidate.evidence)
        assert "selector app.kubernetes.io/name=old-api matches no pod" in evidence
        assert "0 endpoints (ready or not)" in evidence
        assert "no Gateway, Ingress or Gateway API route" in evidence

    @pytest.mark.asyncio
    async def test_gateway_api_not_installed_is_evidence_not_a_blocker(self):
        statuses = {
            kind: STATUS_ABSENT
            for kind in ("gateways", "httproutes", "grpcroutes", "tlsroutes", "tcproutes", "udproutes")
        }
        result = await scan(reader_for("true_orphan.json", statuses=statuses))
        assert [c.name for c in result.candidates] == ["old-api"]

    @pytest.mark.parametrize("annotation", [{"greenkube.io/ignore": "true"}, {"greenkube.io/keep": ""}])
    @pytest.mark.asyncio
    async def test_opt_out_annotations(self, annotation):
        objects = load_objects("true_orphan.json")
        objects["services"][0]["metadata"]["annotations"].update(annotation)
        result = await scan(StaticObjectReader(objects))
        assert result.candidates == []
        assert "opted out" in result.skipped["legacy/old-api"]

    @pytest.mark.asyncio
    async def test_keep_false_does_not_opt_out(self):
        objects = load_objects("true_orphan.json")
        objects["services"][0]["metadata"]["annotations"]["greenkube.io/keep"] = "false"
        result = await scan(StaticObjectReader(objects))
        assert [c.name for c in result.candidates] == ["old-api"]


class TestInUseEvidence:
    @pytest.mark.asyncio
    async def test_matching_pod_means_in_use(self):
        pod = {
            "kind": "Pod",
            "metadata": {"name": "old-api-0", "namespace": "legacy", "labels": {"app.kubernetes.io/name": "old-api"}},
            "status": {"phase": "Pending"},
        }
        result = await scan(reader_for("true_orphan.json", extra={"pods": [pod]}))
        assert result.candidates == []
        assert result.skipped["legacy/old-api"] == "its selector matches 1 pod(s)"

    @pytest.mark.parametrize(
        "kind,workload",
        [
            (
                "deployments",
                {
                    "kind": "Deployment",
                    "metadata": {"name": "old-api", "namespace": "legacy"},
                    "spec": {
                        "replicas": 0,
                        "template": {"metadata": {"labels": {"app.kubernetes.io/name": "old-api", "tier": "web"}}},
                    },
                },
            ),
            (
                "cronjobs",
                {
                    "kind": "CronJob",
                    "metadata": {"name": "old-api-batch", "namespace": "legacy"},
                    "spec": {
                        "jobTemplate": {
                            "spec": {"template": {"metadata": {"labels": {"app.kubernetes.io/name": "old-api"}}}}
                        }
                    },
                },
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_workload_scaled_to_zero_means_in_use(self, kind, workload):
        result = await scan(reader_for("true_orphan.json", extra={kind: [workload]}))
        assert result.candidates == []
        assert "selects it" in result.skipped["legacy/old-api"]

    @pytest.mark.asyncio
    async def test_ingress_publishing_the_address_means_in_use(self):
        ingress = {
            "kind": "Ingress",
            "metadata": {"name": "shop", "namespace": "apps"},
            "spec": {
                "rules": [
                    {
                        "host": "shop.example",
                        "http": {
                            "paths": [
                                {
                                    "path": "/",
                                    "pathType": "Prefix",
                                    "backend": {"service": {"name": "shop", "port": {"number": 80}}},
                                }
                            ]
                        },
                    }
                ]
            },
            "status": {"loadBalancer": {"ingress": [{"ip": "203.0.113.10"}]}},
        }
        result = await scan(reader_for("true_orphan.json", extra={"ingresses": [ingress]}))
        assert result.candidates == []
        assert result.skipped["legacy/old-api"] == "Ingress apps/shop publishes its address"

    @pytest.mark.asyncio
    async def test_ingress_backend_means_in_use(self):
        ingress = {
            "kind": "Ingress",
            "metadata": {"name": "old", "namespace": "legacy"},
            "spec": {"defaultBackend": {"service": {"name": "old-api", "port": {"number": 80}}}},
        }
        result = await scan(reader_for("true_orphan.json", extra={"ingresses": [ingress]}))
        assert result.skipped["legacy/old-api"] == "Ingress legacy/old routes to it"

    @pytest.mark.asyncio
    async def test_route_backend_ref_in_another_namespace_means_in_use(self):
        route = {
            "kind": "HTTPRoute",
            "metadata": {"name": "api", "namespace": "apps"},
            "spec": {"rules": [{"backendRefs": [{"name": "old-api", "namespace": "legacy", "port": 80}]}]},
        }
        result = await scan(reader_for("true_orphan.json", extra={"httproutes": [route]}))
        assert result.skipped["legacy/old-api"] == "HTTPRoute apps/api references it"

    @pytest.mark.asyncio
    async def test_route_to_a_same_named_service_elsewhere_does_not_count(self):
        route = {
            "kind": "HTTPRoute",
            "metadata": {"name": "api", "namespace": "apps"},
            "spec": {"rules": [{"backendRefs": [{"name": "old-api", "port": 80}]}]},
        }
        result = await scan(reader_for("true_orphan.json", extra={"httproutes": [route]}))
        assert [c.name for c in result.candidates] == ["old-api"]


class TestFailClosed:
    @pytest.mark.parametrize(
        "kind,status",
        [
            ("endpointslices", STATUS_UNREADABLE),
            ("endpointslices", STATUS_ABSENT),
            ("pods", STATUS_UNREADABLE),
            ("deployments", STATUS_UNREADABLE),
            ("cronjobs", STATUS_UNREADABLE),
            ("gateways", STATUS_UNREADABLE),
            ("ingresses", STATUS_UNREADABLE),
            ("httproutes", STATUS_UNREADABLE),
            ("endpoints", STATUS_UNREADABLE),
        ],
    )
    @pytest.mark.asyncio
    async def test_unreadable_evidence_means_no_candidate(self, kind, status):
        result = await scan(reader_for("true_orphan.json", statuses={kind: status}))
        assert result.candidates == []
        assert "cannot prove it is unused" in result.skipped["legacy/old-api"]

    @pytest.mark.asyncio
    async def test_services_unreadable_returns_none(self):
        reader = reader_for("true_orphan.json", statuses={"services": STATUS_UNREADABLE})
        assert await LoadBalancerCollector(reader=reader).collect() is None

    @pytest.mark.asyncio
    async def test_reader_exception_returns_none(self):
        reader = AsyncMock()
        reader.list = AsyncMock(side_effect=RuntimeError("API down"))
        assert await LoadBalancerCollector(reader=reader).collect() is None

    @pytest.mark.asyncio
    async def test_mixed_cluster(self):
        """All fixtures together: only the true orphan is reported."""
        reader = reader_for(
            "cilium_gateway.json", "istio_ingressgateway.json", "endpointslice_only.json", "true_orphan.json"
        )
        candidates = await LoadBalancerCollector(reader=reader).collect()
        assert candidates is not None
        assert [(c.namespace, c.name) for c in candidates] == [("legacy", "old-api")]

    @pytest.mark.asyncio
    async def test_non_load_balancer_services_are_not_evaluated(self):
        objects = load_objects("true_orphan.json")
        objects["services"][0]["spec"]["type"] = "ClusterIP"
        reader = StaticObjectReader(objects)
        result = await scan(reader)
        assert result.candidates == [] and result.skipped == {}
        assert [call[0] for call in reader.calls] == ["services"]


class TestKubernetesObjectReader:
    """Failure classification of the live reader (HTTP status of ApiException)."""

    @pytest.mark.parametrize(
        "status,expected", [(404, STATUS_ABSENT), (403, STATUS_UNREADABLE), (500, STATUS_UNREADABLE)]
    )
    @pytest.mark.asyncio
    async def test_classifies_api_errors(self, status, expected):
        from kubernetes_asyncio.client.exceptions import ApiException

        reader = KubernetesObjectReader(api_client=object())

        async def failing(_cont):
            raise ApiException(status=status)

        result = await reader._paginate("gateways", failing)
        assert result.status == expected
        assert result.known is (expected == STATUS_ABSENT)

    @pytest.mark.asyncio
    async def test_paginates(self):
        reader = KubernetesObjectReader(api_client=object())
        pages = {None: ([{"a": 1}], "next"), "next": ([{"b": 2}], None)}

        async def fetch(cont):
            return pages[cont]

        result = await reader._paginate("pods", fetch)
        assert result.items == [{"a": 1}, {"b": 2}] and result.status == "ok"

    @pytest.mark.asyncio
    async def test_custom_kind_falls_back_to_older_version(self, monkeypatch):
        from kubernetes_asyncio.client.exceptions import ApiException

        class FakeCustomObjects:
            def __init__(self, api_client=None):
                pass

            async def list_cluster_custom_object(self, group, version, plural, **kwargs):
                if version == "v1":
                    raise ApiException(status=404)
                return {"items": [{"metadata": {"name": "gw"}}], "metadata": {}}

        import kubernetes_asyncio.client as k8s

        monkeypatch.setattr(k8s, "CustomObjectsApi", FakeCustomObjects)
        result = await KubernetesObjectReader(api_client=object()).list("gateways")
        assert result.status == "ok" and result.items == [{"metadata": {"name": "gw"}}]

    @pytest.mark.asyncio
    async def test_builtin_kind_is_serialized_to_kubernetes_json(self, monkeypatch):
        from kubernetes_asyncio import client as k8s

        api_client = k8s.ApiClient()
        try:
            service = k8s.V1Service(
                metadata=k8s.V1ObjectMeta(name="old-api", namespace="legacy", owner_references=None),
                spec=k8s.V1ServiceSpec(type="LoadBalancer", selector={"app": "old-api"}),
            )

            class FakeCore:
                def __init__(self, api_client=None):
                    pass

                async def list_service_for_all_namespaces(self, **kwargs):
                    return k8s.V1ServiceList(items=[copy.deepcopy(service)], metadata=k8s.V1ListMeta())

            monkeypatch.setattr(k8s, "CoreV1Api", FakeCore)
            result = await KubernetesObjectReader(api_client=api_client).list("services")
        finally:
            await api_client.close()
        assert result.items == [
            {
                "metadata": {"name": "old-api", "namespace": "legacy"},
                "spec": {"selector": {"app": "old-api"}, "type": "LoadBalancer"},
            }
        ]


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# enrich_orphaned_lb_costs
# ---------------------------------------------------------------------------


class TestEnrichOrphanedLbCosts:
    """Tests for the OpenCost cost enrichment of orphaned LoadBalancers."""

    @pytest.mark.asyncio
    async def test_annualizes_opencost_window_costs(self):
        """Window costs should be annualized against the observation window."""
        from greenkube.collectors.opencost_collector import AssetCost, OpenCostCollector

        services = [
            OrphanedLoadBalancer(name="lb-aaa", namespace="default", endpoint_count=0),
            OrphanedLoadBalancer(name="lb-bbb", namespace="default", endpoint_count=0),
        ]
        mock_opencost = AsyncMock(spec=OpenCostCollector)
        mock_opencost.collect_lb_cost_details = AsyncMock(return_value={"default/lb-aaa": AssetCost(2.5)})

        result = await enrich_orphaned_lb_costs(services, window_days=7, opencost=mock_opencost)

        # 2.5 USD over 7 days → 2.5 * 365/7 per year
        assert result[0].annual_cost == pytest.approx(2.5 * 365 / 7)
        assert result[1].annual_cost is None
        mock_opencost.collect_lb_cost_details.assert_awaited_once_with(
            ["default/lb-aaa", "default/lb-bbb"], window_days=7
        )

    @pytest.mark.asyncio
    async def test_keeps_services_unchanged_without_opencost_data(self):
        """Services without OpenCost data should keep annual_cost=None."""
        from greenkube.collectors.opencost_collector import OpenCostCollector

        services = [OrphanedLoadBalancer(name="lb-aaa", namespace="default", endpoint_count=0)]
        mock_opencost = AsyncMock(spec=OpenCostCollector)
        mock_opencost.collect_lb_cost_details = AsyncMock(return_value={})

        result = await enrich_orphaned_lb_costs(services, window_days=7, opencost=mock_opencost)

        assert result[0].annual_cost is None

    @pytest.mark.asyncio
    async def test_handles_opencost_failure_gracefully(self):
        """An OpenCost failure should leave services untouched without raising."""
        from greenkube.collectors.opencost_collector import OpenCostCollector

        services = [OrphanedLoadBalancer(name="lb-aaa", namespace="default", endpoint_count=0)]
        mock_opencost = AsyncMock(spec=OpenCostCollector)
        mock_opencost.collect_lb_cost_details = AsyncMock(side_effect=RuntimeError("boom"))

        result = await enrich_orphaned_lb_costs(services, window_days=7, opencost=mock_opencost)

        assert result is services
        assert result[0].annual_cost is None

    @pytest.mark.asyncio
    async def test_skips_opencost_for_empty_services(self):
        """No services means no OpenCost query."""
        from greenkube.collectors.opencost_collector import OpenCostCollector

        mock_opencost = AsyncMock(spec=OpenCostCollector)

        result = await enrich_orphaned_lb_costs([], opencost=mock_opencost)

        assert result == []
        mock_opencost.collect_lb_cost_details.assert_not_awaited()
