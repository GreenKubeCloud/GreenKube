from typing import Optional

from .base import BaseDiscovery


class OpenCostDiscovery(BaseDiscovery):
    def discover(self) -> Optional[str]:
        candidates = self._collect_candidates("opencost", prefer_namespaces=("opencost",), prefer_ports=(8080,))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        _, svc_name, svc_ns, port = candidates[0]
        return self.build_dns(svc_name, svc_ns, port)
