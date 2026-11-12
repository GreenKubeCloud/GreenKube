"""Built-in connectors that only emit signals (roadmap G).

* :mod:`~greenkube.signals.connectors.push`: the push protocol (``POST /api/v1/signals``,
  schema ``signal.v1alpha1``) and the ``push`` source that replays pushed batches into scans;
* :mod:`~greenkube.signals.connectors.krr`: Robusta KRR reports;
* :mod:`~greenkube.signals.connectors.kubecost`: the Kubecost savings APIs;
* :mod:`~greenkube.signals.connectors.polaris`: Fairwinds Polaris hygiene findings;
* :mod:`~greenkube.signals.connectors.gke`: GKE recommender insights;
* :mod:`~greenkube.signals.connectors.azure_advisor`: Azure Advisor advice about AKS clusters;
* :mod:`~greenkube.signals.connectors.inbox`: storage of pushed signals.

Connectors never create recommendations: their signals reach recommendations
only through consensus (E6).
"""
