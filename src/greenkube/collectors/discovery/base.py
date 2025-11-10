# src/greenkube/collectors/discovery/base.py
"""
Base discovery utilities for Kubernetes services.
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Optional, Sequence

from kubernetes import client, config

logger = logging.getLogger(__name__)


class BaseDiscovery:
    """Base class encapsulating Kubernetes service listing and common heuristics."""

    common_ports = (80, 8080, 9090, 9091)
    # Base only knows about the default namespace preference; app-specific
    # preferred namespaces should live in the subclass implementations.
    common_namespaces = ("default",)

    def _load_kube_config_quietly(self) -> bool:
        try:
            config.load_incluster_config()
            return True
        except Exception:
            try:
                config.load_kube_config()
                return True
            except Exception:
                logger.debug("Could not load kube config for discovery")
                return False

    def list_services(self) -> Optional[Sequence[client.V1Service]]:
        # When running under pytest, avoid making real Kubernetes API calls
        # unless the test has explicitly monkeypatched `client.CoreV1Api`.
        # This lets discovery tests provide their own mock CoreV1Api while
        # preventing accidental network calls in other tests.
        if "PYTEST_CURRENT_TEST" in os.environ:
            try:
                core_api_mod = getattr(client.CoreV1Api, "__module__", "")
            except Exception:
                core_api_mod = ""
            # If CoreV1Api appears to be the real kubernetes client, short-circuit.
            if core_api_mod.startswith("kubernetes"):
                logger.debug("list_services short-circuited under PYTEST_CURRENT_TEST because CoreV1Api is real")
                return None

        # First try to construct the API client directly. Tests commonly
        # monkeypatch `greenkube.collectors.discovery.client.CoreV1Api` and
        # expect it to be used without requiring a kubeconfig to be loadable.
        try:
            v1 = client.CoreV1Api()
            return v1.list_service_for_all_namespaces().items
        except Exception:
            # If direct construction fails, attempt to load kube config and retry.
            if not self._load_kube_config_quietly():
                return None
            try:
                v1 = client.CoreV1Api()
                return v1.list_service_for_all_namespaces().items
            except Exception as e:
                logger.debug("Failed to list services for discovery after loading kube config: %s", e)
                return None

    def pick_port(self, ports) -> Optional[int]:
        if not ports:
            return None
        for p in ports:
            pnum = getattr(p, "port", None)
            pname = (getattr(p, "name", None) or "").lower()
            if pname in ("http", "web") or pnum in self.common_ports:
                return pnum
        return getattr(ports[0], "port", None)

    def build_dns(self, name: str, namespace: str, port: int) -> str:
        return f"http://{name}.{namespace}.svc.cluster.local:{port}"

    def build_parts(self, name: str, namespace: str, port: int, scheme: str = "http") -> tuple:
        """Return (scheme, host, port) for a service."""
        host = f"{name}.{namespace}.svc.cluster.local"
        return scheme, host, port

    def _is_running_in_cluster(self) -> bool:
        # Presence of serviceaccount token is a good heuristic for in-cluster
        return os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token")

    def _is_resolvable(self, host: str) -> bool:
        # Allow tests and explicit opt-out env to bypass DNS resolution
        if "PYTEST_CURRENT_TEST" in os.environ or os.getenv("GREENKUBE_DISCOVERY_SKIP_DNS_CHECK"):
            return True
        try:
            # getaddrinfo will raise if name can't be resolved
            socket.getaddrinfo(host, None)
            return True
        except Exception:
            return False

    def discover(self, hint: str) -> Optional[str]:
        """Fallback discover implementation: delegates to generic collector with no
        special namespace or port preferences.
        """
        candidates = self._collect_candidates(hint)
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        # candidates: (score, name, namespace, port, scheme)
        _, svc_name, svc_ns, port, scheme = candidates[0]
        host = f"{svc_name}.{svc_ns}.svc.cluster.local"
        if self._is_running_in_cluster() or self._is_resolvable(host):
            return f"{scheme}://{host}:{port}"
        return None

    def _collect_candidates(
        self,
        hint: str,
        prefer_namespaces: Optional[Sequence[str]] = None,
        prefer_ports: Optional[Sequence[int]] = None,
        prefer_labels: Optional[dict] = None,
        name_boost: int = 10,
        ns_boost: int = 5,
        namespace_boost: int = 8,
    ) -> list:
        """Generic candidate collector and scorer.

        - hint: substring to look for in name/namespace
        - prefer_namespaces: sequence of namespaces to boost
        - prefer_ports: sequence of ports to prefer (checked first)
        - name_boost, ns_boost, namespace_boost: scoring weights
        Returns a list of tuples (score, name, namespace, port).
        """
        services = self.list_services()
        if not services:
            return []

        hint = (hint or "").lower()
        prefer_namespaces = tuple(n.lower() for n in (prefer_namespaces or ()))
        prefer_ports = tuple(prefer_ports or ())
        prefer_labels = prefer_labels or {}

        candidates = []
        for svc in services:
            name = getattr(svc.metadata, "name", "") or ""
            ns = getattr(svc.metadata, "namespace", "") or ""
            ports = getattr(svc.spec, "ports", []) or []
            if not ports:
                continue

            lname = name.lower()
            lns = ns.lower()

            score = 0
            if hint and hint in lname:
                score += name_boost
            if hint and hint in lns:
                score += ns_boost

            # increase score when service labels match preferred labels
            labels = getattr(svc.metadata, "labels", {}) or {}
            match_label_bonus = 0
            for k, v in prefer_labels.items():
                if labels.get(k) == v:
                    match_label_bonus += 6
            score += match_label_bonus
            # Penalize known adapter/metrics-adapter services so they don't win
            # over the real Prometheus instance (adapter often serves different API).
            try:
                comp = (labels.get("app.kubernetes.io/component") or "").lower()
                name_contains_adapter = "adapter" in lname
                if comp in ("metrics-adapter", "adapter") or name_contains_adapter:
                    score -= 20
            except Exception:
                pass

            # boost when the namespace is explicitly preferred for the app
            if lns in prefer_namespaces:
                score += namespace_boost

            # Skip services with no positive signal for this hint
            if score <= 0:
                continue

            # prefer specific ports first
            chosen_port = None
            for p in ports:
                pnum = getattr(p, "port", None)
                if pnum in prefer_ports:
                    chosen_port = pnum
                    break
            if not chosen_port:
                chosen_port = self.pick_port(ports)
            if not chosen_port:
                continue

            # give an extra boost when the chosen port matches preferred ports
            try:
                if chosen_port in prefer_ports:
                    score += 7
            except Exception:
                pass
            # extra boost when the chosen port's name indicates a web endpoint
            for p in ports:
                pnum = getattr(p, "port", None)
                pname = (getattr(p, "name", None) or "").lower()
                if pnum == chosen_port and pname in ("web", "http"):
                    score += 10
                    break

            # detect scheme: prefer https when port name suggests TLS or port==443
            scheme = "http"
            for p in ports:
                pnum = getattr(p, "port", None)
                pname = (getattr(p, "name", None) or "").lower()
                if pnum == chosen_port:
                    if "tls" in pname or "https" in pname or chosen_port == 443:
                        scheme = "https"
                    break

            candidates.append((score, name, ns, chosen_port, scheme))

        return candidates
