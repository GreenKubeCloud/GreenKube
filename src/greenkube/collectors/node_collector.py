# src/greenkube/collectors/node_collector.py

from kubernetes import client, config
from .base_collector import BaseCollector

class NodeCollector(BaseCollector):
    """
    Collecte les informations de localisation (région/zone) des nœuds 
    directement depuis l'API Kubernetes.
    """

    def __init__(self, kubeconfig_path=None):
        """
        Initialise le client Kubernetes.
        Tente de se connecter de deux manières :
        1. In-cluster: si l'application tourne dans un pod Kubernetes.
        2. Kubeconfig: si l'application tourne en local (comme sur votre Mac).
        """
        try:
            # Idéal pour le déploiement via Helm
            config.load_incluster_config()
        except config.ConfigException:
            # Idéal pour le développement local avec kind
            config.load_kube_config(config_file=kubeconfig_path)
        
        self.core_v1 = client.CoreV1Api()
        # Le label standard pour la zone géographique sur la plupart des clouds.
        self.zone_label = "topology.kubernetes.io/zone"

    def collect(self) -> list[str]:
        """
        Interroge l'API Kubernetes pour lister tous les nœuds et
        extrait la valeur de leur label de zone.

        :return: Une liste de zones uniques (ex: ['europe-west1-b', 'europe-west1-c']).
        """
        print("Collecting node zones from Kubernetes API...")
        zones = set()  # Utiliser un set pour éviter les doublons
        try:
            node_list = self.core_v1.list_node()
            for node in node_list.items:
                node_labels = node.metadata.labels
                if self.zone_label in node_labels:
                    zone = node_labels[self.zone_label]
                    zones.add(zone)
                    print(f"  -> Found node '{node.metadata.name}' in zone '{zone}'")
                else:
                    print(f"  -> Warning: Node '{node.metadata.name}' has no zone label ('{self.zone_label}').")
            
            if not zones:
                print("  -> No zones found on any node. Defaulting to 'local-dev'.")
                return ["local-dev"] # Fournit une valeur par défaut pour les clusters locaux

            return list(zones)

        except Exception as e:
            print(f"Error while collecting node data from Kubernetes API: {e}")
            # En cas d'erreur, on retourne une valeur par défaut pour ne pas crasher
            return ["error-collecting"]