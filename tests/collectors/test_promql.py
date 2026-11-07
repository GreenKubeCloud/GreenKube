"""PromQLCatalog: the single definition of every PromQL expression (roadmap A1, BUG-01)."""

from types import SimpleNamespace

import pytest

from greenkube.collectors.promql import CONTAINER_FILTER, PromQLCatalog, parse_duration_seconds
from greenkube.core.config import Config


def test_rate_window_is_at_least_four_scrape_intervals():
    assert PromQLCatalog(step_seconds=300, scrape_interval_seconds=30).rate_window == "300s"
    assert PromQLCatalog(step_seconds=60, scrape_interval_seconds=30).rate_window == "120s"
    assert PromQLCatalog(step_seconds=30, scrape_interval_seconds=15).rate_window_seconds == 60


def test_container_scoped_expressions_exclude_pod_cgroup_and_pause_series():
    catalog = PromQLCatalog(step_seconds=300)

    assert catalog.container_cpu_usage() == (
        'sum(rate(container_cpu_usage_seconds_total{container!="",container!="POD"}[300s])) '
        "by (namespace,pod,container,node)"
    )
    assert catalog.container_memory_working_set() == (
        'sum(container_memory_working_set_bytes{container!="",container!="POD"}) by (namespace,pod,container,node)'
    )
    for expression in (catalog.pod_disk_read(), catalog.pod_disk_write(), catalog.pod_ephemeral_storage_usage()):
        assert CONTAINER_FILTER in expression


def test_pod_scoped_and_kube_state_expressions_are_unfiltered_by_container():
    catalog = PromQLCatalog(step_seconds=300)

    assert "container!=" not in catalog.pod_network_receive()
    assert "container!=" not in catalog.pod_network_transmit()
    assert catalog.pod_restarts() == "sum(kube_pod_container_status_restarts_total) by (namespace,pod)"
    assert catalog.pod_node() == 'max(kube_pod_info{node!=""}) by (namespace,pod,node)'
    assert catalog.pod_cgroup_cpu_usage() == (
        'sum(rate(container_cpu_usage_seconds_total{container="",pod!=""}[300s])) by (namespace,pod,node)'
    )


def test_extra_matchers_are_applied_to_every_selector():
    catalog = PromQLCatalog(step_seconds=300, extra_matchers=('cluster="eu-1"',))

    assert '{container!="",container!="POD",cluster="eu-1"}' in catalog.container_cpu_usage()
    assert 'container_network_receive_bytes_total{cluster="eu-1"}' in catalog.pod_network_receive()
    assert 'kube_pod_info{node!="",cluster="eu-1"}' in catalog.pod_node()


def test_catalog_reads_validated_settings_and_tolerates_test_doubles():
    catalog = PromQLCatalog.from_config(Config(PROMETHEUS_QUERY_RANGE_STEP="1m", PROMETHEUS_SCRAPE_INTERVAL="15s"))
    assert (catalog.step, catalog.rate_window) == ("60s", "60s")

    fallback = PromQLCatalog.from_config(SimpleNamespace(PROMETHEUS_QUERY_RANGE_STEP=object()))
    assert (fallback.step_seconds, fallback.scrape_interval_seconds) == (300, 30)


@pytest.mark.parametrize(("value", "seconds"), [("30s", 30), ("5m", 300), ("2h", 7200), ("1d", 86400), (45, 45)])
def test_parse_duration_seconds(value, seconds):
    assert parse_duration_seconds(value) == seconds


@pytest.mark.parametrize("value", ["", "5", "0s", "-1m", "1w", True])
def test_parse_duration_seconds_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        parse_duration_seconds(value)


@pytest.mark.parametrize("field", ["PROMETHEUS_SCRAPE_INTERVAL", "COLLECTION_SAFETY_LAG", "COLLECTION_MAX_BACKFILL"])
def test_collection_settings_are_validated(field):
    with pytest.raises(ValueError, match=field):
        Config(**{field: "soon"})
    assert getattr(Config(**{field: "2H"}), field) == "2h"


def test_collection_settings_defaults():
    cfg = Config()
    assert (cfg.PROMETHEUS_SCRAPE_INTERVAL, cfg.COLLECTION_SAFETY_LAG, cfg.COLLECTION_MAX_BACKFILL) == (
        "30s",
        "1m",
        "24h",
    )
