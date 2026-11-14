# src/greenkube/collectors/lb_collector.py
"""
Finds LoadBalancer Services that appear unused, with the evidence.

A LoadBalancer Service is reported only when GreenKube can prove, from the
Kubernetes API, that nothing uses it (roadmap Appendix A, user-reported bug
"orphaned LoadBalancer that was the cluster's Cilium Gateway"). Empty legacy
``Endpoints`` are not proof: Gateway API implementations (Cilium, Istio,
Envoy Gateway, Contour, Kong...) route traffic to selector-less Services
whose backends are never listed as endpoints, and dual-stack or large
services publish their backends in EndpointSlices only.

A Service is never reported when it:

* is opted out (``greenkube.io/ignore: "true"`` or ``greenkube.io/keep``);
* has no selector (its endpoints are managed by something else);
* has owner references (a Gateway, an operator or a mesh manages it);
* carries the labels or annotations of a gateway, ingress or mesh controller;
* has any endpoint, ready or not, in its legacy Endpoints or its EndpointSlices;
* selects any pod, or is selected by the pod template of a Deployment,
  StatefulSet, DaemonSet or CronJob (a workload scaled to zero comes back);
* is referenced by a Gateway or Ingress address, an Ingress backend or a
  Gateway API route (backendRefs or parentRefs);
* depends on an API that could not be read (RBAC, timeout): unproven means
  not reported.

The recommendation is emitted only once the condition has persisted for
``DELETION_MIN_UNUSED_DAYS`` (first-seen persistence,
``greenkube.collectors.deletion_candidates``).

Addresses handed out by a bare-metal allocator (MetalLB, Cilium LB-IPAM,
kube-vip) carry no cloud load-balancer bill: :func:`bare_metal_allocator`
names the allocator so the analyzer reports no cost saving for them.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from greenkube.collectors.k8s_object_reader import KubernetesObjectReader, ObjectList, label_selector
from greenkube.collectors.opencost_collector import OpenCostCollector
from greenkube.core.deletion_safety import opt_out_reason

logger = logging.getLogger(__name__)

#: Label and annotation key prefixes of gateway, ingress and service-mesh controllers.
CONTROLLER_KEY_PREFIXES: Tuple[str, ...] = (
    "gateway.networking.k8s.io/",
    "io.cilium.gateway/",
    "io.cilium/",
    "cilium.io/",
    "ingress.cilium.io/",
    "gateway.istio.io/",
    "istio.io/",
    "service.istio.io/",
    "networking.istio.io/",
    "install.operator.istio.io/",
    "operator.istio.io/",
    "gateway.envoyproxy.io/",
    "projectcontour.io/",
    "konghq.com/",
    "configuration.konghq.com/",
    "gateway-operator.konghq.com/",
    "traefik.io/",
    "traefik.ingress.kubernetes.io/",
    "nginx.org/",
    "linkerd.io/",
    "kuma.io/",
    "consul.hashicorp.com/",
    "gateway.solo.io/",
    "gloo.solo.io/",
    "haproxy.org/",
)

#: Exact label keys of controller-managed gateway Services (``istio: ingressgateway``).
CONTROLLER_LABEL_KEYS = frozenset({"istio", "gateway-name", "gateway.envoyproxy.io/owning-gateway-name"})

#: Label values of ``app``/``app.kubernetes.io/component``/``app.kubernetes.io/name`` that mark a gateway.
_GATEWAY_LABEL_VALUES = frozenset(
    {
        "gateway",
        "ingress-gateway",
        "ingressgateway",
        "egressgateway",
        "istio-ingressgateway",
        "istio-egressgateway",
        "envoy-gateway",
        "cilium-gateway",
    }
)
_GATEWAY_LABEL_KEYS = ("app", "app.kubernetes.io/component", "app.kubernetes.io/name")

#: Bare-metal LoadBalancer allocators: (allocator, label/annotation key prefixes, loadBalancerClass fragments).
#: They assign addresses from a local pool, so a LoadBalancer Service has no cloud bill.
BARE_METAL_ALLOCATORS: Tuple[Tuple[str, Tuple[str, ...], Tuple[str, ...]], ...] = (
    ("MetalLB", ("metallb.universe.tf/", "metallb.io/"), ("metallb",)),
    ("Cilium LB-IPAM", ("lbipam.cilium.io/", "io.cilium/lb-ipam"), ("io.cilium/", "cilium.io/")),
    ("kube-vip", ("kube-vip.io/",), ("kube-vip",)),
)
#: Service status conditions that Cilium LB-IPAM sets on the Services it serves.
_CILIUM_LB_IPAM_CONDITIONS = frozenset({"cilium.io/IPAMRequestSatisfied", "io.cilium/IPAMRequestSatisfied"})

#: Workload kinds whose pod template can match a Service selector while scaled to zero.
_WORKLOAD_KINDS: Tuple[Tuple[str, str], ...] = (
    ("deployments", "Deployment"),
    ("statefulsets", "StatefulSet"),
    ("daemonsets", "DaemonSet"),
    ("cronjobs", "CronJob"),
)

#: Outcomes of an orphan check (:class:`OrphanCheck`).
ORPHAN = "orphan"
ORPHAN_IN_USE = "in_use"
ORPHAN_UNKNOWN = "unknown"
ORPHAN_ABSENT = "absent"

#: Gateway API route kinds whose backendRefs or parentRefs may name a Service.
ROUTE_KINDS: Tuple[str, ...] = ("httproutes", "grpcroutes", "tlsroutes", "tcproutes", "udproutes")


@dataclass
class OrphanedLoadBalancer:
    """A LoadBalancer Service that appears unused, with the evidence."""

    name: str
    namespace: str
    endpoint_count: int = 0
    external_ip: Optional[str] = None
    ports: str = ""
    annual_cost: Optional[float] = None
    uid: str = ""
    selector: Dict[str, str] = field(default_factory=dict)
    #: Facts that show the Service is unused (shown in the recommendation).
    evidence: List[str] = field(default_factory=list)
    #: When GreenKube first saw it unused (None: not persisted, never recommended).
    first_seen_at: Optional[datetime] = None
    #: Bare-metal allocator of its address (MetalLB, Cilium LB-IPAM, kube-vip): no cloud bill.
    bare_metal_allocator: Optional[str] = None


@dataclass
class LoadBalancerScan:
    """Outcome of one LoadBalancer evaluation."""

    candidates: List[OrphanedLoadBalancer] = field(default_factory=list)
    #: ``namespace/name`` -> why the Service is not a candidate.
    skipped: Dict[str, str] = field(default_factory=dict)
    #: True when every LoadBalancer Service was evaluated (Services could be listed).
    complete: bool = False


def _metadata(obj: Mapping[str, Any]) -> Mapping[str, Any]:
    return obj.get("metadata") or {}


def _key(namespace: str, name: str) -> str:
    return f"{namespace}/{name}"


def controller_marker(metadata: Mapping[str, Any]) -> Optional[str]:
    """The label or annotation that shows a gateway, ingress or mesh controller manages the object."""
    labels = metadata.get("labels") or {}
    annotations = metadata.get("annotations") or {}
    for source, values in (("label", labels), ("annotation", annotations)):
        for key in values:
            if key in CONTROLLER_LABEL_KEYS or key.startswith(CONTROLLER_KEY_PREFIXES):
                return f"{source} {key}"
    for key in _GATEWAY_LABEL_KEYS:
        value = str(labels.get(key, "")).lower()
        if value in _GATEWAY_LABEL_VALUES:
            return f"label {key}={labels[key]}"
    return None


def bare_metal_allocator(service: Mapping[str, Any]) -> Optional[str]:
    """The bare-metal allocator (MetalLB, Cilium LB-IPAM, kube-vip) of a LoadBalancer Service, if any.

    Read from its ``loadBalancerClass``, the labels and annotations the allocator
    writes (``metallb.io/ip-allocated-from-pool``, ``kube-vip.io/vipHost``...) and
    Cilium's ``IPAMRequestSatisfied`` status condition.
    """
    metadata = _metadata(service)
    spec = service.get("spec") or {}
    load_balancer_class = str(spec.get("loadBalancerClass") or "").lower()
    keys = [*(metadata.get("labels") or {}), *(metadata.get("annotations") or {})]
    labels = metadata.get("labels") or {}
    for allocator, prefixes, classes in BARE_METAL_ALLOCATORS:
        if load_balancer_class and any(fragment in load_balancer_class for fragment in classes):
            return allocator
        if any(str(key).startswith(prefixes) for key in keys):
            return allocator
    if str(labels.get("implementation", "")).lower() == "kube-vip":
        return "kube-vip"
    conditions = (service.get("status") or {}).get("conditions") or []
    if any(isinstance(item, Mapping) and item.get("type") in _CILIUM_LB_IPAM_CONDITIONS for item in conditions):
        return "Cilium LB-IPAM"
    return None


def static_exclusion(service: Mapping[str, Any]) -> Optional[str]:
    """Why a LoadBalancer Service is never a deletion candidate, from its own manifest."""
    metadata = _metadata(service)
    spec = service.get("spec") or {}
    opt_out = opt_out_reason(metadata)
    if opt_out:
        return f"opted out ({opt_out})"
    if not spec.get("selector"):
        return "no selector: its endpoints are managed outside Kubernetes Services (gateway, mesh or manual)"
    owners = metadata.get("ownerReferences") or []
    if owners:
        owner = owners[0]
        return f"owned by {owner.get('kind', 'an object')} {owner.get('name', '')}".rstrip()
    marker = controller_marker(metadata)
    if marker:
        return f"managed by a gateway, ingress or mesh controller ({marker})"
    return None


def external_addresses(service: Mapping[str, Any]) -> List[str]:
    ingress = ((service.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
    addresses = [entry.get("ip") or entry.get("hostname") for entry in ingress if isinstance(entry, Mapping)]
    spec = service.get("spec") or {}
    addresses.extend(spec.get("externalIPs") or [])
    if spec.get("loadBalancerIP"):
        addresses.append(spec["loadBalancerIP"])
    return [address for address in addresses if address]


def _legacy_endpoint_count(endpoints: Optional[Mapping[str, Any]]) -> int:
    if not endpoints:
        return 0
    count = 0
    for subset in endpoints.get("subsets") or []:
        count += len(subset.get("addresses") or []) + len(subset.get("notReadyAddresses") or [])
    return count


def _slice_endpoint_count(slices: Iterable[Mapping[str, Any]]) -> int:
    return sum(len(item.get("endpoints") or []) for item in slices)


def _template_labels(kind: str, workload: Mapping[str, Any]) -> Mapping[str, Any]:
    spec = workload.get("spec") or {}
    if kind == "cronjobs":
        spec = ((spec.get("jobTemplate") or {}).get("spec")) or {}
    return ((spec.get("template") or {}).get("metadata") or {}).get("labels") or {}


def _selects(selector: Mapping[str, str], labels: Mapping[str, Any]) -> bool:
    return bool(selector) and all(labels.get(key) == value for key, value in selector.items())


def _refers_to_service(ref: Mapping[str, Any], namespace: str, name: str, default_namespace: str) -> bool:
    kind = ref.get("kind") or "Service"
    group = ref.get("group") or ""
    return (
        kind == "Service"
        and group in ("", "core")
        and ref.get("name") == name
        and (ref.get("namespace") or default_namespace) == namespace
    )


def gateway_reference(gateways: Iterable[Mapping[str, Any]], addresses: List[str]) -> Optional[str]:
    """A Gateway whose spec or status addresses include one of the Service's external addresses."""
    if not addresses:
        return None
    wanted = set(addresses)
    for gateway in gateways:
        metadata = _metadata(gateway)
        values = [
            entry.get("value")
            for part in ((gateway.get("spec") or {}).get("addresses"), (gateway.get("status") or {}).get("addresses"))
            for entry in (part or [])
            if isinstance(entry, Mapping)
        ]
        if wanted.intersection(value for value in values if value):
            return f"Gateway {metadata.get('namespace')}/{metadata.get('name')} uses its address"
    return None


def ingress_reference(
    ingresses: Iterable[Mapping[str, Any]], namespace: str, name: str, addresses: List[str]
) -> Optional[str]:
    """An Ingress that routes to the Service or publishes its address."""
    wanted = set(addresses)
    for ingress in ingresses:
        metadata = _metadata(ingress)
        label = f"Ingress {metadata.get('namespace')}/{metadata.get('name')}"
        status = ((ingress.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
        published = {entry.get("ip") or entry.get("hostname") for entry in status if isinstance(entry, Mapping)}
        if wanted and wanted.intersection(published):
            return f"{label} publishes its address"
        if metadata.get("namespace") != namespace:
            continue
        spec = ingress.get("spec") or {}
        backends = [spec.get("defaultBackend") or {}]
        for rule in spec.get("rules") or []:
            backends.extend(path.get("backend") or {} for path in ((rule.get("http") or {}).get("paths") or []))
        for backend in backends:
            if ((backend.get("service") or {}).get("name")) == name:
                return f"{label} routes to it"
    return None


def route_reference(routes: Iterable[Mapping[str, Any]], namespace: str, name: str) -> Optional[str]:
    """A Gateway API route whose backendRefs or parentRefs name the Service."""
    for route in routes:
        metadata = _metadata(route)
        route_namespace = metadata.get("namespace") or ""
        spec = route.get("spec") or {}
        refs: List[Mapping[str, Any]] = list(spec.get("parentRefs") or [])
        for rule in spec.get("rules") or []:
            refs.extend(rule.get("backendRefs") or [])
        if any(isinstance(ref, Mapping) and _refers_to_service(ref, namespace, name, route_namespace) for ref in refs):
            return f"{route.get('kind') or 'Route'} {route_namespace}/{metadata.get('name')} references it"
    return None


class LoadBalancerCollector:
    """Evaluates every LoadBalancer Service and returns those that appear unused."""

    def __init__(self, reader: Any = None):
        self.reader = reader

    async def collect(self) -> Optional[List[OrphanedLoadBalancer]]:
        """Candidates of one complete evaluation, or None when nothing could be proven (any error)."""
        scan = await self.scan()
        return scan.candidates if scan.complete else None

    async def scan(self) -> LoadBalancerScan:
        result = LoadBalancerScan()
        reader = self.reader or KubernetesObjectReader()
        try:
            services = await reader.list("services")
            if not services.known:
                logger.warning(
                    "Cannot list Services (%s); no LoadBalancer cleanup recommendations this scan.", services.detail
                )
                return result
            result.complete = True
            pending: List[Mapping[str, Any]] = []
            for service in services.items:
                if (service.get("spec") or {}).get("type") != "LoadBalancer":
                    continue
                metadata = _metadata(service)
                key = _key(metadata.get("namespace") or "", metadata.get("name") or "")
                reason = static_exclusion(service)
                if reason:
                    result.skipped[key] = reason
                else:
                    pending.append(service)
            for service in pending:
                candidate, reason = await self._evaluate(reader, service)
                if candidate is not None:
                    result.candidates.append(candidate)
                else:
                    metadata = _metadata(service)
                    result.skipped[_key(metadata.get("namespace") or "", metadata.get("name") or "")] = reason
            for key, reason in sorted(result.skipped.items()):
                logger.debug("LoadBalancer Service %s is not a cleanup candidate: %s", key, reason)
            logger.info(
                "Evaluated LoadBalancer Services: %d appear unused, %d in use or unproven.",
                len(result.candidates),
                len(result.skipped),
            )
        except Exception as e:
            logger.warning(
                "Failed to evaluate LoadBalancer Services: %s. Continuing without LB cleanup recommendations.", e
            )
            return LoadBalancerScan()
        return result

    async def _evaluate(self, reader: Any, service: Mapping[str, Any]) -> Tuple[Optional[OrphanedLoadBalancer], str]:
        candidate, reason, _status = await evaluate_load_balancer_usage(reader, service)
        return candidate, reason


async def evaluate_load_balancer_usage(
    reader: Any, service: Mapping[str, Any]
) -> Tuple[Optional[OrphanedLoadBalancer], str, str]:
    """The usage checks of one LoadBalancer Service past its static exclusions.

    Returns the candidate (or ``None``), the reason it is not one, and the outcome:
    ``orphan``, ``in_use`` or ``unknown`` (an API it depends on could not be read).
    """
    metadata = _metadata(service)
    spec = service.get("spec") or {}
    namespace = metadata.get("namespace") or ""
    name = metadata.get("name") or ""
    selector = {str(k): str(v) for k, v in (spec.get("selector") or {}).items()}
    addresses = external_addresses(service)

    def unproven(listing: ObjectList) -> str:
        return f"cannot prove it is unused: {listing.kind} unreadable ({listing.detail})"

    endpoints = await reader.list("endpoints", namespace)
    if not endpoints.known:
        return None, unproven(endpoints), ORPHAN_UNKNOWN
    legacy = next((item for item in endpoints.items if _metadata(item).get("name") == name), None)
    legacy_count = _legacy_endpoint_count(legacy)
    if legacy_count:
        return None, f"{legacy_count} endpoint(s) in its Endpoints object", ORPHAN_IN_USE

    slices = await reader.list("endpointslices", namespace, f"kubernetes.io/service-name={name}")
    if slices.status != "ok":
        return None, unproven(slices), ORPHAN_UNKNOWN
    slice_count = _slice_endpoint_count(slices.items)
    if slice_count:
        return None, f"{slice_count} endpoint(s) in {len(slices.items)} EndpointSlice(s)", ORPHAN_IN_USE

    pods = await reader.list("pods", namespace, label_selector(selector))
    if not pods.known:
        return None, unproven(pods), ORPHAN_UNKNOWN
    if pods.items:
        return None, f"its selector matches {len(pods.items)} pod(s)", ORPHAN_IN_USE

    for kind, label in _WORKLOAD_KINDS:
        workloads = await reader.list(kind, namespace)
        if not workloads.known:
            return None, unproven(workloads), ORPHAN_UNKNOWN
        for workload in workloads.items:
            if _selects(selector, _template_labels(kind, workload)):
                return (
                    None,
                    f"{label} {_metadata(workload).get('name')} selects it (scaled to zero or between runs)",
                    ORPHAN_IN_USE,
                )

    gateways = await reader.list("gateways")
    if not gateways.known:
        return None, unproven(gateways), ORPHAN_UNKNOWN
    reference = gateway_reference(gateways.items, addresses)
    if reference:
        return None, reference, ORPHAN_IN_USE
    ingresses = await reader.list("ingresses")
    if not ingresses.known:
        return None, unproven(ingresses), ORPHAN_UNKNOWN
    reference = ingress_reference(ingresses.items, namespace, name, addresses)
    if reference:
        return None, reference, ORPHAN_IN_USE
    for kind in ROUTE_KINDS:
        routes = await reader.list(kind)
        if not routes.known:
            return None, unproven(routes), ORPHAN_UNKNOWN
        reference = route_reference(routes.items, namespace, name)
        if reference:
            return None, reference, ORPHAN_IN_USE

    selector_text = label_selector(selector)
    evidence = [
        f"selector {selector_text} matches no pod in namespace {namespace}",
        "no Deployment, StatefulSet, DaemonSet or CronJob pod template matches the selector",
        "0 endpoints (ready or not) in its Endpoints object and EndpointSlices",
        "no owner references and no gateway, ingress or mesh controller labels or annotations",
        "no Gateway, Ingress or Gateway API route references it or uses its address",
    ]
    ports = ",".join(
        f"{port.get('port')}/{port.get('protocol') or 'TCP'}"
        for port in spec.get("ports") or []
        if isinstance(port, Mapping) and port.get("port")
    )
    return (
        OrphanedLoadBalancer(
            name=name,
            namespace=namespace,
            endpoint_count=0,
            external_ip=addresses[0] if addresses else None,
            ports=ports,
            uid=metadata.get("uid") or "",
            selector=selector,
            evidence=evidence,
            bare_metal_allocator=bare_metal_allocator(service),
        ),
        "",
        ORPHAN,
    )


@dataclass(frozen=True)
class OrphanCheck:
    """Whether a resource proposed for deletion is still unused, with the reason.

    ``status`` is ``orphan`` (every check passed), ``in_use`` (something uses it, or it
    is no longer a deletion candidate), ``unknown`` (an API a check depends on could not
    be read: never read as unused) or ``absent`` (the resource no longer exists).
    """

    status: str
    reason: str = ""

    @property
    def orphan(self) -> bool:
        return self.status == ORPHAN


async def check_load_balancer(reader: Any, service: Mapping[str, Any]) -> OrphanCheck:
    """The full orphan check of one live Service, as the collector runs it (H6 re-check).

    A Service that is no longer of type ``LoadBalancer``, is opted out, lost its selector,
    gained an owner or a gateway/mesh label, has endpoints (Endpoints or EndpointSlices),
    is selected by a pod or a workload template, or is referenced by a Gateway, an
    Ingress or a Gateway API route is ``in_use``; an unreadable dependency is ``unknown``.
    """
    spec = service.get("spec") or {}
    if spec.get("type") != "LoadBalancer":
        return OrphanCheck(ORPHAN_IN_USE, f"is no longer a LoadBalancer (live type {spec.get('type') or 'ClusterIP'})")
    reason = static_exclusion(service)
    if reason:
        return OrphanCheck(ORPHAN_IN_USE, reason)
    _candidate, reason, status = await evaluate_load_balancer_usage(reader, service)
    return OrphanCheck(status, reason)


async def enrich_orphaned_lb_costs(
    services: List[OrphanedLoadBalancer],
    window_days: int = 7,
    opencost: Optional[OpenCostCollector] = None,
) -> List[OrphanedLoadBalancer]:
    """Attach real annualized OpenCost LoadBalancer costs when available.

    Services that OpenCost has no cost data for keep ``annual_cost=None`` so
    the recommender falls back to the flat LOAD_BALANCER_COST_PER_MONTH estimate.

    Args:
        services: The LoadBalancer descriptors to enrich (mutated in place).
        window_days: Observation window in days used for the OpenCost query;
                     the reported window cost is annualized against it.
        opencost: Optional OpenCostCollector instance (dependency injection for tests).

    Returns:
        The same list of services, enriched in place.
    """
    if not services:
        return services

    collector = opencost or OpenCostCollector()
    try:
        costs = await collector.collect_lb_costs([f"{s.namespace}/{s.name}" for s in services], window_days=window_days)
        enriched_count = 0
        if costs:
            for service in services:
                window_cost = costs.get(f"{service.namespace}/{service.name}")
                if window_cost:
                    service.annual_cost = window_cost * (365.0 / window_days)
                    enriched_count += 1
        if enriched_count:
            logger.info(
                "Attached real OpenCost costs to %d of %d unused LoadBalancer Services.",
                enriched_count,
                len(services),
            )
        else:
            logger.debug("OpenCost has no cost data for the unused LoadBalancer Services; using estimates.")
    except Exception as e:
        logger.warning("Could not enrich LoadBalancer costs from OpenCost: %s. Using estimates.", e)
    finally:
        if opencost is None:
            await collector.close()

    return services
