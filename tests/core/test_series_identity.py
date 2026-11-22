"""COL-21: series identity reads only the labels the PromQL catalog groups by.

Every catalog query aggregates ``by (namespace,pod,container,node)`` (or a
subset), so alternative label names (``kubernetes_namespace``, ``pod_name``,
``container_name``, ``kubernetes_node``...) never reach the parser. Accepting
them suggested support for setups that in fact yield no data.
"""

from greenkube.collectors.promql import PromQLCatalog
from greenkube.core.historical_range_processor import HistoricalRangeProcessor


def test_identity_reads_the_catalog_labels():
    series = {"metric": {"namespace": "ns", "pod": "p", "container": "c", "node": "n"}}
    assert HistoricalRangeProcessor._series_identity(series) == ("ns", "p", "c", "n")


def test_alternative_label_names_are_not_read():
    series = {
        "metric": {
            "kubernetes_namespace": "ns",
            "namespace_name": "ns",
            "pod_name": "p",
            "kubernetes_pod_name": "p",
            "container_name": "c",
            "kubernetes_node": "n",
        }
    }
    assert HistoricalRangeProcessor._series_identity(series) == ("", "", "", "")


def test_every_usage_query_groups_by_the_labels_the_parser_reads():
    catalog = PromQLCatalog(step_seconds=300)
    for query in (
        catalog.cpu_usage_by_level(),
        catalog.memory_working_set_by_level(),
        catalog.memory_working_set_peak_by_level(),
        catalog.pod_restarts(),
        catalog.pod_node(),
    ):
        assert "by (namespace,pod" in query, query
