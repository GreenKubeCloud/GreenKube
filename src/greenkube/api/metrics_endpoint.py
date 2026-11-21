# src/greenkube/api/metrics_endpoint.py
"""
Prometheus metrics exposition for GreenKube.

Exposes comprehensive cluster, namespace, pod, node, and recommendation
metrics as Prometheus Gauges so they can be scraped by Prometheus and
visualized in Grafana dashboards.

Label conventions follow kube-state-metrics standards (namespace, pod, node)
and add `cluster` and `region` for multi-cluster / multi-region environments.
"""

import logging
import time
import weakref
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import log2
from typing import Any, List, Optional, Sequence

from prometheus_client import CollectorRegistry, Gauge, generate_latest
from prometheus_client.core import CounterMetricFamily

from greenkube.core.config import get_config
from greenkube.core.observability import generate_latest_observability, record_automation_backlog
from greenkube.core.recommendation_ranking import rank_recommendations
from greenkube.core.savings_attributor import annual_rates_by_method
from greenkube.core.sustainability_score import SustainabilityScorer
from greenkube.models.metrics import CombinedMetric, MetricsSummaryRow, Recommendation, RecommendationRecord
from greenkube.models.node import NodeInfo
from greenkube.models.savings import (
    METHOD_MEASURED,
    METHOD_PROJECTED,
    SAVINGS_METHODS,
    SavingsAccrualTotal,
    SavingsCounterTotal,
)
from greenkube.storage.base_gitops_repository import GitOpsRepository
from greenkube.utils.date_utils import ensure_utc

logger = logging.getLogger(__name__)

# Use a custom registry to avoid polluting the default registry with
# process/platform collectors that are irrelevant in a FastAPI context.
REGISTRY = CollectorRegistry()


def _get_cluster_name() -> str:
    """Return the configured cluster name, falling back to empty string."""
    try:
        cfg = get_config()
        return cfg.CLUSTER_NAME or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Pod-level gauges — kube-state-metrics compatible labels
# ---------------------------------------------------------------------------
POD_LABELS = ["cluster", "namespace", "pod", "node", "region"]

POD_CO2 = Gauge(
    "greenkube_pod_co2e_grams",
    "GHG Scope 2 — indirect CO2e emissions from purchased electricity per pod, in grams",
    POD_LABELS,
    registry=REGISTRY,
)
POD_EMBODIED_CO2 = Gauge(
    "greenkube_pod_embodied_co2e_grams",
    "GHG Scope 3 (Cat. 1) — upstream hardware manufacturing CO2e per pod, in grams",
    POD_LABELS,
    registry=REGISTRY,
)
POD_COST = Gauge(
    "greenkube_pod_cost_dollars",
    "Cloud cost per pod in dollars",
    POD_LABELS,
    registry=REGISTRY,
)
POD_ENERGY = Gauge(
    "greenkube_pod_energy_joules",
    "Energy consumption per pod in Joules",
    POD_LABELS,
    registry=REGISTRY,
)
POD_CPU_REQUEST = Gauge(
    "greenkube_pod_cpu_request_millicores",
    "CPU request per pod in millicores",
    POD_LABELS,
    registry=REGISTRY,
)
POD_CPU_USAGE = Gauge(
    "greenkube_pod_cpu_usage_millicores",
    "Actual CPU usage per pod in millicores",
    POD_LABELS,
    registry=REGISTRY,
)
POD_MEMORY_REQUEST = Gauge(
    "greenkube_pod_memory_request_bytes",
    "Memory request per pod in bytes",
    POD_LABELS,
    registry=REGISTRY,
)
POD_MEMORY_USAGE = Gauge(
    "greenkube_pod_memory_usage_bytes",
    "Actual memory usage per pod in bytes",
    POD_LABELS,
    registry=REGISTRY,
)
POD_PUE = Gauge(
    "greenkube_pue",
    "Power Usage Effectiveness per pod measurement",
    POD_LABELS,
    registry=REGISTRY,
)
POD_GRID_INTENSITY = Gauge(
    "greenkube_grid_intensity_gco2_kwh",
    "Grid carbon intensity in gCO2e/kWh per pod measurement",
    POD_LABELS,
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Namespace-level aggregate gauges
# ---------------------------------------------------------------------------
NS_LABELS = ["cluster", "namespace"]

NS_CO2_TOTAL = Gauge(
    "greenkube_namespace_co2e_grams_total",
    "GHG Scope 2 — total electricity CO2e per namespace in grams",
    NS_LABELS,
    registry=REGISTRY,
)
NS_EMBODIED_CO2_TOTAL = Gauge(
    "greenkube_namespace_embodied_co2e_grams_total",
    "GHG Scope 3 (Cat. 1) — total hardware manufacturing CO2e per namespace in grams",
    NS_LABELS,
    registry=REGISTRY,
)
NS_COST_TOTAL = Gauge(
    "greenkube_namespace_cost_dollars_total",
    "Total cost per namespace in dollars",
    NS_LABELS,
    registry=REGISTRY,
)
NS_ENERGY_TOTAL = Gauge(
    "greenkube_namespace_energy_joules_total",
    "Total energy per namespace in Joules",
    NS_LABELS,
    registry=REGISTRY,
)
NS_POD_COUNT = Gauge(
    "greenkube_namespace_pod_count",
    "Number of pods per namespace",
    NS_LABELS,
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Cluster-level summary gauges
# ---------------------------------------------------------------------------
CLUSTER_LABELS = ["cluster"]

CLUSTER_CO2_TOTAL = Gauge(
    "greenkube_cluster_co2e_grams_total",
    "GHG Scope 2 — total electricity CO2e across all pods in grams",
    CLUSTER_LABELS,
    registry=REGISTRY,
)
CLUSTER_EMBODIED_CO2_TOTAL = Gauge(
    "greenkube_cluster_embodied_co2e_grams_total",
    "GHG Scope 3 (Cat. 1) — total hardware manufacturing CO2e across all pods in grams",
    CLUSTER_LABELS,
    registry=REGISTRY,
)
CLUSTER_COST_TOTAL = Gauge(
    "greenkube_cluster_cost_dollars_total",
    "Total cost across all pods in dollars",
    CLUSTER_LABELS,
    registry=REGISTRY,
)
CLUSTER_ENERGY_TOTAL = Gauge(
    "greenkube_cluster_energy_joules_total",
    "Total energy across all pods in Joules",
    CLUSTER_LABELS,
    registry=REGISTRY,
)
CLUSTER_POD_COUNT = Gauge(
    "greenkube_cluster_pod_count",
    "Total number of unique pods in latest collection",
    CLUSTER_LABELS,
    registry=REGISTRY,
)
CLUSTER_NAMESPACE_COUNT = Gauge(
    "greenkube_cluster_namespace_count",
    "Total number of unique namespaces in latest collection",
    CLUSTER_LABELS,
    registry=REGISTRY,
)

# Node idle share (unreserved capacity, ``__idle__`` rows) of the latest collection, and the
# cluster totals = allocated (greenkube_cluster_*_total) + idle (WP-05.3, NRG-02). Absent when
# the idle history does not cover the snapshot. Never add an idle gauge to a with_idle gauge.
_IDLE_NOTE = "Absent when the idle history does not cover the latest collection."
CLUSTER_IDLE_CO2 = Gauge(
    "greenkube_cluster_idle_co2e_grams",
    "GHG Scope 2 CO2e of the node idle share (unreserved capacity, no pod) in grams, latest collection. "
    "Not included in greenkube_cluster_co2e_grams_total. " + _IDLE_NOTE,
    CLUSTER_LABELS,
    registry=REGISTRY,
)
CLUSTER_IDLE_EMBODIED_CO2 = Gauge(
    "greenkube_cluster_idle_embodied_co2e_grams",
    "GHG Scope 3 (Cat. 1) CO2e of the node idle share in grams, latest collection. " + _IDLE_NOTE,
    CLUSTER_LABELS,
    registry=REGISTRY,
)
CLUSTER_IDLE_COST = Gauge(
    "greenkube_cluster_idle_cost_dollars",
    "Cost of the node idle share in dollars, latest collection. " + _IDLE_NOTE,
    CLUSTER_LABELS,
    registry=REGISTRY,
)
CLUSTER_IDLE_ENERGY = Gauge(
    "greenkube_cluster_idle_energy_joules",
    "Energy of the node idle share in Joules, latest collection. " + _IDLE_NOTE,
    CLUSTER_LABELS,
    registry=REGISTRY,
)
CLUSTER_WITH_IDLE_CO2 = Gauge(
    "greenkube_cluster_with_idle_co2e_grams",
    "Cluster total GHG Scope 2 CO2e in grams: allocated to pods plus the node idle share. " + _IDLE_NOTE,
    CLUSTER_LABELS,
    registry=REGISTRY,
)
CLUSTER_WITH_IDLE_EMBODIED_CO2 = Gauge(
    "greenkube_cluster_with_idle_embodied_co2e_grams",
    "Cluster total GHG Scope 3 (Cat. 1) CO2e in grams: allocated plus the node idle share. " + _IDLE_NOTE,
    CLUSTER_LABELS,
    registry=REGISTRY,
)
CLUSTER_WITH_IDLE_COST = Gauge(
    "greenkube_cluster_with_idle_cost_dollars",
    "Cluster total cost in dollars: allocated to pods plus the node idle share. " + _IDLE_NOTE,
    CLUSTER_LABELS,
    registry=REGISTRY,
)
CLUSTER_WITH_IDLE_ENERGY = Gauge(
    "greenkube_cluster_with_idle_energy_joules",
    "Cluster total energy in Joules: allocated to pods plus the node idle share. " + _IDLE_NOTE,
    CLUSTER_LABELS,
    registry=REGISTRY,
)
#: (idle gauge, cluster-total gauge, CombinedMetric / NodeIdleMetric attribute) per measure.
_IDLE_GAUGES = (
    (CLUSTER_IDLE_CO2, CLUSTER_WITH_IDLE_CO2, "co2e_grams"),
    (CLUSTER_IDLE_EMBODIED_CO2, CLUSTER_WITH_IDLE_EMBODIED_CO2, "embodied_co2e_grams"),
    (CLUSTER_IDLE_COST, CLUSTER_WITH_IDLE_COST, "total_cost"),
    (CLUSTER_IDLE_ENERGY, CLUSTER_WITH_IDLE_ENERGY, "joules"),
)

# ---------------------------------------------------------------------------
# Dashboard summary gauges
# ---------------------------------------------------------------------------
DASHBOARD_NAMESPACE_ALL = "__all__"
DASHBOARD_NAMESPACE_CLUSTER = "_cluster"
DASHBOARD_SUMMARY_LABELS = ["cluster", "window", "namespace"]
DASHBOARD_SUMMARY_CO2_LABELS = ["cluster", "window", "namespace", "scope"]
DASHBOARD_SAVINGS_LABELS = ["cluster", "window", "namespace", "recommendation_type"]
DASHBOARD_WINDOW_ALIASES = {
    "1h": ("1h", "3600s"),
    "6h": ("6h", "21600s"),
    "24h": ("24h", "86400s"),
    "7d": ("7d", "604800s"),
    "30d": ("30d", "2592000s"),
    "1y": ("1y", "31536000s"),
    "ytd": ("ytd",),
}

DASHBOARD_SUMMARY_CO2 = Gauge(
    "greenkube_dashboard_summary_co2e_grams_total",
    "Pre-computed dashboard CO2e totals for a fixed time window. Scope is one of scope2, scope3, all.",
    DASHBOARD_SUMMARY_CO2_LABELS,
    registry=REGISTRY,
)
DASHBOARD_SUMMARY_COST = Gauge(
    "greenkube_dashboard_summary_cost_dollars_total",
    "Pre-computed dashboard cloud cost total for a fixed time window.",
    DASHBOARD_SUMMARY_LABELS,
    registry=REGISTRY,
)
DASHBOARD_SUMMARY_ENERGY = Gauge(
    "greenkube_dashboard_summary_energy_joules_total",
    "Pre-computed dashboard energy total for a fixed time window.",
    DASHBOARD_SUMMARY_LABELS,
    registry=REGISTRY,
)
DASHBOARD_SUMMARY_POD_COUNT = Gauge(
    "greenkube_dashboard_summary_pod_count",
    "Pre-computed dashboard pod count for a fixed time window.",
    DASHBOARD_SUMMARY_LABELS,
    registry=REGISTRY,
)
DASHBOARD_SUMMARY_NAMESPACE_COUNT = Gauge(
    "greenkube_dashboard_summary_namespace_count",
    "Pre-computed dashboard namespace count for a fixed time window.",
    DASHBOARD_SUMMARY_LABELS,
    registry=REGISTRY,
)
DASHBOARD_SAVINGS_CO2 = Gauge(
    "greenkube_dashboard_savings_co2e_grams_total",
    "Realized (measured) CO2e savings accrued during a fixed dashboard time window.",
    DASHBOARD_SAVINGS_LABELS,
    registry=REGISTRY,
)
DASHBOARD_SAVINGS_COST = Gauge(
    "greenkube_dashboard_savings_cost_dollars_total",
    "Realized (measured) cost savings accrued during a fixed dashboard time window.",
    DASHBOARD_SAVINGS_LABELS,
    registry=REGISTRY,
)
DASHBOARD_SAVINGS_ACCRUED_CO2 = Gauge(
    "greenkube_dashboard_savings_accrued_co2e_grams",
    "CO2e savings accrued during a fixed dashboard time window, by method. Never sum methods.",
    DASHBOARD_SAVINGS_LABELS + ["method"],
    registry=REGISTRY,
)
DASHBOARD_SAVINGS_ACCRUED_COST = Gauge(
    "greenkube_dashboard_savings_accrued_cost_dollars",
    "Cost savings accrued during a fixed dashboard time window, by method. Never sum methods.",
    DASHBOARD_SAVINGS_LABELS + ["method"],
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# GreenKube self-monitoring gauges
# ---------------------------------------------------------------------------
GREENKUBE_ESTIMATED_METRICS_RATIO = Gauge(
    "greenkube_estimated_metrics_ratio",
    "Fraction of metrics that rely on estimated values (0.0 = all measured, 1.0 = all estimated)",
    ["cluster", "namespace"],
    registry=REGISTRY,
)
GREENKUBE_LAST_COLLECTION_TIMESTAMP = Gauge(
    "greenkube_last_collection_timestamp_seconds",
    "Unix timestamp of the most recent metric in the database",
    ["cluster"],
    registry=REGISTRY,
)
GREENKUBE_METRICS_TOTAL = Gauge(
    "greenkube_metrics_total",
    "Total number of combined metric records in the latest window",
    ["cluster"],
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Sustainability Golden Signal gauges
# ---------------------------------------------------------------------------
CARBON_INTENSITY_SCORE = Gauge(
    "greenkube_carbon_intensity_score",
    "Energy-weighted average grid carbon intensity across the cluster (gCO2e/kWh). "
    "Lower is better — kept for backward compatibility.",
    ["cluster"],
    registry=REGISTRY,
)
CARBON_INTENSITY_ZONE = Gauge(
    "greenkube_carbon_intensity_zone",
    "Current grid carbon intensity per electricity zone (gCO2e/kWh)",
    ["cluster", "zone"],
    registry=REGISTRY,
)
ZONE_GRID_INTENSITY_MAP = Gauge(
    "greenkube_zone_grid_intensity_gco2_kwh",
    "Zone-level grid carbon intensity with node membership labels for Grafana map bubbles.",
    ["cluster", "zone", "lookup", "nodes", "node_count", "bubble_size", "bubble_label", "map_label"],
    registry=REGISTRY,
)
SUSTAINABILITY_SCORE = Gauge(
    "greenkube_sustainability_score",
    "Composite sustainability score (0-100, higher is better). "
    "Aggregates resource efficiency, carbon intensity, waste elimination, "
    "node efficiency, scaling practices, carbon-aware scheduling, and stability.",
    ["cluster", "namespace"],
    registry=REGISTRY,
)
SUSTAINABILITY_DIMENSION_SCORE = Gauge(
    "greenkube_sustainability_dimension_score",
    "Per-dimension sustainability score (0-100, higher is better)",
    ["cluster", "namespace", "dimension"],
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Node-level gauges
# ---------------------------------------------------------------------------
NODE_LABELS = ["node", "instance_type", "zone", "region", "cloud_provider", "architecture"]

NODE_CPU_CAPACITY = Gauge(
    "greenkube_node_cpu_capacity_millicores",
    "CPU capacity per node in millicores",
    NODE_LABELS,
    registry=REGISTRY,
)
NODE_MEMORY_CAPACITY = Gauge(
    "greenkube_node_memory_capacity_bytes",
    "Memory capacity per node in bytes",
    NODE_LABELS,
    registry=REGISTRY,
)
NODE_EMBODIED = Gauge(
    "greenkube_node_embodied_emissions_kg",
    "GHG Scope 3 (Cat. 1) — hardware manufacturing embodied emissions per node in kgCO2e",
    NODE_LABELS,
    registry=REGISTRY,
)
NODE_INFO = Gauge(
    "greenkube_node_info",
    "Node metadata info gauge (always 1)",
    NODE_LABELS,
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Recommendation gauges (existing)
# ---------------------------------------------------------------------------
RECOMMENDATION_COUNT = Gauge(
    "greenkube_recommendations_total",
    "Number of active recommendations by type and priority",
    ["cluster", "namespace", "type", "priority"],
    registry=REGISTRY,
)
RECOMMENDATION_SAVINGS_COST = Gauge(
    "greenkube_recommendations_savings_cost_dollars",
    "Total potential cost savings from recommendations by type",
    ["cluster", "type"],
    registry=REGISTRY,
)
RECOMMENDATION_SAVINGS_CO2 = Gauge(
    "greenkube_recommendations_savings_co2e_grams",
    "Total potential CO2e savings from recommendations by type",
    ["cluster", "type"],
    registry=REGISTRY,
)
TOP_RECOMMENDATIONS = Gauge(
    "greenkube_top_recommendations",
    "Ranked active recommendations by projected annual savings. "
    "sort_metric is the ranking basis; value_metric is the exposed savings value.",
    ["cluster", "rank", "sort_metric", "value_metric", "namespace", "type", "resource", "scope", "priority"],
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# Per-namespace recommendation savings gauges
# ---------------------------------------------------------------------------
NS_REC_SAVINGS_CO2 = Gauge(
    "greenkube_namespace_recommendation_savings_co2e_grams_total",
    "Total potential CO2e savings from active recommendations targeting this namespace",
    ["cluster", "namespace"],
    registry=REGISTRY,
)
NS_REC_SAVINGS_COST = Gauge(
    "greenkube_namespace_recommendation_savings_cost_dollars_total",
    "Total potential cost savings from active recommendations targeting this namespace",
    ["cluster", "namespace"],
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Savings ledger metrics (DB-backed)
#
# The ``*_attributed_*_total`` and ``*_reversed_*_total`` families are monotonic
# counters computed from the append-only, hash-chained ledger v2 (LED-04:
# positive entries are attributed, negative entries such as supersession,
# drift and restatement reversals are reversed; the cost of reliability fixes
# is a cost, never a saving, and is left out), labelled by ``method``
# (projected, measured, user_declared). Net savings = attributed − reversed.
# Never sum methods: only ``method="measured"`` is realized savings.
# ---------------------------------------------------------------------------
SAVINGS_COUNTER_LABELS = ["cluster", "recommendation_type", "method"]


class _SavingsLedgerCounterCollector:
    """Expose the latest ledger counter snapshot as Prometheus counters."""

    _FAMILIES = (
        (
            "greenkube_co2e_savings_attributed_grams",
            "Cumulative CO2e (grams) accrued in the savings ledger, by method. Monotonic.",
            "accrued_co2e_grams",
        ),
        (
            "greenkube_cost_savings_attributed_dollars",
            "Cumulative cost (dollars) accrued in the savings ledger, by method; the cost of reliability fixes "
            "is not a saving and is left out. Monotonic.",
            "accrued_cost_dollars",
        ),
        (
            "greenkube_co2e_savings_reversed_grams",
            "Cumulative CO2e (grams) reversed: negative ledger entries (supersession, drift, restatement). Monotonic.",
            "reversed_co2e_grams",
        ),
        (
            "greenkube_cost_savings_reversed_dollars",
            "Cumulative cost reversed: negative savings entries (supersession, drift, restatement); the cost of "
            "reliability fixes is not counted. Monotonic.",
            "reversed_cost_dollars",
        ),
    )

    def __init__(self) -> None:
        self._snapshot: list[tuple[str, SavingsCounterTotal]] = []

    def update(self, cluster: str, counters: Sequence[SavingsCounterTotal]) -> None:
        self._snapshot = [(cluster, counter) for counter in counters]

    def describe(self):
        return [
            CounterMetricFamily(name, documentation, labels=SAVINGS_COUNTER_LABELS)
            for name, documentation, _ in self._FAMILIES
        ]

    def collect(self):
        snapshot = list(self._snapshot)
        for name, documentation, attribute in self._FAMILIES:
            family = CounterMetricFamily(name, documentation, labels=SAVINGS_COUNTER_LABELS)
            for cluster, counter in snapshot:
                family.add_metric(
                    [cluster, counter.recommendation_type, counter.method],
                    max(0.0, float(getattr(counter, attribute))),
                )
            yield family


#: Ledger clusters whose savings metrics already read ledger v2 in this process (the switch is one-way).
_LEDGER_V2_COUNTERS: dict[str, str] = {}


async def savings_counter_source(ledger_repo, ledger_cluster: str) -> Optional[str]:
    """The ledger v2 cluster uid the savings counters and gauges read, or None to keep the v1 ledger.

    The counters switch to ledger v2 only once its accrued book reconciles
    with the v1 rows (the projector caught up), so the switch never shows as a
    drop; afterwards they stay on v2 in this process even while the projector
    catches up a new row (review WP-09).
    """
    known = _LEDGER_V2_COUNTERS.get(ledger_cluster)
    if known is not None:
        return known
    identity = await ledger_repo.get_identity(ledger_cluster)
    if identity is None:
        logger.info("Savings ledger v2 has no cluster identity yet: savings metrics read the v1 ledger.")
        return None
    from ..verify.ledger.reconciliation import reconcile

    if not await reconcile(ledger_repo, identity[0], ledger_cluster):
        logger.info("Savings ledger v2 does not reconcile with the v1 ledger yet: savings metrics read the v1 ledger.")
        return None
    _LEDGER_V2_COUNTERS[ledger_cluster] = identity[0]
    return identity[0]


SAVINGS_LEDGER_COUNTERS = _SavingsLedgerCounterCollector()
REGISTRY.register(SAVINGS_LEDGER_COUNTERS)

SAVINGS_MEASURED_RATIO = Gauge(
    "greenkube_savings_measured_vs_projected_ratio",
    "Share of net accrued CO2e savings that is measured rather than projected (0–1), "
    "both accrued on the allocated basis since ledger/2.",
    ["cluster"],
    registry=REGISTRY,
)
SAVINGS_RUN_RATE_CO2 = Gauge(
    "greenkube_savings_run_rate_annual_co2e_grams",
    "Current annual CO2e savings rate of applied recommendations, by method (projected, measured, user_declared).",
    ["cluster", "method"],
    registry=REGISTRY,
)
SAVINGS_RUN_RATE_COST = Gauge(
    "greenkube_savings_run_rate_annual_cost_dollars",
    "Current annual cost savings rate of applied recommendations, by method (projected, measured, user_declared).",
    ["cluster", "method"],
    registry=REGISTRY,
)
# Legacy gauges kept for backward compatibility — show the total annual
# projection from applied recommendations (not window-aware).
CLUSTER_CO2_SAVED = Gauge(
    "greenkube_cluster_co2e_saved_grams_total",
    "Annual projected CO2e savings (grams/year) of applied recommendations still accruing as projected.",
    ["cluster"],
    registry=REGISTRY,
)
CLUSTER_COST_SAVED = Gauge(
    "greenkube_cluster_cost_saved_dollars_total",
    "Annual projected cost savings (dollars/year) of applied recommendations still accruing as projected.",
    ["cluster"],
    registry=REGISTRY,
)
RECOMMENDATIONS_IMPLEMENTED = Gauge(
    "greenkube_recommendations_implemented_total",
    "Number of recommendations marked as applied, by recommendation type",
    ["cluster", "namespace", "type"],
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Pod efficiency ratios
# ---------------------------------------------------------------------------
POD_CPU_EFFICIENCY = Gauge(
    "greenkube_pod_cpu_efficiency_ratio",
    "CPU usage / CPU request ratio per pod (0.0–1.0, capped). Low values indicate overprovisioning.",
    POD_LABELS,
    registry=REGISTRY,
)
POD_MEMORY_EFFICIENCY = Gauge(
    "greenkube_pod_memory_efficiency_ratio",
    "Memory usage / memory request ratio per pod (0.0–1.0, capped). Low values indicate overprovisioning.",
    POD_LABELS,
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Pod stability gauge
# ---------------------------------------------------------------------------
POD_RESTART_COUNT = Gauge(
    "greenkube_pod_restart_count",
    "Total container restart count for the pod",
    POD_LABELS,
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Node allocation gauges (sum of pod requests per node)
# ---------------------------------------------------------------------------
NODE_CPU_ALLOCATED = Gauge(
    "greenkube_node_cpu_allocated_millicores",
    "Sum of CPU requests (millicores) of all pods scheduled on the node",
    NODE_LABELS,
    registry=REGISTRY,
)
NODE_MEMORY_ALLOCATED = Gauge(
    "greenkube_node_memory_allocated_bytes",
    "Sum of memory requests (bytes) of all pods scheduled on the node",
    NODE_LABELS,
    registry=REGISTRY,
)

# Module-level cache: populated by update_cluster_metrics(), consumed by update_node_metrics()
_node_cpu_allocated: dict[str, int] = {}
_node_memory_allocated: dict[str, int] = {}


# ---------------------------------------------------------------------------
# Update helpers
# ---------------------------------------------------------------------------


def _clear_gauge(gauge: Gauge) -> None:
    """Clear all label combinations from a gauge."""
    gauge._metrics.clear()


# Savings accrue on one-hour slots and run rates only move when a recommendation
# changes state, so scrapes within this many seconds reuse the previous savings
# refresh instead of reloading applied recommendations and scanning the ledger.
SAVINGS_REFRESH_TTL_SECONDS = 60.0
_savings_refreshed_at: "weakref.WeakKeyDictionary[object, dict[str, float]]" = weakref.WeakKeyDictionary()


def _savings_refresh_due(source: object, key: str) -> bool:
    """Return True when the savings gauges read from ``source`` are stale."""
    try:
        last = _savings_refreshed_at.get(source, {}).get(key)
    except TypeError:
        return True
    return last is None or time.monotonic() - last >= SAVINGS_REFRESH_TTL_SECONDS


def _mark_savings_refreshed(source: object, key: str) -> None:
    try:
        _savings_refreshed_at.setdefault(source, {})[key] = time.monotonic()
    except TypeError:
        pass  # not weak-referenceable: refresh on every scrape


def reset_savings_refresh_cache() -> None:
    """Force the next scrape to reload every savings gauge."""
    _savings_refreshed_at.clear()


def clear_dashboard_summary_metrics() -> None:
    """Clear all pre-computed dashboard summary Prometheus gauges."""
    for gauge in (
        DASHBOARD_SUMMARY_CO2,
        DASHBOARD_SUMMARY_COST,
        DASHBOARD_SUMMARY_ENERGY,
        DASHBOARD_SUMMARY_POD_COUNT,
        DASHBOARD_SUMMARY_NAMESPACE_COUNT,
    ):
        _clear_gauge(gauge)


def clear_dashboard_savings_metrics() -> None:
    """Clear all DB-backed dashboard savings Prometheus gauges."""
    _clear_gauge(DASHBOARD_SAVINGS_CO2)
    _clear_gauge(DASHBOARD_SAVINGS_COST)
    _clear_gauge(DASHBOARD_SAVINGS_ACCRUED_CO2)
    _clear_gauge(DASHBOARD_SAVINGS_ACCRUED_COST)


def _dashboard_window_labels(window_slug: str) -> tuple[str, ...]:
    """Return all Prometheus label values that should resolve to a summary window."""
    return DASHBOARD_WINDOW_ALIASES.get(window_slug, (window_slug,))


def _dashboard_window_ranges(now: datetime) -> tuple[tuple[str, datetime, datetime], ...]:
    """Return canonical dashboard summary windows as exact UTC ranges."""
    ytd_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return (
        ("1h", now - timedelta(hours=1), now),
        ("6h", now - timedelta(hours=6), now),
        ("24h", now - timedelta(hours=24), now),
        ("7d", now - timedelta(days=7), now),
        ("30d", now - timedelta(days=30), now),
        ("ytd", ytd_start, now),
        ("1y", now - timedelta(days=365), now),
    )


def _compact_node_names(node_names: list[str], limit: int = 8) -> str:
    """Return a bounded node list suitable for Prometheus labels and Grafana tooltips."""
    if len(node_names) <= limit:
        return ", ".join(node_names)
    visible = ", ".join(node_names[:limit])
    return f"{visible}, +{len(node_names) - limit} more"


def _bubble_size_for_node_count(node_count: int) -> float:
    """Scale map bubbles logarithmically so tiny and huge clusters remain readable."""
    return round(max(14.0, min(56.0, 10.0 + log2(node_count + 1) * 6.0)), 2)


def update_dashboard_summary_metrics(rows: List[MetricsSummaryRow], reset: bool = False) -> None:
    """Expose pre-computed dashboard summary rows as Prometheus gauges."""
    if reset:
        clear_dashboard_summary_metrics()

    cluster = _get_cluster_name()
    for row in rows:
        namespace = row.namespace or DASHBOARD_NAMESPACE_ALL
        for window_label in _dashboard_window_labels(row.window_slug):
            labels = {"cluster": cluster, "window": window_label, "namespace": namespace}

            DASHBOARD_SUMMARY_CO2.labels(**labels, scope="scope2").set(row.total_co2e_grams)
            DASHBOARD_SUMMARY_CO2.labels(**labels, scope="scope3").set(row.total_embodied_co2e_grams)
            DASHBOARD_SUMMARY_CO2.labels(**labels, scope="all").set(row.total_co2e_all_scopes)
            DASHBOARD_SUMMARY_COST.labels(**labels).set(row.total_cost)
            DASHBOARD_SUMMARY_ENERGY.labels(**labels).set(row.total_energy_joules)
            DASHBOARD_SUMMARY_POD_COUNT.labels(**labels).set(row.pod_count)
            DASHBOARD_SUMMARY_NAMESPACE_COUNT.labels(**labels).set(row.namespace_count)


def update_dashboard_savings_metrics(
    window_slug: str,
    totals_by_type: dict[str, dict[str, float]],
    cluster: str | None = None,
    namespace: str | None = None,
    reset: bool = False,
) -> None:
    """Expose exact DB-backed savings totals for a dashboard time window."""
    if reset:
        clear_dashboard_savings_metrics()

    cluster = cluster if cluster is not None else _get_cluster_name()
    namespace_label = namespace or DASHBOARD_NAMESPACE_ALL
    total_co2e = sum(totals.get("co2e_saved_grams", 0.0) for totals in totals_by_type.values())
    total_cost = sum(totals.get("cost_saved_dollars", 0.0) for totals in totals_by_type.values())

    for window_label in _dashboard_window_labels(window_slug):
        labels = {"cluster": cluster, "window": window_label, "namespace": namespace_label}
        DASHBOARD_SAVINGS_CO2.labels(**labels, recommendation_type="all").set(total_co2e)
        DASHBOARD_SAVINGS_COST.labels(**labels, recommendation_type="all").set(total_cost)

        for recommendation_type, totals in totals_by_type.items():
            DASHBOARD_SAVINGS_CO2.labels(**labels, recommendation_type=recommendation_type).set(
                totals.get("co2e_saved_grams", 0.0)
            )
            DASHBOARD_SAVINGS_COST.labels(**labels, recommendation_type=recommendation_type).set(
                totals.get("cost_saved_dollars", 0.0)
            )


def update_dashboard_savings_from_breakdown(
    window_slug: str,
    breakdown: Sequence[SavingsAccrualTotal],
    *,
    cluster: str | None = None,
    namespaces: Sequence[str] = (),
) -> None:
    """Expose ledger accruals of one dashboard window.

    ``greenkube_dashboard_savings_*_total`` carry realized (measured) savings
    only; every method is exposed separately on
    ``greenkube_dashboard_savings_accrued_*`` with a ``method`` label.
    """
    cluster = cluster if cluster is not None else _get_cluster_name()
    rows_by_scope: dict[str, list[SavingsAccrualTotal]] = defaultdict(list)
    for row in breakdown:
        rows_by_scope[DASHBOARD_NAMESPACE_ALL].append(row)
        rows_by_scope[row.namespace or DASHBOARD_NAMESPACE_CLUSTER].append(row)
    scopes = list(dict.fromkeys([DASHBOARD_NAMESPACE_ALL, DASHBOARD_NAMESPACE_CLUSTER, *namespaces, *rows_by_scope]))

    for scope in scopes:
        rows = rows_by_scope.get(scope, [])
        realized: dict[str, dict[str, float]] = defaultdict(
            lambda: {"co2e_saved_grams": 0.0, "cost_saved_dollars": 0.0}
        )
        by_method: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
        for row in rows:
            if row.method == METHOD_MEASURED:
                realized[row.recommendation_type]["co2e_saved_grams"] += row.co2e_saved_grams
                realized[row.recommendation_type]["cost_saved_dollars"] += row.cost_saved_dollars
            for type_label in ("all", row.recommendation_type):
                by_method[(type_label, row.method)][0] += row.co2e_saved_grams
                by_method[(type_label, row.method)][1] += row.cost_saved_dollars
        update_dashboard_savings_metrics(window_slug, dict(realized), cluster=cluster, namespace=scope)
        if rows or scope == DASHBOARD_NAMESPACE_ALL:
            # Explicit zeros per method keep panels at 0 instead of "No data".
            for method in SAVINGS_METHODS:
                by_method.setdefault(("all", method), [0.0, 0.0])
        for window_label in _dashboard_window_labels(window_slug):
            labels = {"cluster": cluster, "window": window_label, "namespace": scope}
            for (type_label, method), (co2e, cost) in by_method.items():
                DASHBOARD_SAVINGS_ACCRUED_CO2.labels(**labels, recommendation_type=type_label, method=method).set(co2e)
                DASHBOARD_SAVINGS_ACCRUED_COST.labels(**labels, recommendation_type=type_label, method=method).set(cost)


def update_cluster_metrics(metrics: List[CombinedMetric]) -> None:
    """Update all pod-level, namespace-level, and cluster-level Prometheus gauges.

    Args:
        metrics: The latest list of CombinedMetric objects from all pods.
    """
    cluster = _get_cluster_name()

    # Clear previous values
    for g in (
        POD_CO2,
        POD_EMBODIED_CO2,
        POD_COST,
        POD_ENERGY,
        POD_CPU_REQUEST,
        POD_CPU_USAGE,
        POD_MEMORY_REQUEST,
        POD_MEMORY_USAGE,
        POD_PUE,
        POD_GRID_INTENSITY,
        POD_RESTART_COUNT,
        POD_CPU_EFFICIENCY,
        POD_MEMORY_EFFICIENCY,
        NS_CO2_TOTAL,
        NS_EMBODIED_CO2_TOTAL,
        NS_COST_TOTAL,
        NS_ENERGY_TOTAL,
        NS_POD_COUNT,
        CARBON_INTENSITY_SCORE,
        CARBON_INTENSITY_ZONE,
        ZONE_GRID_INTENSITY_MAP,
        GREENKUBE_ESTIMATED_METRICS_RATIO,
        SUSTAINABILITY_SCORE,
        SUSTAINABILITY_DIMENSION_SCORE,
    ):
        _clear_gauge(g)

    if not metrics:
        CLUSTER_CO2_TOTAL.labels(cluster=cluster).set(0)
        CLUSTER_EMBODIED_CO2_TOTAL.labels(cluster=cluster).set(0)
        CLUSTER_COST_TOTAL.labels(cluster=cluster).set(0)
        CLUSTER_ENERGY_TOTAL.labels(cluster=cluster).set(0)
        CLUSTER_POD_COUNT.labels(cluster=cluster).set(0)
        CLUSTER_NAMESPACE_COUNT.labels(cluster=cluster).set(0)
        GREENKUBE_ESTIMATED_METRICS_RATIO.labels(cluster=cluster, namespace=DASHBOARD_NAMESPACE_ALL).set(0)
        GREENKUBE_METRICS_TOTAL.labels(cluster=cluster).set(0)
        CARBON_INTENSITY_SCORE.labels(cluster=cluster).set(0)
        SUSTAINABILITY_SCORE.labels(cluster=cluster, namespace=DASHBOARD_NAMESPACE_ALL).set(50)
        _node_cpu_allocated.clear()
        _node_memory_allocated.clear()
        return

    # Namespace aggregations
    ns_co2: dict[str, float] = defaultdict(float)
    ns_embodied: dict[str, float] = defaultdict(float)
    ns_cost: dict[str, float] = defaultdict(float)
    ns_energy: dict[str, float] = defaultdict(float)
    ns_pods: dict[str, set] = defaultdict(set)
    ns_metrics: dict[str, list[CombinedMetric]] = defaultdict(list)

    # Node allocation aggregations
    node_cpu_alloc: dict[str, int] = defaultdict(int)
    node_mem_alloc: dict[str, int] = defaultdict(int)

    # For sustainability golden signal: energy-weighted intensity
    total_weighted_intensity = 0.0
    total_energy = 0.0
    zone_intensities: dict[str, float] = {}
    zone_weighted_intensity: dict[str, float] = defaultdict(float)
    zone_energy: dict[str, float] = defaultdict(float)
    zone_nodes: dict[str, set[str]] = defaultdict(set)

    for m in metrics:
        region = m.emaps_zone or m.node_zone or ""
        labels = {
            "cluster": cluster,
            "namespace": m.namespace,
            "pod": m.pod_name,
            "node": m.node or "unknown",
            "region": region,
        }

        POD_CO2.labels(**labels).set(m.co2e_grams)
        POD_EMBODIED_CO2.labels(**labels).set(m.embodied_co2e_grams or 0.0)
        POD_COST.labels(**labels).set(m.total_cost)
        POD_ENERGY.labels(**labels).set(m.joules)
        POD_CPU_REQUEST.labels(**labels).set(m.cpu_request)
        # NULL usage means no sample at the step (COL-03): no series, never a 0.
        if m.cpu_usage_millicores is not None:
            POD_CPU_USAGE.labels(**labels).set(m.cpu_usage_millicores)
        POD_MEMORY_REQUEST.labels(**labels).set(m.memory_request)
        if m.memory_usage_bytes is not None:
            POD_MEMORY_USAGE.labels(**labels).set(m.memory_usage_bytes)
        POD_PUE.labels(**labels).set(m.pue)
        POD_GRID_INTENSITY.labels(**labels).set(m.grid_intensity)

        # Restart count
        if m.restart_count is not None:
            POD_RESTART_COUNT.labels(**labels).set(m.restart_count)

        # Efficiency ratios (cpu_usage / cpu_request, memory_usage / memory_request)
        if m.cpu_request and m.cpu_request > 0 and m.cpu_usage_millicores is not None:
            POD_CPU_EFFICIENCY.labels(**labels).set(min(1.0, m.cpu_usage_millicores / m.cpu_request))
        if m.memory_request and m.memory_request > 0 and m.memory_usage_bytes is not None:
            POD_MEMORY_EFFICIENCY.labels(**labels).set(min(1.0, m.memory_usage_bytes / m.memory_request))

        # Node allocation accumulation
        node_key = m.node or "unknown"
        node_cpu_alloc[node_key] += m.cpu_request
        node_mem_alloc[node_key] += m.memory_request

        # Accumulate namespace totals
        ns = m.namespace
        ns_co2[ns] += m.co2e_grams
        ns_embodied[ns] += m.embodied_co2e_grams or 0.0
        ns_cost[ns] += m.total_cost
        ns_energy[ns] += m.joules
        ns_pods[ns].add(m.pod_name)
        ns_metrics[ns].append(m)

        # Sustainability golden signal: accumulate for weighted average
        if m.grid_intensity > 0 and m.joules > 0:
            total_weighted_intensity += m.grid_intensity * m.joules
            total_energy += m.joules
        # Track per-zone intensity (last seen value per zone)
        zone = m.emaps_zone or m.node_zone
        if zone and m.grid_intensity > 0:
            zone_intensities[zone] = m.grid_intensity
            if m.joules > 0:
                zone_weighted_intensity[zone] += m.grid_intensity * m.joules
                zone_energy[zone] += m.joules
            zone_nodes[zone].add(m.node or "unknown")

    # Set namespace gauges
    for ns in ns_co2:
        NS_CO2_TOTAL.labels(cluster=cluster, namespace=ns).set(ns_co2[ns])
        NS_EMBODIED_CO2_TOTAL.labels(cluster=cluster, namespace=ns).set(ns_embodied[ns])
        NS_COST_TOTAL.labels(cluster=cluster, namespace=ns).set(ns_cost[ns])
        NS_ENERGY_TOTAL.labels(cluster=cluster, namespace=ns).set(ns_energy[ns])
        NS_POD_COUNT.labels(cluster=cluster, namespace=ns).set(len(ns_pods[ns]))

    # Publish node allocation maps for update_node_metrics() to consume
    _node_cpu_allocated.clear()
    _node_cpu_allocated.update(node_cpu_alloc)
    _node_memory_allocated.clear()
    _node_memory_allocated.update(node_mem_alloc)

    # Cluster totals
    CLUSTER_CO2_TOTAL.labels(cluster=cluster).set(sum(ns_co2.values()))
    CLUSTER_EMBODIED_CO2_TOTAL.labels(cluster=cluster).set(sum(ns_embodied.values()))
    CLUSTER_COST_TOTAL.labels(cluster=cluster).set(sum(ns_cost.values()))
    CLUSTER_ENERGY_TOTAL.labels(cluster=cluster).set(sum(ns_energy.values()))
    CLUSTER_POD_COUNT.labels(cluster=cluster).set(len({m.pod_name for m in metrics}))
    CLUSTER_NAMESPACE_COUNT.labels(cluster=cluster).set(len(ns_co2))

    # Self-monitoring metrics
    GREENKUBE_METRICS_TOTAL.labels(cluster=cluster).set(len(metrics))
    estimated_count = sum(1 for m in metrics if m.is_estimated)
    GREENKUBE_ESTIMATED_METRICS_RATIO.labels(cluster=cluster, namespace=DASHBOARD_NAMESPACE_ALL).set(
        estimated_count / len(metrics) if metrics else 0.0
    )
    for ns, items in ns_metrics.items():
        ns_estimated_count = sum(1 for item in items if item.is_estimated)
        GREENKUBE_ESTIMATED_METRICS_RATIO.labels(cluster=cluster, namespace=ns).set(
            ns_estimated_count / len(items) if items else 0.0
        )
    latest_ts = max((m.timestamp for m in metrics if m.timestamp), default=None)
    if latest_ts:
        GREENKUBE_LAST_COLLECTION_TIMESTAMP.labels(cluster=cluster).set(latest_ts.timestamp())

    # --- Sustainability Golden Signal ---
    # Energy-weighted average carbon intensity across the cluster
    weighted_avg = total_weighted_intensity / total_energy if total_energy > 0 else 0.0
    CARBON_INTENSITY_SCORE.labels(cluster=cluster).set(round(weighted_avg, 2))

    # Per-zone intensity
    for zone, intensity in zone_intensities.items():
        CARBON_INTENSITY_ZONE.labels(cluster=cluster, zone=zone).set(intensity)
        node_names = sorted(zone_nodes.get(zone, {"unknown"}))
        node_count = len(node_names)
        weighted_intensity = zone_weighted_intensity[zone] / zone_energy[zone] if zone_energy[zone] > 0 else intensity
        rounded_intensity = round(weighted_intensity)
        nodes_label = _compact_node_names(node_names)
        node_word = "node" if node_count == 1 else "nodes"
        lookup = zone.split("-", 1)[0] if zone else "unknown"
        bubble_label = f"{zone}\n{rounded_intensity:g} gCO₂/kWh\n{node_count} {node_word}"
        map_label = f"{zone} · {rounded_intensity:g} gCO₂/kWh · {node_count} {node_word} · {nodes_label}"
        ZONE_GRID_INTENSITY_MAP.labels(
            cluster=cluster,
            zone=zone,
            lookup=lookup,
            nodes=nodes_label,
            node_count=str(node_count),
            bubble_size=str(_bubble_size_for_node_count(node_count)),
            bubble_label=bubble_label,
            map_label=map_label,
        ).set(round(weighted_intensity, 2))

    # Comprehensive sustainability score (0-100, 100 = best)
    scorer = SustainabilityScorer()
    score_result = scorer.compute(metrics)
    SUSTAINABILITY_SCORE.labels(cluster=cluster, namespace=DASHBOARD_NAMESPACE_ALL).set(score_result.overall_score)
    for dim, dim_score in score_result.dimension_scores.items():
        SUSTAINABILITY_DIMENSION_SCORE.labels(cluster=cluster, namespace=DASHBOARD_NAMESPACE_ALL, dimension=dim).set(
            dim_score
        )
    for ns, items in ns_metrics.items():
        namespace_score = scorer.compute(items)
        SUSTAINABILITY_SCORE.labels(cluster=cluster, namespace=ns).set(namespace_score.overall_score)
        for dim, dim_score in namespace_score.dimension_scores.items():
            SUSTAINABILITY_DIMENSION_SCORE.labels(cluster=cluster, namespace=ns, dimension=dim).set(dim_score)

    logger.debug("Updated Prometheus cluster metrics with %d pod metrics.", len(metrics))


def update_node_metrics(nodes: List[NodeInfo]) -> None:
    """Update node-level Prometheus gauges with current node information.

    Args:
        nodes: The latest list of NodeInfo objects.
    """
    for g in (
        NODE_CPU_CAPACITY,
        NODE_MEMORY_CAPACITY,
        NODE_EMBODIED,
        NODE_INFO,
        NODE_CPU_ALLOCATED,
        NODE_MEMORY_ALLOCATED,
    ):
        _clear_gauge(g)

    for node in nodes:
        labels = {
            "node": node.name,
            "instance_type": node.instance_type or "unknown",
            "zone": node.zone or "unknown",
            "region": node.region or "unknown",
            "cloud_provider": node.cloud_provider or "unknown",
            "architecture": node.architecture or "unknown",
        }
        NODE_INFO.labels(**labels).set(1)
        if node.cpu_capacity_millicores is not None:
            NODE_CPU_CAPACITY.labels(**labels).set(node.cpu_capacity_millicores)
        if node.memory_capacity_bytes is not None:
            NODE_MEMORY_CAPACITY.labels(**labels).set(node.memory_capacity_bytes)
        NODE_EMBODIED.labels(**labels).set(node.embodied_emissions_kg or 0.0)

        # Pod allocation totals (populated by the most recent update_cluster_metrics call)
        if node.name in _node_cpu_allocated:
            NODE_CPU_ALLOCATED.labels(**labels).set(_node_cpu_allocated[node.name])
        if node.name in _node_memory_allocated:
            NODE_MEMORY_ALLOCATED.labels(**labels).set(_node_memory_allocated[node.name])

    logger.debug("Updated Prometheus node metrics with %d nodes.", len(nodes))


def update_recommendation_metrics(recommendations: Sequence[Recommendation | RecommendationRecord]) -> None:
    """Updates Prometheus gauges with current recommendation counts and savings.

    Clears previous values and sets new ones based on the provided list.

    Args:
        recommendations: The current list of active recommendations.
    """
    _clear_gauge(RECOMMENDATION_COUNT)
    _clear_gauge(RECOMMENDATION_SAVINGS_COST)
    _clear_gauge(RECOMMENDATION_SAVINGS_CO2)
    _clear_gauge(TOP_RECOMMENDATIONS)
    _clear_gauge(NS_REC_SAVINGS_CO2)
    _clear_gauge(NS_REC_SAVINGS_COST)

    if not recommendations:
        return

    cluster = _get_cluster_name()
    count_by_type_priority: dict[tuple[str, str], int] = defaultdict(int)
    count_by_namespace_type_priority: dict[tuple[str, str, str], int] = defaultdict(int)
    savings_cost_by_type: dict[str, float] = defaultdict(float)
    savings_co2_by_type: dict[str, float] = defaultdict(float)
    ns_savings_co2: dict[str, float] = defaultdict(float)
    ns_savings_cost: dict[str, float] = defaultdict(float)

    for rec in recommendations:
        rec_type = rec.type.value if hasattr(rec.type, "value") else str(rec.type)
        count_by_type_priority[(rec_type, rec.priority)] += 1
        ns = getattr(rec, "namespace", None) or DASHBOARD_NAMESPACE_CLUSTER
        count_by_namespace_type_priority[(ns, rec_type, rec.priority)] += 1
        savings_cost_by_type[rec_type] += rec.potential_savings_cost or 0.0
        savings_co2_by_type[rec_type] += rec.potential_savings_co2e_grams or 0.0
        ns_savings_co2[ns] += rec.potential_savings_co2e_grams or 0.0
        ns_savings_cost[ns] += rec.potential_savings_cost or 0.0

    for (rec_type, priority), count in count_by_type_priority.items():
        RECOMMENDATION_COUNT.labels(
            cluster=cluster,
            namespace=DASHBOARD_NAMESPACE_ALL,
            type=rec_type,
            priority=priority,
        ).set(count)

    for (namespace, rec_type, priority), count in count_by_namespace_type_priority.items():
        RECOMMENDATION_COUNT.labels(cluster=cluster, namespace=namespace, type=rec_type, priority=priority).set(count)

    for rec_type, cost in savings_cost_by_type.items():
        RECOMMENDATION_SAVINGS_COST.labels(cluster=cluster, type=rec_type).set(cost)

    for rec_type, co2 in savings_co2_by_type.items():
        RECOMMENDATION_SAVINGS_CO2.labels(cluster=cluster, type=rec_type).set(co2)

    for ns, co2 in ns_savings_co2.items():
        NS_REC_SAVINGS_CO2.labels(cluster=cluster, namespace=ns).set(co2)

    for ns, cost in ns_savings_cost.items():
        NS_REC_SAVINGS_COST.labels(cluster=cluster, namespace=ns).set(cost)

    for savings_metric in ("co2", "cost"):
        for rec in rank_recommendations(recommendations, limit=None, savings_metric=savings_metric):
            labels = {
                "cluster": cluster,
                "rank": str(rec.rank),
                "sort_metric": rec.sort_metric,
                "namespace": rec.namespace or DASHBOARD_NAMESPACE_CLUSTER,
                "type": rec.type.value if hasattr(rec.type, "value") else str(rec.type),
                "resource": rec.resource,
                "scope": rec.scope,
                "priority": rec.priority,
            }
            TOP_RECOMMENDATIONS.labels(**labels, value_metric="co2e_grams").set(rec.projected_savings_co2e_grams)
            TOP_RECOMMENDATIONS.labels(**labels, value_metric="cost_dollars").set(rec.projected_savings_cost)

    logger.debug("Updated Prometheus metrics with %d recommendations.", len(recommendations))


def update_attributed_savings_metrics(counters: Sequence[SavingsCounterTotal], cluster: str) -> None:
    """Publish the ledger's monotonic counters and the measured share.

    Args:
        counters: Output of ``SavingsLedgerRepository.get_lifetime_counters()``.
        cluster:  Cluster label value.
    """
    SAVINGS_LEDGER_COUNTERS.update(cluster, counters)
    _clear_gauge(SAVINGS_MEASURED_RATIO)
    net: dict[str, float] = defaultdict(float)
    for counter in counters:
        net[counter.method] += counter.accrued_co2e_grams - counter.reversed_co2e_grams
    measured = max(net.get(METHOD_MEASURED, 0.0), 0.0)
    projected = max(net.get(METHOD_PROJECTED, 0.0), 0.0)
    total = measured + projected
    SAVINGS_MEASURED_RATIO.labels(cluster=cluster).set(measured / total if total > 0 else 0.0)
    logger.debug("Updated savings ledger counters for cluster=%s: %d groups.", cluster, len(counters))


def update_savings_run_rate_metrics(applied_records: Sequence[RecommendationRecord]) -> None:
    """Publish current annual savings rates by method.

    ``greenkube_cluster_*_saved_*_total`` (legacy) keep the projected annual
    rate only, so recommendations that were not realized, are inconclusive or
    are in rollback review no longer count.
    """
    cluster = _get_cluster_name()
    rates = annual_rates_by_method(applied_records)
    for gauge in (SAVINGS_RUN_RATE_CO2, SAVINGS_RUN_RATE_COST, CLUSTER_CO2_SAVED, CLUSTER_COST_SAVED):
        _clear_gauge(gauge)
    for method in SAVINGS_METHODS:
        values = rates.get(method, {})
        SAVINGS_RUN_RATE_CO2.labels(cluster=cluster, method=method).set(values.get("co2e_grams", 0.0))
        SAVINGS_RUN_RATE_COST.labels(cluster=cluster, method=method).set(values.get("cost", 0.0))
    projected = rates.get(METHOD_PROJECTED, {})
    CLUSTER_CO2_SAVED.labels(cluster=cluster).set(projected.get("co2e_grams", 0.0))
    CLUSTER_COST_SAVED.labels(cluster=cluster).set(projected.get("cost", 0.0))


def update_realized_savings_metrics(applied_records: List[RecommendationRecord]) -> None:
    """Update annual savings-rate gauges and implemented counts from applied records.

    Args:
        applied_records: Applied recommendation records (any applied state).
    """
    _clear_gauge(RECOMMENDATIONS_IMPLEMENTED)
    update_savings_run_rate_metrics(applied_records)

    cluster = _get_cluster_name()
    implemented_by_type: dict[str, int] = defaultdict(int)
    implemented_by_namespace_type: dict[tuple[str, str], int] = defaultdict(int)
    for r in applied_records:
        rec_type = r.type.value if hasattr(r.type, "value") else str(r.type)
        implemented_by_type[rec_type] += 1
        namespace = r.namespace or "_cluster"
        implemented_by_namespace_type[(namespace, rec_type)] += 1

    for rec_type, count in implemented_by_type.items():
        RECOMMENDATIONS_IMPLEMENTED.labels(cluster=cluster, namespace=DASHBOARD_NAMESPACE_ALL, type=rec_type).set(count)
    for (namespace, rec_type), count in implemented_by_namespace_type.items():
        RECOMMENDATIONS_IMPLEMENTED.labels(cluster=cluster, namespace=namespace, type=rec_type).set(count)

    logger.debug("Updated savings rate metrics for %d applied recommendations.", len(applied_records))


def update_realized_savings_from_stats(stats_rows: List[dict]) -> None:
    """Update the implemented-recommendation counts from pre-aggregated SQL stats.

    Accepts the output of ``RecommendationRepository.get_applied_recommendations_stats()``
    — one dict per (type, namespace) pair — so a Prometheus scrape does not load
    full recommendation records for the counts. Savings rates are published by
    :func:`update_savings_run_rate_metrics`, which knows each record's outcome.

    Args:
        stats_rows: List of dicts with keys: type, namespace, count,
                    total_co2e_grams, total_cost_dollars.
    """
    _clear_gauge(RECOMMENDATIONS_IMPLEMENTED)

    cluster = _get_cluster_name()
    implemented_by_type: dict[str, int] = defaultdict(int)
    for r in stats_rows:
        rec_type = r["type"]
        ns = r["namespace"] or "_cluster"
        count = int(r["count"])
        implemented_by_type[rec_type] += count
        RECOMMENDATIONS_IMPLEMENTED.labels(cluster=cluster, namespace=ns, type=rec_type).set(count)

    for rec_type, count in implemented_by_type.items():
        RECOMMENDATIONS_IMPLEMENTED.labels(cluster=cluster, namespace=DASHBOARD_NAMESPACE_ALL, type=rec_type).set(count)

    logger.debug("Updated implemented recommendation counts: %d applied.", sum(implemented_by_type.values()))


def update_idle_metrics(
    latest_metrics: Sequence[CombinedMetric],
    idle_rows: Sequence[Any],
    idle_since: Optional[datetime],
) -> None:
    """Set the node idle share and cluster-total gauges of the latest collection (WP-05.3, NRG-02).

    The idle share is the latest ``__idle__`` row of each node in ``idle_rows``
    (as the allocated gauges take the latest row of each pod); the cluster
    total adds it to the pods of ``latest_metrics``. Both are removed, never
    zeroed, when no idle row is stored or when a pod row of the snapshot is
    older than the first idle row (``idle_since``): the idle share would then
    cover part of the snapshot only, the rule of ``/metrics/summary``.
    """
    cluster = _get_cluster_name()
    for idle_gauge, total_gauge, _ in _IDLE_GAUGES:
        _clear_gauge(idle_gauge)
        _clear_gauge(total_gauge)
    latest_idle: dict[str, Any] = {}
    for row in idle_rows:
        current = latest_idle.get(row.node)
        if current is None or ensure_utc(row.timestamp) > ensure_utc(current.timestamp):
            latest_idle[row.node] = row
    if not latest_idle or idle_since is None:
        return
    pod_times = [ensure_utc(m.timestamp) for m in latest_metrics if m.timestamp is not None]
    if pod_times and min(pod_times) < ensure_utc(idle_since):
        return
    for idle_gauge, total_gauge, attribute in _IDLE_GAUGES:
        idle = sum(float(getattr(row, attribute) or 0.0) for row in latest_idle.values())
        allocated = sum(float(getattr(m, attribute) or 0.0) for m in latest_metrics)
        idle_gauge.labels(cluster=cluster).set(idle)
        total_gauge.labels(cluster=cluster).set(allocated + idle)


def get_metrics_output() -> bytes:
    """Generates the Prometheus text exposition format output.

    Returns:
        Bytes containing the Prometheus text format metrics.
    """
    return generate_latest(REGISTRY) + generate_latest_observability()


async def refresh_metrics_from_db(
    combined_repo,
    node_repo,
    reco_repo,
    savings_repo=None,
    summary_repo=None,
    gitops_repository: GitOpsRepository | None = None,
) -> None:
    """Read the latest metrics from the database and refresh all Prometheus gauges.

    This is the critical bridge between the scheduler container (which writes
    data to Postgres) and the API container (which exposes ``/prometheus/metrics``).
    Because these are separate processes, in-memory gauge updates made by the
    scheduler never reach the API.  This function is called on each Prometheus
    scrape so the gauges always reflect the most recent data.

    Args:
        combined_repo: CombinedMetricsRepository instance.
        node_repo: NodeRepository instance.
        reco_repo: RecommendationRepository instance.
    """
    from datetime import datetime, timedelta

    now = datetime.now(timezone.utc)
    dashboard_namespaces: list[str] | None = None

    async def get_dashboard_namespaces(metric_family: str) -> list[str]:
        nonlocal dashboard_namespaces
        if dashboard_namespaces is not None:
            return dashboard_namespaces
        try:
            dashboard_namespaces = await combined_repo.list_namespaces()
        except Exception as exc:
            logger.warning(
                "refresh_metrics_from_db: failed to list namespaces for dashboard %s metrics: %s",
                metric_family,
                exc,
            )
            dashboard_namespaces = []
        return dashboard_namespaces

    # --- Combined / cluster / namespace / pod metrics ---
    try:
        # Read the last 20 minutes of data.  The scheduler stores metrics with
        # 5-minute-aligned timestamps (e.g. 21:10, 21:15, …) and may run up to
        # ~11 minutes before the current time, so a 10-minute window would miss
        # the most recent batch.  20 minutes guarantees we always cover at least
        # one full scheduler cycle.
        #
        # Use read_latest_per_pod when available (SQL DISTINCT ON) to avoid
        # loading the full 20-minute history just to find the latest snapshot
        # per pod.  Falls back to Python deduplication on non-SQL backends.
        start = now - timedelta(minutes=20)
        try:
            latest_metrics = await combined_repo.read_latest_per_pod(start_time=start, end_time=now)
        except Exception:
            # Fallback: load all and deduplicate in Python
            metrics = await combined_repo.read_combined_metrics(start_time=start, end_time=now)
            latest: dict[tuple[str, str], CombinedMetric] = {}
            for m in metrics:
                key = (m.namespace, m.pod_name)
                existing = latest.get(key)
                if existing is None:
                    latest[key] = m
                elif m.timestamp is not None and (existing.timestamp is None or m.timestamp > existing.timestamp):
                    latest[key] = m
            latest_metrics = list(latest.values())
        update_cluster_metrics(latest_metrics)
        try:
            # Node idle share of the same window, and the cluster totals (WP-05.3, NRG-02).
            idle_rows = await combined_repo.read_idle_metrics(start, now)
            idle_since = await combined_repo.idle_data_since() if idle_rows else None
            update_idle_metrics(latest_metrics, idle_rows, idle_since)
        except Exception as exc:
            update_idle_metrics([], [], None)
            logger.warning("refresh_metrics_from_db: failed to refresh the node idle share: %s", exc)
    except Exception as exc:
        logger.warning("refresh_metrics_from_db: failed to refresh cluster metrics: %s", exc)

    # --- Node metrics ---
    try:
        nodes = await node_repo.get_latest_snapshots_before(now)
        update_node_metrics(nodes)
    except Exception as exc:
        logger.warning("refresh_metrics_from_db: failed to refresh node metrics: %s", exc)

    # --- Recommendation metrics (all active, no time filter) ---
    try:
        active_recs = await reco_repo.get_active_recommendations()
        update_recommendation_metrics(active_recs)  # type: ignore[arg-type]
    except Exception as exc:
        logger.warning("refresh_metrics_from_db: failed to refresh recommendation metrics: %s", exc)

    # --- Implemented recommendation counts (applied recommendations, all time) ---
    try:
        stats = await reco_repo.get_applied_recommendations_stats()
        update_realized_savings_from_stats(stats)
    except Exception as exc:
        logger.warning("refresh_metrics_from_db: failed to refresh implemented recommendation metrics: %s", exc)

    # --- Annual savings rates by method (applied recommendations) ---
    run_rate_key = f"run_rate:{_get_cluster_name()}"
    if _savings_refresh_due(reco_repo, run_rate_key):
        try:
            applied = await reco_repo.get_applied_recommendations()
            update_savings_run_rate_metrics(applied)
            _mark_savings_refreshed(reco_repo, run_rate_key)
        except Exception as exc:
            logger.warning("refresh_metrics_from_db: failed to refresh savings rate metrics: %s", exc)

    # --- Savings ledger: monotonic counters and exact dashboard windows ---
    if savings_repo is not None:
        try:
            from ..core.savings_attributor import savings_cluster_name

            ledger_cluster = savings_cluster_name()
            cluster = _get_cluster_name()
            ledger_key = f"ledger:{ledger_cluster}:{cluster}"
            if _savings_refresh_due(savings_repo, ledger_key):
                # LED-04: counters and dashboard windows come from the hash-chained ledger v2 (restated,
                # reversals visible); the v1 staging ledger only before the ledger has a cluster identity.
                from ..storage.ledger_repository import get_ledger_repository

                ledger_repo = get_ledger_repository()
                ledger_uid = await savings_counter_source(ledger_repo, ledger_cluster)
                if ledger_uid is not None:
                    counters = await ledger_repo.lifetime_counters(ledger_uid)
                else:
                    counters = await savings_repo.get_lifetime_counters(ledger_cluster)
                update_attributed_savings_metrics(counters, cluster=cluster)
                clear_dashboard_savings_metrics()
                savings_namespaces = await get_dashboard_namespaces("savings")
                for window_slug, start_time, end_time in _dashboard_window_ranges(now):
                    if ledger_uid is not None:
                        breakdown = await ledger_repo.accrual_breakdown(ledger_uid, start_time, end_time)
                    else:
                        breakdown = await savings_repo.get_accrual_breakdown(ledger_cluster, start_time, end_time)
                    update_dashboard_savings_from_breakdown(
                        window_slug,
                        breakdown,
                        cluster=cluster,
                        namespaces=savings_namespaces,
                    )
                _mark_savings_refreshed(savings_repo, ledger_key)
        except Exception as exc:
            logger.warning("refresh_metrics_from_db: failed to refresh savings ledger metrics: %s", exc)

    if gitops_repository is not None:
        try:
            backlog = await gitops_repository.get_automation_backlog_summary(stale_before=now - timedelta(minutes=15))
            record_automation_backlog(backlog.as_metrics())
        except NotImplementedError:
            logger.debug("refresh_metrics_from_db: GitOps repository does not expose backlog metrics")
        except Exception as exc:
            logger.warning("refresh_metrics_from_db: failed to refresh automation backlog metrics: %s", exc)

    # --- Pre-computed dashboard summary windows ---
    if summary_repo is not None:
        try:
            rows = await summary_repo.get_rows(namespace=None)
            update_dashboard_summary_metrics(rows, reset=True)
            namespaces = await get_dashboard_namespaces("summary")
            for namespace in namespaces:
                namespace_rows = await summary_repo.get_rows(namespace=namespace)
                update_dashboard_summary_metrics(namespace_rows, reset=False)
        except Exception as exc:
            logger.warning("refresh_metrics_from_db: failed to refresh dashboard summary metrics: %s", exc)
