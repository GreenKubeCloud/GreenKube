# src/greenkube/collectors/k8s_object_reader.py
"""Read-only Kubernetes object listing for deletion evidence.

Deletion recommendations must prove that a resource is unused, so every read
distinguishes three outcomes (principle: fail closed and explain):

* ``ok`` — the objects were listed;
* ``absent`` — the API is not served by the cluster (HTTP 404, for example the
  Gateway API CRDs are not installed): no such object can exist, which is
  itself evidence;
* ``unreadable`` — the read failed (HTTP 403 because the ClusterRole lacks the
  permission, a timeout, a server error). Nothing can be concluded from it,
  so callers must not recommend a deletion that depends on it.

Objects are returned as plain dictionaries in the Kubernetes JSON shape
(camelCase keys, exactly what ``kubectl get -o json`` prints), so the
evidence rules are pure functions that tests feed with real manifests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

from greenkube.core.config import config as global_config

logger = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_ABSENT = "absent"
STATUS_UNREADABLE = "unreadable"

#: Page size of list calls (bounded memory per call).
PAGE_SIZE = 500


@dataclass
class ObjectList:
    """Result of one list call."""

    kind: str
    items: List[Dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_OK
    detail: str = ""

    @property
    def known(self) -> bool:
        """True when the result proves what exists (listed, or the API is not served)."""
        return self.status in (STATUS_OK, STATUS_ABSENT)


#: Custom resources read through the CustomObjects API: kind -> [(group, version, plural)], newest first.
CUSTOM_KINDS: Dict[str, List[Tuple[str, str, str]]] = {
    "gateways": [
        ("gateway.networking.k8s.io", "v1", "gateways"),
        ("gateway.networking.k8s.io", "v1beta1", "gateways"),
    ],
    "httproutes": [
        ("gateway.networking.k8s.io", "v1", "httproutes"),
        ("gateway.networking.k8s.io", "v1beta1", "httproutes"),
    ],
    "grpcroutes": [("gateway.networking.k8s.io", "v1", "grpcroutes")],
    "tlsroutes": [("gateway.networking.k8s.io", "v1alpha2", "tlsroutes")],
    "tcproutes": [("gateway.networking.k8s.io", "v1alpha2", "tcproutes")],
    "udproutes": [("gateway.networking.k8s.io", "v1alpha2", "udproutes")],
    "volumesnapshots": [("snapshot.storage.k8s.io", "v1", "volumesnapshots")],
    "volumesnapshotcontents": [("snapshot.storage.k8s.io", "v1", "volumesnapshotcontents")],
}

#: Built-in kinds: kind -> (API class name, all-namespaces method, namespaced method or None).
BUILTIN_KINDS: Dict[str, Tuple[str, str, Optional[str]]] = {
    "services": ("CoreV1Api", "list_service_for_all_namespaces", "list_namespaced_service"),
    "endpoints": ("CoreV1Api", "list_endpoints_for_all_namespaces", "list_namespaced_endpoints"),
    "pods": ("CoreV1Api", "list_pod_for_all_namespaces", "list_namespaced_pod"),
    "namespaces": ("CoreV1Api", "list_namespace", None),
    "persistentvolumes": ("CoreV1Api", "list_persistent_volume", None),
    "persistentvolumeclaims": (
        "CoreV1Api",
        "list_persistent_volume_claim_for_all_namespaces",
        "list_namespaced_persistent_volume_claim",
    ),
    "endpointslices": (
        "DiscoveryV1Api",
        "list_endpoint_slice_for_all_namespaces",
        "list_namespaced_endpoint_slice",
    ),
    "ingresses": ("NetworkingV1Api", "list_ingress_for_all_namespaces", "list_namespaced_ingress"),
    "deployments": ("AppsV1Api", "list_deployment_for_all_namespaces", "list_namespaced_deployment"),
    "statefulsets": ("AppsV1Api", "list_stateful_set_for_all_namespaces", "list_namespaced_stateful_set"),
    "daemonsets": ("AppsV1Api", "list_daemon_set_for_all_namespaces", "list_namespaced_daemon_set"),
    "cronjobs": ("BatchV1Api", "list_cron_job_for_all_namespaces", "list_namespaced_cron_job"),
}


def label_selector(selector: Optional[Mapping[str, str]]) -> str:
    """Equality-based label selector string for a Service selector."""
    return ",".join(f"{key}={value}" for key, value in sorted((selector or {}).items()))


def _http_status(exc: BaseException) -> Optional[int]:
    status = getattr(exc, "status", None)
    return status if isinstance(status, int) else None


class KubernetesObjectReader:
    """Lists Kubernetes objects as JSON dictionaries, classifying failures.

    Results are cached per ``(kind, namespace, selector)`` for the lifetime of
    the reader (one scan), so several evidence rules share one API call.
    """

    def __init__(self, api_client: Any = None, request_timeout: Optional[int] = None):
        self._api_client = api_client
        self._timeout = request_timeout if request_timeout is not None else global_config.K8S_REQUEST_TIMEOUT
        self._cache: Dict[Tuple[str, Optional[str], str], ObjectList] = {}

    async def _client(self) -> Any:
        if self._api_client is None:
            from greenkube.core.k8s_client import get_core_v1_api

            core = await get_core_v1_api()
            if core is None:
                return None
            self._api_client = getattr(core, "api_client", None)
        return self._api_client

    async def list(self, kind: str, namespace: Optional[str] = None, selector: str = "") -> ObjectList:
        key = (kind, namespace, selector)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        result = await self._list(kind, namespace, selector)
        if result.status == STATUS_UNREADABLE:
            logger.info("Deletion evidence: cannot read %s (%s).", kind, result.detail)
        self._cache[key] = result
        return result

    async def _list(self, kind: str, namespace: Optional[str], selector: str) -> ObjectList:
        try:
            api_client = await self._client()
        except Exception as exc:  # configuration errors: nothing can be proven
            return ObjectList(kind, status=STATUS_UNREADABLE, detail=str(exc)[:200])
        if api_client is None:
            return ObjectList(kind, status=STATUS_UNREADABLE, detail="Kubernetes API not configured")
        if kind in CUSTOM_KINDS:
            return await self._list_custom(api_client, kind, namespace, selector)
        if kind not in BUILTIN_KINDS:
            raise ValueError(f"Unsupported kind {kind!r}")
        from kubernetes_asyncio import client as k8s

        api_name, all_method, namespaced_method = BUILTIN_KINDS[kind]
        api = getattr(k8s, api_name)(api_client=api_client)
        kwargs: Dict[str, Any] = {}
        if namespace is not None and namespaced_method:
            method = getattr(api, namespaced_method)
            args: Tuple[Any, ...] = (namespace,)
        else:
            method = getattr(api, all_method)
            args = ()
        if selector:
            kwargs["label_selector"] = selector

        async def fetch(cont: Optional[str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
            response = await method(*args, **self._paging(cont, kwargs))
            items = [api_client.sanitize_for_serialization(item) for item in (response.items or [])]
            return items, getattr(response.metadata, "_continue", None) if response.metadata else None

        return await self._paginate(kind, fetch)

    async def _list_custom(self, api_client: Any, kind: str, namespace: Optional[str], selector: str) -> ObjectList:
        from kubernetes_asyncio import client as k8s

        api = k8s.CustomObjectsApi(api_client=api_client)
        last: Optional[ObjectList] = None
        for group, version, plural in CUSTOM_KINDS[kind]:
            kwargs: Dict[str, Any] = {"label_selector": selector} if selector else {}

            async def fetch(
                cont: Optional[str], group=group, version=version, plural=plural, kwargs=kwargs
            ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
                if namespace is not None:
                    response = await api.list_namespaced_custom_object(
                        group, version, namespace, plural, **self._paging(cont, kwargs)
                    )
                else:
                    response = await api.list_cluster_custom_object(
                        group, version, plural, **self._paging(cont, kwargs)
                    )
                response = response if isinstance(response, Mapping) else {}
                metadata = response.get("metadata") or {}
                return list(response.get("items") or []), metadata.get("continue") or None

            result = await self._paginate(kind, fetch)
            if result.status != STATUS_ABSENT:
                return result
            last = result  # this version is not served; try an older one
        return last or ObjectList(kind, status=STATUS_ABSENT)

    def _paging(self, cont: Optional[str], extra: Mapping[str, Any]) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": PAGE_SIZE, "_request_timeout": self._timeout or None, **extra}
        if cont:
            params["_continue"] = cont
        return params

    async def _paginate(
        self, kind: str, fetch: Callable[[Optional[str]], Awaitable[Tuple[List[Dict[str, Any]], Optional[str]]]]
    ) -> ObjectList:
        items: List[Dict[str, Any]] = []
        cont: Optional[str] = None
        try:
            while True:
                page, cont = await fetch(cont)
                items.extend(page)
                if not cont:
                    return ObjectList(kind, items=items)
        except Exception as exc:
            status = _http_status(exc)
            if status == 404:
                return ObjectList(kind, status=STATUS_ABSENT, detail="API not served by the cluster")
            detail = f"HTTP {status}" if status else type(exc).__name__
            if status == 403:
                detail = "HTTP 403: the GreenKube ClusterRole cannot list it"
            return ObjectList(kind, status=STATUS_UNREADABLE, detail=detail)


class StaticObjectReader:
    """In-memory reader over JSON manifests (tests, offline evaluation).

    ``objects`` maps a kind to its manifests; ``statuses`` forces a kind to be
    ``absent`` or ``unreadable``. Label selectors are equality-based.
    """

    def __init__(
        self,
        objects: Optional[Mapping[str, List[Dict[str, Any]]]] = None,
        statuses: Optional[Mapping[str, str]] = None,
    ):
        self.objects = {kind: list(items) for kind, items in (objects or {}).items()}
        self.statuses = dict(statuses or {})
        self.calls: List[Tuple[str, Optional[str], str]] = []

    async def list(self, kind: str, namespace: Optional[str] = None, selector: str = "") -> ObjectList:
        self.calls.append((kind, namespace, selector))
        status = self.statuses.get(kind, STATUS_OK)
        if status != STATUS_OK:
            return ObjectList(kind, status=status, detail=f"{kind} {status}")
        wanted = dict(part.split("=", 1) for part in selector.split(",") if "=" in part) if selector else {}
        items = []
        for item in self.objects.get(kind, []):
            metadata = item.get("metadata") or {}
            if namespace is not None and metadata.get("namespace") != namespace:
                continue
            labels = metadata.get("labels") or {}
            if any(labels.get(key) != value for key, value in wanted.items()):
                continue
            items.append(item)
        return ObjectList(kind, items=items)
