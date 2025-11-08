from typing import Optional

from .base import BaseDiscovery


class PrometheusDiscovery(BaseDiscovery):
    def discover(self) -> Optional[str]:
        candidates = self._collect_candidates(
            "prometheus", prefer_namespaces=("monitoring", "prometheus"), prefer_ports=(9090,)
        )
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        _, svc_name, svc_ns, port = candidates[0]
        return self.build_dns(svc_name, svc_ns, port)
