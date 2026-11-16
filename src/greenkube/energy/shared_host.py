# src/greenkube/energy/shared_host.py
"""Kubernetes nodes that are one physical host (kind, k3d, minikube multi-node) (NRG-15).

Container-based nodes report the host's CPU and memory as their own capacity. Charging each
node its full idle power, memory power and embodied emissions multiplies the host by the node
count. Nodes are grouped by ``status.nodeInfo.bootID`` (same kernel), or by a ``kind://`` /
``k3d://`` provider ID when the boot ID is unknown; each node of a group of N gets 1/N of the
host's idle power, memory power and embodied emissions. Dynamic power is not scaled: each
node's utilisation is already a fraction of the host capacity.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

#: Provider ID schemes of nodes that run as containers of one machine.
CONTAINER_NODE_SCHEMES = ("kind://", "k3d://")
#: ``estimation_reasons`` prefix of rows of a node that shares its host.
SHARED_HOST_REASON_PREFIX = "Shared host"


@dataclass(frozen=True)
class SharedHost:
    """The host group of one node: its key, node count and this node's share of the host."""

    key: str
    size: int
    share: float

    def reason(self, node_name: str) -> str:
        """The estimation reason of the rows of ``node_name``."""
        return (
            f"{SHARED_HOST_REASON_PREFIX}: {self.size} nodes share one host ({self.key}); host idle power, "
            f"memory power and embodied emissions split 1/{self.size} to node '{node_name}'"
        )


def _host_key(info: Any) -> Optional[str]:
    host_id = getattr(info, "host_id", None)
    if host_id:
        return f"bootID {host_id}"
    provider_id = getattr(info, "provider_id", None) or ""
    for scheme in CONTAINER_NODE_SCHEMES:
        if provider_id.startswith(scheme):
            return f"{scheme.rstrip(':/')} provider"
    return None


def shared_host_groups(nodes: Mapping[str, Any]) -> Dict[str, SharedHost]:
    """``{node name: SharedHost}`` for the nodes that share a host with at least one other node."""
    members: Dict[str, list[str]] = defaultdict(list)
    for name, info in nodes.items():
        key = _host_key(info)
        if key is not None:
            members[key].append(name)
    groups: Dict[str, SharedHost] = {}
    for key, names in members.items():
        if len(names) < 2:
            continue
        for name in names:
            groups[name] = SharedHost(key=key, size=len(names), share=1.0 / len(names))
    return groups
