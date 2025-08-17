# src/greenkube/collectors/node_collector.py

from .base_collector import BaseCollector

class NodeCollector(BaseCollector):
    """
    A collector responsible for discovering the geographical zones of Kubernetes nodes.
    """
    
    def collect(self):
        """
        Get all nodes in the cluster.
        """
        # TODO - Implement actual node discovery logic.
        pass

    def get_zones(self) -> list[str]:
        """
        Retrieves a list of unique geographical zones for all nodes in the cluster.

        **Placeholder Implementation:**
        In a real-world scenario, this method would query the Kubernetes API or a
        cloud provider's API to get node labels that specify the region/zone
        (e.g., 'topology.kubernetes.io/zone' or 'failure-domain.beta.kubernetes.io/zone').

        For now, it returns a hardcoded list for demonstration purposes.

        Returns:
            list[str]: A list of unique zone identifiers (e.g., ['FR', 'DE']).
        """
        print("Discovering node zones... (using placeholder data)")
        # TODO: Replace this with actual cloud/Kubernetes API calls.
        # Example hardcoded zones for a multi-region cluster:
        discovered_zones = [
            "FR",  # A node in France
            "DE",  # A node in Germany
            "FR"   # Another node in France
        ]

        # Return only the unique zones
        unique_zones = list(set(discovered_zones))
        print(f"Found unique zones: {unique_zones}")
        return unique_zones

