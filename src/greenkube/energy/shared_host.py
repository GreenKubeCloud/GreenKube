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
#: Reason suffix of a group formed from the current node snapshots (snapshots stored before 0183).
CURRENT_SNAPSHOT_NOTE = "grouping from the current node snapshot (the snapshot in force had no host or provider id)"


@dataclass(frozen=True)
class SharedHost:
    """The host group of one node: its key, node count and this node's share of the host."""

    key: str
    size: int
    share: float
    #: ``True`` when the group needed the ids of the current snapshot of at least one member.
    from_current_snapshot: bool = False
    #: ``True`` when this node itself took the ids of its current snapshot (its snapshot had none).
    borrowed_ids: bool = False

    def reason(self, node_name: str) -> str:
        """The estimation reason of the rows of ``node_name``."""
        reason = (
            f"{SHARED_HOST_REASON_PREFIX}: {self.size} nodes share one host ({self.key}); host idle power, "
            f"memory power and embodied emissions split 1/{self.size} to node '{node_name}'"
        )
        if self.from_current_snapshot:
            reason += f"; {CURRENT_SNAPSHOT_NOTE}"
        return reason


@dataclass(frozen=True)
class HostShareScaled:
    """A node's power and embodied inputs once scaled to its share of a shared host."""

    idle_watts: float
    max_watts: float
    memory_watts_per_gib: float
    embodied_g_per_hour: float


def scale_to_host_share(
    *, idle_watts: float, max_watts: float, memory_watts_per_gib: float, embodied_g_per_hour: float, share: float
) -> HostShareScaled:
    """Scale idle power, memory power and embodied emissions by ``share``; keep the dynamic range.

    The one rule for collection, ``greenkube recompute`` and projections (NRG-15): utilisation is
    already a fraction of the host capacity, so only the baseline is split. A share outside
    (0, 1] counts as 1.
    """
    share = share if 0.0 < share <= 1.0 else 1.0
    idle = max(idle_watts, 0.0)
    return HostShareScaled(
        idle_watts=idle * share,
        max_watts=idle * share + max(max_watts - idle_watts, 0.0),
        memory_watts_per_gib=max(memory_watts_per_gib, 0.0) * share,
        embodied_g_per_hour=max(embodied_g_per_hour, 0.0) * share,
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


def shared_host_groups(nodes: Mapping[str, Any], current: Optional[Mapping[str, Any]] = None) -> Dict[str, SharedHost]:
    """``{node name: SharedHost}`` for the nodes that share a host with at least one other node.

    ``current`` maps node names to their current snapshots. A node of ``nodes`` without host or
    provider id (a snapshot stored before migration 0183) takes the ids of its current snapshot;
    the groups that need it say so in their reason.
    """
    members: Dict[str, list[str]] = defaultdict(list)
    borrowed: set[str] = set()
    borrowing_nodes: set[str] = set()
    for name, info in nodes.items():
        key = _host_key(info)
        no_ids = not getattr(info, "host_id", None) and not getattr(info, "provider_id", None)
        if no_ids and current is not None and name in current:
            key = _host_key(current[name])
            if key is not None:
                borrowed.add(key)
                borrowing_nodes.add(name)
        if key is not None:
            members[key].append(name)
    groups: Dict[str, SharedHost] = {}
    for key, names in members.items():
        if len(names) < 2:
            continue
        for name in names:
            groups[name] = SharedHost(
                key=key,
                size=len(names),
                share=1.0 / len(names),
                from_current_snapshot=key in borrowed,
                borrowed_ids=name in borrowing_nodes,
            )
    return groups
