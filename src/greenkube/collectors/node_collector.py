# src/greenkube/collectors/node_collector.py

import logging

from kubernetes import client, config

from greenkube.core.config import config as global_config

from .base_collector import BaseCollector

logger = logging.getLogger(__name__)


class NodeCollector(BaseCollector):
    """Collects node zone information and instance types from the Kubernetes cluster."""

    def __init__(self):
        # Attempt to load Kubernetes configuration. Prefer in-cluster, fall
        # back to local kubeconfig. Only if one of these succeeds will we
        # instantiate the CoreV1Api client. This mirrors expected test
        # behavior where tests may patch the config and/or CoreV1Api.
        config_loaded = False
        try:
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes configuration.")
            config_loaded = True
        except config.ConfigException:
            try:
                config.load_kube_config()
                logger.info("Loaded Kubernetes configuration from kubeconfig file.")
                config_loaded = True
            except config.ConfigException as e:
                logger.warning("Kubernetes configuration not available: %s", e)

        if not config_loaded:
            # No usable kube config found; disable cluster access.
            self.v1 = None
            return

        # Create the API client (tests commonly patch this constructor).
        try:
            self.v1 = client.CoreV1Api()
        except Exception as e:
            logger.warning("Failed to create Kubernetes API client: %s", e)
            self.v1 = None

    def collect(self) -> dict:
        """
        Collects node names and their corresponding zones from Kubernetes labels.

        Returns:
            dict: A dictionary mapping node names to their zone labels.
        """
        nodes_zones_map = {}
        # If there's no configured Kubernetes client, return empty results.
        if not getattr(self, "v1", None):
            logger.debug("Kubernetes client not configured; skipping node zone collection.")
            return nodes_zones_map
        try:
            nodes = self.v1.list_node(watch=False)
            if not nodes.items:
                logger.warning("No nodes found in the cluster.")
                return nodes_zones_map

            for node in nodes.items:
                node_name = node.metadata.name
                zone = None
                # Prefer the standard topology label, fall back to legacy if needed
                if node.metadata.labels:
                    zone = node.metadata.labels.get("topology.kubernetes.io/zone") or node.metadata.labels.get(
                        "failure-domain.beta.kubernetes.io/zone"
                    )

                if zone:
                    nodes_zones_map[node_name] = zone
                    logger.info(" -> Found node '%s' in zone '%s'", node_name, zone)
                else:
                    logger.warning(" -> Zone label not found for node '%s'", node_name)

            if not nodes_zones_map:
                logger.warning("No nodes with a zone label were found.")

        except client.ApiException as e:
            logger.error("Kubernetes API error while listing nodes: %s", e)
            return {}
        except Exception as e:
            logger.error("An unexpected error occurred while collecting node zones: %s", e)
            return {}

        return nodes_zones_map

    def collect_instance_types(self) -> dict:
        """
        Collect node -> instance_type mapping using Kubernetes node labels.
        The label key is configurable via `global_config.PROMETHEUS_NODE_INSTANCE_LABEL`.

        Returns:
            dict: node name -> instance_type (only entries where instance type label exists)
        """
        label_key = getattr(
            global_config,
            "PROMETHEUS_NODE_INSTANCE_LABEL",
            "label_node_kubernetes_io_instance_type",
        )
        node_instance_map = {}

        # If there's no configured Kubernetes client, return empty results.
        if not getattr(self, "v1", None):
            logger.debug("Kubernetes client not configured; skipping instance type collection.")
            return node_instance_map

        try:
            nodes = self.v1.list_node(watch=False)
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
                # This helps produce more realistic energy estimates when
                # cloud instance labels are absent.
                try:
                    capacity = getattr(node, "status", None) and getattr(node.status, "capacity", None)
                    if capacity and "cpu" in capacity:
                        cpu_cores = int(str(capacity["cpu"]))
                        inferred_label = f"cpu-{cpu_cores}"
                        node_instance_map[node_name] = inferred_label
                        logger.info(
                            "Inferred instance cores for node '%s': %s cores",
                            node_name,
                            cpu_cores,
                        )
                except Exception:
                    # If anything goes wrong parsing capacity, skip silently.
                    logger.debug(
                        "Could not infer instance type from capacity for node '%s'",
                        node_name,
                    )

        except client.ApiException as e:
            logger.error("Kubernetes API error while listing nodes for instance types: %s", e)
            return {}
        except Exception as e:
            logger.error("Unexpected error while collecting node instance types: %s", e)
            return {}

        return node_instance_map
