"""Built-in connectors that only emit signals (roadmap G).

* :mod:`~greenkube.signals.connectors.push`: the push protocol (``POST /api/v1/signals``,
  schema ``signal.v1alpha1``) and the ``push`` source that reads pushed batches into scans;
* :mod:`~greenkube.signals.connectors.krr`: Robusta KRR reports;
* :mod:`~greenkube.signals.connectors.inbox`: storage of pushed signals and the base of the
  sources that read it.

Connectors never create recommendations: their signals reach recommendations
only through consensus (E6), and an external source alone never does (A.5).
"""
