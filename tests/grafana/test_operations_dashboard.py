"""The operations dashboard reads only metric families GreenKube exposes, and is ready for grafana.com (B.7)."""

import importlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "dashboards" / "greenkube-operations.json"
METRIC = re.compile(r"\bgreenkube_[a-z0-9_]+")
SUFFIXES = ("_total", "_bucket", "_count", "_sum", "_created")

#: Modules that register families on the scraped registries (the API registry and OBSERVABILITY_REGISTRY).
REGISTERING_MODULES = (
    "greenkube.api.metrics_endpoint",
    "greenkube.core.observability",
    "greenkube.core.collection_telemetry",
    "greenkube.core.savings_attributor",
    "greenkube.verify.ledger.verification",
    "greenkube.verify.ledger.reconciliation",
    "greenkube.verify.ledger.projector",
    "greenkube.automation.git.rate_limit",
    "greenkube.automation.git.budget",
    "greenkube.automation.readiness",
    "greenkube.auth.metrics",
)
#: Families of the API registry: every API replica serves the same database-backed values.
API_FAMILIES_PATTERN = re.compile(r"greenkube_(co2e|cost)_savings_(attributed|reversed)_[a-z]+_total")


def _dashboard() -> dict:
    return json.loads(DASHBOARD.read_text(encoding="utf-8"))


def _panels(dashboard: dict) -> list[dict]:
    return [panel for panel in dashboard["panels"] if panel.get("targets")]


def _exprs(dashboard: dict) -> list[str]:
    return [target["expr"] for panel in _panels(dashboard) for target in panel["targets"]]


def _family(name: str) -> str:
    for suffix in SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _scraped_families() -> tuple[set[str], set[str]]:
    for module in REGISTERING_MODULES:
        importlib.import_module(module)
    from greenkube.api.metrics_endpoint import REGISTRY as API_REGISTRY
    from greenkube.core.observability import REGISTRY as OBSERVABILITY_REGISTRY

    api = {family.name for family in API_REGISTRY.collect()}
    observability = {family.name for family in OBSERVABILITY_REGISTRY.collect()}
    return api, observability


def test_every_queried_metric_is_registered_on_a_scraped_registry():
    api, observability = _scraped_families()
    used = {name for expr in _exprs(_dashboard()) for name in METRIC.findall(expr)}
    assert used
    missing = {name for name in used if _family(name) not in api | observability and name not in api | observability}
    assert not missing, f"dashboard queries metrics no scraped registry exposes: {sorted(missing)}"


def test_api_registry_series_are_deduplicated_across_replicas():
    """Every API pod serves the same counters: sum only after max by the series labels."""
    api, _ = _scraped_families()
    savings = [expr for expr in _exprs(_dashboard()) if API_FAMILIES_PATTERN.search(expr)]
    assert savings
    for expr in savings:
        for match in API_FAMILIES_PATTERN.finditer(expr):
            before = expr[: match.start()]
            assert before.rstrip().endswith("max by (cluster, recommendation_type, method) ("), expr
    for expr in _exprs(_dashboard()):
        for name in METRIC.findall(expr):
            if _family(name) in api and not API_FAMILIES_PATTERN.fullmatch(name):
                # Gauges of the API registry are read through max(), never summed.
                assert "sum" not in expr, expr


def test_panels_are_scoped_or_marked():
    for panel in _panels(_dashboard()):
        for target in panel["targets"]:
            assert 'namespace=~"$namespace"' in target["expr"], target["expr"]
        has_cluster = all('cluster=~"$cluster"' in target["expr"] for target in panel["targets"])
        assert has_cluster or panel["title"].endswith("(all clusters)"), panel["title"]


def test_no_panel_reads_per_pod_or_footprint_series():
    for expr in _exprs(_dashboard()):
        assert "greenkube_pod_" not in expr
        assert "greenkube_namespace_" not in expr
        assert "sustainability" not in expr


def test_dashboard_is_ready_for_grafana_com():
    dashboard = _dashboard()
    assert "uid" not in dashboard and dashboard.get("id") is None
    assert [item["name"] for item in dashboard["__inputs"]] == ["DS_PROMETHEUS"]
    assert {item["id"] for item in dashboard["__requires"]} >= {"grafana", "prometheus", "timeseries", "stat", "text"}
    variables = {item["name"]: item for item in dashboard["templating"]["list"]}
    assert variables["DS_PROMETHEUS"]["type"] == "datasource"
    assert dashboard["title"] and dashboard["description"] and "greenkube" in dashboard["tags"]
    about = next(panel for panel in dashboard["panels"] if panel["type"] == "text")
    assert "https://docs.greenkube.cloud" in about["options"]["content"]
    for panel in dashboard["panels"]:
        datasources = [panel.get("datasource")] + [target.get("datasource") for target in panel.get("targets", [])]
        for datasource in filter(None, datasources):
            assert datasource["uid"] == "${DS_PROMETHEUS}"
        grid = panel["gridPos"]
        assert grid["x"] + grid["w"] <= 24
    assert len({panel["id"] for panel in dashboard["panels"]}) == len(dashboard["panels"])


@pytest.mark.parametrize("name", ["greenkube_metrics_total", "greenkube_last_collection_timestamp_seconds"])
def test_freshness_families_are_on_the_api_registry(name):
    api, _ = _scraped_families()
    assert name in api
