# src/greenkube/collectors/node_collector.py

import logging
from datetime import datetime, timezone

from kubernetes_asyncio.client.rest import ApiException

from greenkube.core.config import config as global_config
from greenkube.core.k8s_client import get_core_v1_api

from .base_collector import BaseCollector

if False:  # TYPE_CHECKING
    pass

from ..utils.k8s_utils import parse_quantity

logger = logging.getLogger(__name__)


class NodeCollector(BaseCollector):
    """Collects node zone information and instance types from the Kubernetes cluster."""

    def __init__(self):
        self._api = None

    async def _ensure_client(self):
        """
        Lazily initialize the Kubernetes Async client using the centralized thread-safe loader.
        """
        if self._api:
            return self._api

        self._api = await get_core_v1_api()
        return self._api

    async def collect(self) -> dict:
        """
        Collects comprehensive node information from Kubernetes.

        Returns:
            dict: A dictionary mapping node names to NodeInfo objects containing
                  zone, region, cloud provider, instance type, architecture, and node pool.
        """
        from greenkube.models.node import NodeInfo

        nodes_info = {}
        api = await self._ensure_client()
        if not api:
            logger.debug("Kubernetes client not configured; skipping node collection.")
            return nodes_info

        try:
            # Async call
            nodes = await api.list_node(watch=False)
            if not nodes.items:
                logger.warning("No nodes found in the cluster.")
                return nodes_info

            for node in nodes.items:
                node_name = node.metadata.name
                labels = node.metadata.labels or {}

                cloud_provider = self._detect_cloud_provider(labels)
                instance_type = self._extract_instance_type(labels, node, cloud_provider)

                zone = labels.get("topology.kubernetes.io/zone") or labels.get("failure-domain.beta.kubernetes.io/zone")
                region = labels.get("topology.kubernetes.io/region") or labels.get(
                    "failure-domain.beta.kubernetes.io/region"
                )
                architecture = labels.get("kubernetes.io/arch") or labels.get("beta.kubernetes.io/arch")
                node_pool = self._extract_node_pool(labels, cloud_provider)

                cpu_capacity = self._extract_cpu_capacity(node)
                memory_capacity = self._extract_memory_capacity(node)

                nodes_info[node_name] = NodeInfo(
                    name=node_name,
                    instance_type=instance_type,
                    zone=zone,
                    region=region,
                    cloud_provider=cloud_provider,
                    architecture=architecture,
                    node_pool=node_pool,
                    cpu_capacity_cores=cpu_capacity,
                    memory_capacity_bytes=memory_capacity,
                    timestamp=datetime.now(timezone.utc),
                )

                logger.info(
                    " -> Node '%s': provider=%s, instance=%s, zone=%s, cpu=%s, mem=%s",
                    node_name,
                    cloud_provider,
                    instance_type,
                    zone,
                    cpu_capacity,
                    memory_capacity,
                )

            if not nodes_info:
                logger.warning("No nodes found in the cluster.")

        except ApiException as e:
            logger.error("Kubernetes API error while listing nodes: %s", e)
            return {}
        except Exception as e:
            logger.error("An unexpected error occurred while collecting nodes: %s", e)
            return {}
        finally:
            # We should close the api client to avoid resource leak?
            # self._api.api_client.close() ?
            # Since we cache self._api, we might keep it open.
            pass

        return nodes_info

    async def close(self):
        """Close the Kubernetes API client if it exists."""
        if self._api:
            await self._api.api_client.close()
            logger.debug("NodeCollector Kubernetes client closed.")
            self._api = None

    async def collect_detailed_info(self) -> dict:
        """
        Collect comprehensive node information including cloud provider, instance type,
        zone, region, and other metadata.
        """
        nodes_info = {}
        api = await self._ensure_client()

        if not api:
            logger.debug("Kubernetes client not configured; skipping detailed node collection.")
            return nodes_info

        try:
            nodes = await api.list_node(watch=False)
            if not nodes.items:
                logger.debug("No nodes found when collecting detailed info.")
                return nodes_info

            for node in nodes.items:
                node_name = node.metadata.name
                labels = node.metadata.labels or {}

                # Detect cloud provider
                cloud_provider = self._detect_cloud_provider(labels)

                # Extract instance type with multiple fallbacks
                instance_type = self._extract_instance_type(labels, node, cloud_provider)

                # Extract zone
                zone = labels.get("topology.kubernetes.io/zone") or labels.get("failure-domain.beta.kubernetes.io/zone")

                # Extract region
                region = labels.get("topology.kubernetes.io/region") or labels.get(
                    "failure-domain.beta.kubernetes.io/region"
                )

                # Extract architecture
                architecture = labels.get("kubernetes.io/arch") or labels.get("beta.kubernetes.io/arch")

                # Extract node pool (cloud-specific)
                node_pool = self._extract_node_pool(labels, cloud_provider)

                nodes_info[node_name] = {
                    "instance_type": instance_type,
                    "zone": zone,
                    "region": region,
                    "cloud_provider": cloud_provider,
                    "architecture": architecture,
                    "node_pool": node_pool,
                }

                logger.info(
                    "Node '%s': provider=%s, instance=%s, zone=%s, region=%s, arch=%s",
                    node_name,
                    cloud_provider,
                    instance_type,
                    zone,
                    region,
                    architecture,
                )

        except ApiException as e:
            logger.error("Kubernetes API error while collecting detailed node info: %s", e)
            return {}
        except Exception as e:
            logger.error("Unexpected error while collecting detailed node info: %s", e)
            return {}

        return nodes_info

    def _detect_cloud_provider(self, labels: dict) -> str:
        """
        Detect the cloud provider from node labels.
        """
        # Check for OVH-specific labels
        if any(key.startswith("k8s.ovh.net/") for key in labels.keys()):
            return "ovh"

        # Check for Azure-specific labels
        if any(key.startswith("kubernetes.azure.com/") for key in labels.keys()):
            return "azure"

        # Check for AWS-specific labels
        if any(key.startswith("eks.amazonaws.com/") for key in labels.keys()):
            return "aws"
        if "node.kubernetes.io/instance-type" in labels and labels.get("topology.kubernetes.io/region", "").startswith(
            ("us-", "eu-", "ap-", "ca-", "sa-")
        ):
            # AWS regions typically start with these prefixes
            return "aws"

        # Check for GCP-specific labels
        if any(key.startswith("cloud.google.com/") for key in labels.keys()):
            return "gcp"

        # Check ProviderID patterns (fallback)
        # This would require accessing node.spec.providerID, but we only have labels here
        # So we return unknown if no cloud-specific labels are found
        return "unknown"

    def _extract_instance_type(self, labels: dict, node, cloud_provider: str) -> str:
        """
        Extract instance type from node labels with cloud-specific fallbacks.
        """
        # Try standard Kubernetes label first
        instance_type = labels.get("node.kubernetes.io/instance-type")
        if instance_type:
            return instance_type

        # Try beta label
        instance_type = labels.get("beta.kubernetes.io/instance-type")
        if instance_type:
            return instance_type

        # Cloud-specific labels
        if cloud_provider == "ovh":
            # OVH uses the same labels as standard Kubernetes
            pass
        elif cloud_provider == "azure":
            # Azure might have additional labels
            pass

        # Try configured label key from global config
        label_key = getattr(
            global_config,
            "PROMETHEUS_NODE_INSTANCE_LABEL",
            "label_node_kubernetes_io_instance_type",
        )
        instance_type = labels.get(label_key)
        if instance_type:
            return instance_type

        # Fallback: infer from CPU capacity
        try:
            capacity = getattr(node, "status", None) and getattr(node.status, "capacity", None)
            if capacity and "cpu" in capacity:
                # Use shared parse_quantity
                cpu_qty = parse_quantity(capacity["cpu"])
                cpu_cores = int(cpu_qty)
                return f"cpu-{cpu_cores}"
        except Exception as e:
            logger.debug("Could not infer instance type from capacity: %s", e)

        return "unknown"

    def _extract_node_pool(self, labels: dict, cloud_provider: str) -> str:
        """
        Extract node pool name from cloud-specific labels.
        """
        if cloud_provider == "ovh":
            return labels.get("k8s.ovh.net/nodepool")
        elif cloud_provider == "azure":
            return labels.get("kubernetes.azure.com/agentpool") or labels.get("agentpool")
        elif cloud_provider == "aws":
            return labels.get("eks.amazonaws.com/nodegroup")
        elif cloud_provider == "gcp":
            return labels.get("cloud.google.com/gke-nodepool")

        return None

    async def collect_instance_types(self) -> dict:
        """
        Collect node -> instance_type mapping using Kubernetes node labels.
        """
        label_key = getattr(
            global_config,
            "PROMETHEUS_NODE_INSTANCE_LABEL",
            "label_node_kubernetes_io_instance_type",
        )
        node_instance_map = {}

        api = await self._ensure_client()
        if not api:
            logger.debug("Kubernetes client not configured; skipping instance type collection.")
            return node_instance_map

        try:
            nodes = await api.list_node(watch=False)
            if not nodes.items:
                logger.debug("No nodes found when collecting instance types.")
                return node_instance_map

            for node in nodes.items:
                node_name = node.metadata.name
                labels = node.metadata.labels or {}
                instance_type = labels.get(label_key)
                if instance_type:
                    node_instance_map[node_name] = instance_type
                    logger.info(
                        "Found instance type for node '%s': %s",
                        node_name,
                        instance_type,
                    )
                    continue

                # If explicit instance-type label is not available, attempt to
                # infer instance size from the node capacity (number of CPUs).
                try:
                    capacity = getattr(node, "status", None) and getattr(node.status, "capacity", None)
                    if capacity and "cpu" in capacity:
                        # Use shared parse_quantity
                        cpu_qty = parse_quantity(capacity["cpu"])
                        cpu_cores = int(cpu_qty)

                        inferred_label = f"cpu-{cpu_cores}"
                        node_instance_map[node_name] = inferred_label
                        logger.info(
                            "Inferred instance cores for node '%s': %s cores",
                            node_name,
                            cpu_cores,
                        )
                except Exception as e:
                    # Log warning with exception info instead of silent failure
                    logger.warning(
                        "Could not infer instance type from capacity for node '%s': %s", node_name, e, exc_info=True
                    )

        except ApiException as e:
            logger.error("Kubernetes API error while listing nodes for instance types: %s", e)
            return {}
        except Exception as e:
            logger.error("Unexpected error while collecting node instance types: %s", e)
            return {}

        return node_instance_map

    def _extract_cpu_capacity(self, node) -> float | None:
        """Extract CPU capacity from node status."""
        try:
            capacity = getattr(node, "status", None) and getattr(node.status, "capacity", None)
            if capacity and "cpu" in capacity:
                cpu_qty = parse_quantity(capacity["cpu"])
                return float(cpu_qty)
        except Exception as e:
            logger.debug("Could not extract CPU capacity: %s", e)
        return None

    def _extract_memory_capacity(self, node) -> int | None:
        """Extract memory capacity from node status."""
        try:
            capacity = getattr(node, "status", None) and getattr(node.status, "capacity", None)
            if capacity and "memory" in capacity:
                mem_qty = parse_quantity(capacity["memory"])
                return int(mem_qty)
        except Exception as e:
            logger.debug("Could not extract memory capacity: %s", e)
        return None
