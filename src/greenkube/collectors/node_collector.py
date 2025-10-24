# src/greenkube/collectors/node_collector.py

from kubernetes import client, config
from .base_collector import BaseCollector
import logging # Use logging instead of print for errors/warnings

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

class NodeCollector(BaseCollector):
    """ Collects node zone information from the Kubernetes cluster. """

    def __init__(self):
        try:
            # Try loading incluster config first, then kubeconfig
            config.load_incluster_config()
            logging.info("Loaded in-cluster Kubernetes configuration.")
        except config.ConfigException:
            try:
                config.load_kube_config()
                logging.info("Loaded Kubernetes configuration from kubeconfig file.")
            except config.ConfigException as e:
                logging.error(f"Could not configure Kubernetes client: {e}")
                raise  # Re-raise the exception to signal failure

        self.v1 = client.CoreV1Api()

    def collect(self) -> dict:
        """
        Collects node names and their corresponding zones from Kubernetes labels.

        Returns:
            dict: A dictionary mapping node names to their zone labels.
                  Returns an empty dictionary if no nodes or zones are found,
                  or if an error occurs.
        """
        nodes_zones_map = {}
        try:
            # print("Collecting node zones from Kubernetes API...") # Use logging
            nodes = self.v1.list_node(watch=False)
            if not nodes.items:
                logging.warning("No nodes found in the cluster.")
                return nodes_zones_map # Return empty dict

            for node in nodes.items:
                node_name = node.metadata.name
                zone = node.metadata.labels.get('topology.kubernetes.io/zone')
                if zone:
                    nodes_zones_map[node_name] = zone
                    logging.info(f" -> Found node '{node_name}' in zone '{zone}'")
                else:
                    logging.warning(f" -> Zone label 'topology.kubernetes.io/zone' not found for node '{node_name}'.")

            if not nodes_zones_map:
                 logging.warning("No nodes with a 'topology.kubernetes.io/zone' label were found.")

        except client.ApiException as e:
            logging.error(f"Kubernetes API error while listing nodes: {e}")
            # Depending on the error, you might want to return {} or raise it
            return {} # Return empty dict on API error for resilience
        except Exception as e:
             logging.error(f"An unexpected error occurred while collecting node zones: {e}")
             return {} # Return empty dict on other errors


        return nodes_zones_map
