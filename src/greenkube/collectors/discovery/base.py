"""
Base discovery utilities for Kubernetes services.
"""

from __future__ import annotations

import logging
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

    def discover(self, hint: str) -> Optional[str]:
        """Fallback discover implementation: delegates to generic collector with no
        special namespace or port preferences.
        """
        candidates = self._collect_candidates(hint)
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        _, svc_name, svc_ns, port = candidates[0]
        return self.build_dns(svc_name, svc_ns, port)

    def _collect_candidates(
        self,
        hint: str,
        prefer_namespaces: Optional[Sequence[str]] = None,
        prefer_ports: Optional[Sequence[int]] = None,
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
            # do not treat 'default' namespace alone as a positive signal;
            # only boost when the namespace is explicitly preferred for the app
            if lns in prefer_namespaces:
                score += namespace_boost

            # Skip services with no positive signal for this hint
            if score <= 0:
                continue

            if score > 0:
                # prefer specific ports first
                chosen_port = None
                for p in ports:
                    pnum = getattr(p, "port", None)
                    if pnum in prefer_ports:
                        chosen_port = pnum
                        break
                if not chosen_port:
                    chosen_port = self.pick_port(ports)
                if chosen_port:
                    candidates.append((score, name, ns, chosen_port))

        return candidates
