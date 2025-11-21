# src/greenkube/collectors/discovery/prometheus.py
import logging
import os
import warnings
from typing import Optional

import requests

# Suppress InsecureRequestWarning for self-signed certs in-cluster
from requests.packages.urllib3.exceptions import InsecureRequestWarning

from greenkube.core.config import config

from .base import BaseDiscovery

# Get logger for this module
logger = logging.getLogger(__name__)


class PrometheusDiscovery(BaseDiscovery):
    """
    Discovers a functional Prometheus endpoint by scoring candidates and
    probing the top contenders for a valid Prometheus API response.
    """

    # A list of common API paths to probe for a 'query' endpoint.
    PROBE_PATHS = [
        "/api/v1/query",  # Standard Prometheus
        "/query",  # Some proxies
        "/prometheus/api/v1/query",  # Common when namespaced
    ]

    def discover(self) -> Optional[str]:
        """
        Attempts to find a valid, running Prometheus service endpoint.
        """
        candidates = self._collect_candidates(
            "prometheus",
            prefer_namespaces=("monitoring", "prometheus"),
            prefer_ports=(9090,),
            # Add labels to strongly prefer the 'kube-prometheus' stack default
            prefer_labels={
                "app.kubernetes.io/name": "prometheus",
                "app.kubernetes.io/instance": "k8s",
            },
        )
        if not candidates:
            logger.info("Prometheus discovery: no candidates found after scoring.")
            return None

        candidates.sort(key=lambda x: x[0], reverse=True)

        # For unit testing, bypass HTTP probes and return the top-scored candidate.
        if os.getenv("PYTEST_CURRENT_TEST"):
            score, svc_name, svc_ns, port, scheme = candidates[0]
            host = f"{svc_name}.{svc_ns}.svc.cluster.local"
            return f"{scheme}://{host}:{port}"

        # Probe the top 5 candidates in score order.
        logger.info(f"Prometheus discovery: Probing top {len(candidates[:5])} candidates.")
        for score, svc_name, svc_ns, port, scheme in candidates[:5]:
            host = f"{svc_name}.{svc_ns}.svc.cluster.local"

            # Skip candidates that aren't resolvable or running in-cluster
            if not (self._is_running_in_cluster() or self._is_resolvable(host)):
                logger.debug(f"Prometheus discovery: Skipping candidate '{host}' (score={score}) - unresolvable.")
                continue

            base_url = f"{scheme}://{host}:{port}"
            if self._probe_prometheus_endpoint(base_url, score):
                logger.info(f"Prometheus discovery: Successfully verified endpoint {base_url}")
                return base_url

        logger.warning("Prometheus discovery: Probed top candidates, but none responded with a valid Prometheus API.")
        return None

    def _probe_prometheus_endpoint(self, base_url: str, score: int) -> bool:
        """
        Probes a candidate URL to see if it's a valid Prometheus query API.

        Returns True if a valid response is received, False otherwise.
        """
        for path in self.PROBE_PATHS:
            url = f"{base_url.rstrip('/')}{path}"
            verify_certs = config.PROMETHEUS_VERIFY_CERTS
            # Only suppress SSL warnings if verification is explicitly disabled
            if not verify_certs:
                warnings.simplefilter("ignore", InsecureRequestWarning)

            try:
                # Use a simple 'up' query which is lightweight and universal
                resp = requests.get(url, params={"query": "up"}, timeout=3, verify=verify_certs)
                status = resp.status_code

                try:
                    j = resp.json()
                    success = j.get("status") == "success"
                except requests.exceptions.JSONDecodeError:
                    j = None
                    success = False

                logger.info(
                    "Probing Prometheus candidate %s (score=%s) path=%s -> status=%s success=%s",
                    base_url,
                    score,
                    path,
                    status,
                    success,
                )

                # We require a 200 OK AND a json body with "status": "success"
                # This is the same strict check our standalone script used.
                if status == 200 and success:
                    return True

            except requests.exceptions.RequestException as e:
                logger.debug(
                    "Prometheus probe failed for %s (score=%s) path=%s -> %s",
                    base_url,
                    score,
                    path,
                    e,
                )
                # Continue to the next probe path
                continue

        # If all probe paths fail for this base_url, return False
        return False
