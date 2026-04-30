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
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import List

from prometheus_client import CollectorRegistry, Gauge, generate_latest

from greenkube.core.config import get_config
from greenkube.core.sustainability_score import SustainabilityScorer
from greenkube.models.metrics import CombinedMetric, MetricsSummaryRow, Recommendation, RecommendationRecord
from greenkube.models.node import NodeInfo

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
POD_NETWORK_RX = Gauge(
    "greenkube_pod_network_receive_bytes",
    "Network bytes received per second per pod",
    POD_LABELS,
    registry=REGISTRY,
)
POD_NETWORK_TX = Gauge(
    "greenkube_pod_network_transmit_bytes",
    "Network bytes transmitted per second per pod",
    POD_LABELS,
    registry=REGISTRY,
)
POD_DISK_READ = Gauge(
    "greenkube_pod_disk_read_bytes",
    "Disk bytes read per second per pod",
    POD_LABELS,
    registry=REGISTRY,
)
POD_DISK_WRITE = Gauge(
    "greenkube_pod_disk_write_bytes",
    "Disk bytes written per second per pod",
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

# ---------------------------------------------------------------------------
# Dashboard summary gauges
# ---------------------------------------------------------------------------
DASHBOARD_NAMESPACE_ALL = "__all__"
DASHBOARD_SUMMARY_LABELS = ["cluster", "window", "namespace"]
DASHBOARD_SUMMARY_CO2_LABELS = ["cluster", "window", "namespace", "scope"]
DASHBOARD_SAVINGS_LABELS = ["cluster", "window", "recommendation_type"]
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
    "DB-backed CO2e savings attributed during a fixed dashboard time window.",
    DASHBOARD_SAVINGS_LABELS,
    registry=REGISTRY,
)
DASHBOARD_SAVINGS_COST = Gauge(
    "greenkube_dashboard_savings_cost_dollars_total",
    "DB-backed cost savings attributed during a fixed dashboard time window.",
    DASHBOARD_SAVINGS_LABELS,
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# GreenKube self-monitoring gauges
# ---------------------------------------------------------------------------
GREENKUBE_ESTIMATED_METRICS_RATIO = Gauge(
    "greenkube_estimated_metrics_ratio",
    "Fraction of metrics that rely on estimated values (0.0 = all measured, 1.0 = all estimated)",
    registry=REGISTRY,
)
GREENKUBE_LAST_COLLECTION_TIMESTAMP = Gauge(
    "greenkube_last_collection_timestamp_seconds",
    "Unix timestamp of the most recent metric in the database",
    registry=REGISTRY,
)
GREENKUBE_METRICS_TOTAL = Gauge(
    "greenkube_metrics_total",
    "Total number of combined metric records in the latest window",
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
    ["cluster", "zone", "lookup", "nodes", "node_count", "map_label"],
    registry=REGISTRY,
)
SUSTAINABILITY_SCORE = Gauge(
    "greenkube_sustainability_score",
    "Composite sustainability score (0-100, higher is better). "
    "Aggregates resource efficiency, carbon intensity, waste elimination, "
    "node efficiency, scaling practices, carbon-aware scheduling, and stability.",
    ["cluster"],
    registry=REGISTRY,
)
SUSTAINABILITY_DIMENSION_SCORE = Gauge(
    "greenkube_sustainability_dimension_score",
    "Per-dimension sustainability score (0-100, higher is better)",
    ["cluster", "dimension"],
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
    ["cluster", "type", "priority"],
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
# Realized savings gauges (DB-backed cumulative totals)
#
# These gauges hold the CUMULATIVE total savings attributed to each
# recommendation type since GreenKube installation. Prefer the
# greenkube_dashboard_savings_* gauges for exact dashboard time windows.
# ---------------------------------------------------------------------------
SAVINGS_CO2_ATTRIBUTED = Gauge(
    "greenkube_co2e_savings_attributed_grams_total",
    "Cumulative CO2e (grams) avoided, prorated from applied recommendations. "
    "Prefer greenkube_dashboard_savings_co2e_grams_total for exact dashboard windows.",
    ["cluster", "recommendation_type"],
    registry=REGISTRY,
)
SAVINGS_COST_ATTRIBUTED = Gauge(
    "greenkube_cost_savings_attributed_dollars_total",
    "Cumulative cloud cost (dollars) avoided, prorated from applied recommendations. "
    "Prefer greenkube_dashboard_savings_cost_dollars_total for exact dashboard windows.",
    ["cluster", "recommendation_type"],
    registry=REGISTRY,
)
# Legacy gauges kept for backward compatibility — show the total annual
# projection from applied recommendations (not window-aware).
CLUSTER_CO2_SAVED = Gauge(
    "greenkube_cluster_co2e_saved_grams_total",
    "Annual projected CO2e savings (grams/year) from all applied recommendations.",
    ["cluster"],
    registry=REGISTRY,
)
CLUSTER_COST_SAVED = Gauge(
    "greenkube_cluster_cost_saved_dollars_total",
    "Annual projected cost savings (dollars/year) from all applied recommendations.",
    ["cluster"],
    registry=REGISTRY,
)
RECOMMENDATIONS_IMPLEMENTED = Gauge(
    "greenkube_recommendations_implemented_total",
    "Number of recommendations marked as applied, by recommendation type",
    ["cluster", "type"],
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
    reset: bool = False,
) -> None:
    """Expose exact DB-backed savings totals for a dashboard time window."""
    if reset:
        clear_dashboard_savings_metrics()

    cluster = cluster if cluster is not None else _get_cluster_name()
    total_co2e = sum(totals.get("co2e_saved_grams", 0.0) for totals in totals_by_type.values())
    total_cost = sum(totals.get("cost_saved_dollars", 0.0) for totals in totals_by_type.values())

    for window_label in _dashboard_window_labels(window_slug):
        labels = {"cluster": cluster, "window": window_label}
        DASHBOARD_SAVINGS_CO2.labels(**labels, recommendation_type="all").set(total_co2e)
        DASHBOARD_SAVINGS_COST.labels(**labels, recommendation_type="all").set(total_cost)

        for recommendation_type, totals in totals_by_type.items():
            DASHBOARD_SAVINGS_CO2.labels(**labels, recommendation_type=recommendation_type).set(
                totals.get("co2e_saved_grams", 0.0)
            )
            DASHBOARD_SAVINGS_COST.labels(**labels, recommendation_type=recommendation_type).set(
                totals.get("cost_saved_dollars", 0.0)
            )


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
        POD_NETWORK_RX,
        POD_NETWORK_TX,
        POD_DISK_READ,
        POD_DISK_WRITE,
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
        GREENKUBE_ESTIMATED_METRICS_RATIO.set(0)
        GREENKUBE_METRICS_TOTAL.set(0)
        CARBON_INTENSITY_SCORE.labels(cluster=cluster).set(0)
        SUSTAINABILITY_SCORE.labels(cluster=cluster).set(50)
        _node_cpu_allocated.clear()
        _node_memory_allocated.clear()
        return

    # Namespace aggregations
    ns_co2: dict[str, float] = defaultdict(float)
    ns_embodied: dict[str, float] = defaultdict(float)
    ns_cost: dict[str, float] = defaultdict(float)
    ns_energy: dict[str, float] = defaultdict(float)
    ns_pods: dict[str, set] = defaultdict(set)

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
        POD_CPU_USAGE.labels(**labels).set(m.cpu_usage_millicores or 0)
        POD_MEMORY_REQUEST.labels(**labels).set(m.memory_request)
        POD_MEMORY_USAGE.labels(**labels).set(m.memory_usage_bytes or 0)
        POD_NETWORK_RX.labels(**labels).set(m.network_receive_bytes or 0.0)
        POD_NETWORK_TX.labels(**labels).set(m.network_transmit_bytes or 0.0)
        POD_DISK_READ.labels(**labels).set(m.disk_read_bytes or 0.0)
        POD_DISK_WRITE.labels(**labels).set(m.disk_write_bytes or 0.0)
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
    GREENKUBE_METRICS_TOTAL.set(len(metrics))
    estimated_count = sum(1 for m in metrics if m.is_estimated)
    GREENKUBE_ESTIMATED_METRICS_RATIO.set(estimated_count / len(metrics) if metrics else 0.0)
    latest_ts = max((m.timestamp for m in metrics if m.timestamp), default=None)
    if latest_ts:
        GREENKUBE_LAST_COLLECTION_TIMESTAMP.set(latest_ts.timestamp())

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
        nodes_label = ", ".join(node_names)
        node_word = "node" if node_count == 1 else "nodes"
        lookup = zone.split("-", 1)[0] if zone else "unknown"
        map_label = f"{zone} · {rounded_intensity:g} gCO₂/kWh · {node_count} {node_word} · {nodes_label}"
        ZONE_GRID_INTENSITY_MAP.labels(
            cluster=cluster,
            zone=zone,
            lookup=lookup,
            nodes=nodes_label,
            node_count=str(node_count),
            map_label=map_label,
        ).set(round(weighted_intensity, 2))

    # Comprehensive sustainability score (0-100, 100 = best)
    scorer = SustainabilityScorer()
    score_result = scorer.compute(metrics)
    SUSTAINABILITY_SCORE.labels(cluster=cluster).set(score_result.overall_score)
    for dim, dim_score in score_result.dimension_scores.items():
        SUSTAINABILITY_DIMENSION_SCORE.labels(cluster=cluster, dimension=dim).set(dim_score)

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


def update_recommendation_metrics(recommendations: List[Recommendation]) -> None:
    """Updates Prometheus gauges with current recommendation counts and savings.

    Clears previous values and sets new ones based on the provided list.

    Args:
        recommendations: The current list of active recommendations.
    """
    _clear_gauge(RECOMMENDATION_COUNT)
    _clear_gauge(RECOMMENDATION_SAVINGS_COST)
    _clear_gauge(RECOMMENDATION_SAVINGS_CO2)
    _clear_gauge(NS_REC_SAVINGS_CO2)
    _clear_gauge(NS_REC_SAVINGS_COST)

    if not recommendations:
        return

    cluster = _get_cluster_name()
    count_by_type_priority: dict[tuple[str, str], int] = defaultdict(int)
    savings_cost_by_type: dict[str, float] = defaultdict(float)
    savings_co2_by_type: dict[str, float] = defaultdict(float)
    ns_savings_co2: dict[str, float] = defaultdict(float)
    ns_savings_cost: dict[str, float] = defaultdict(float)

    for rec in recommendations:
        rec_type = rec.type.value if hasattr(rec.type, "value") else str(rec.type)
        count_by_type_priority[(rec_type, rec.priority)] += 1
        savings_cost_by_type[rec_type] += rec.potential_savings_cost or 0.0
        savings_co2_by_type[rec_type] += rec.potential_savings_co2e_grams or 0.0
        ns = getattr(rec, "namespace", None) or "_cluster"
        ns_savings_co2[ns] += rec.potential_savings_co2e_grams or 0.0
        ns_savings_cost[ns] += rec.potential_savings_cost or 0.0

    for (rec_type, priority), count in count_by_type_priority.items():
        RECOMMENDATION_COUNT.labels(cluster=cluster, type=rec_type, priority=priority).set(count)

    for rec_type, cost in savings_cost_by_type.items():
        RECOMMENDATION_SAVINGS_COST.labels(cluster=cluster, type=rec_type).set(cost)

    for rec_type, co2 in savings_co2_by_type.items():
        RECOMMENDATION_SAVINGS_CO2.labels(cluster=cluster, type=rec_type).set(co2)

    for ns, co2 in ns_savings_co2.items():
        NS_REC_SAVINGS_CO2.labels(cluster=cluster, namespace=ns).set(co2)

    for ns, cost in ns_savings_cost.items():
        NS_REC_SAVINGS_COST.labels(cluster=cluster, namespace=ns).set(cost)

    logger.debug("Updated Prometheus metrics with %d recommendations.", len(recommendations))


def update_attributed_savings_metrics(
    cumulative_totals: dict,
    cluster: str,
) -> None:
    """Update the DB-backed cumulative savings gauges.

    Called by ``refresh_metrics_from_db`` with the output of
    ``SavingsAttributor.get_cumulative_totals()``.

    Args:
        cumulative_totals: ``{rec_type: {"co2e_saved_grams": float, "cost_saved_dollars": float}}``
        cluster:           Cluster name label value.
    """
    _clear_gauge(SAVINGS_CO2_ATTRIBUTED)
    _clear_gauge(SAVINGS_COST_ATTRIBUTED)

    if not cumulative_totals:
        return

    for rec_type, totals in cumulative_totals.items():
        SAVINGS_CO2_ATTRIBUTED.labels(cluster=cluster, recommendation_type=rec_type).set(
            totals.get("co2e_saved_grams", 0.0)
        )
        SAVINGS_COST_ATTRIBUTED.labels(cluster=cluster, recommendation_type=rec_type).set(
            totals.get("cost_saved_dollars", 0.0)
        )

    logger.debug("Updated attributed savings gauges for cluster=%s: %d types.", cluster, len(cumulative_totals))


def update_realized_savings_metrics(applied_records: List[RecommendationRecord]) -> None:
    """Update Prometheus gauges for realized (already achieved) savings.

    Reads applied recommendation records and exposes cumulative CO2e and cost
    savings, plus a count of implemented recommendations by type.

    Args:
        applied_records: List of RecommendationRecord objects with status='applied'.
    """
    _clear_gauge(CLUSTER_CO2_SAVED)
    _clear_gauge(CLUSTER_COST_SAVED)
    _clear_gauge(RECOMMENDATIONS_IMPLEMENTED)

    cluster = _get_cluster_name()

    if not applied_records:
        CLUSTER_CO2_SAVED.labels(cluster=cluster).set(0)
        CLUSTER_COST_SAVED.labels(cluster=cluster).set(0)
        return

    total_co2_saved = sum(r.carbon_saved_co2e_grams or 0.0 for r in applied_records)
    total_cost_saved = sum(r.cost_saved or 0.0 for r in applied_records)

    CLUSTER_CO2_SAVED.labels(cluster=cluster).set(total_co2_saved)
    CLUSTER_COST_SAVED.labels(cluster=cluster).set(total_cost_saved)

    implemented_by_type: dict[str, int] = defaultdict(int)
    for r in applied_records:
        rec_type = r.type.value if hasattr(r.type, "value") else str(r.type)
        implemented_by_type[rec_type] += 1

    for rec_type, count in implemented_by_type.items():
        RECOMMENDATIONS_IMPLEMENTED.labels(cluster=cluster, type=rec_type).set(count)

    logger.debug(
        "Updated realized savings metrics: %.2f g CO2e saved, $%.2f saved, %d recommendations applied.",
        total_co2_saved,
        total_cost_saved,
        len(applied_records),
    )


def get_metrics_output() -> bytes:
    """Generates the Prometheus text exposition format output.

    Returns:
        Bytes containing the Prometheus text format metrics.
    """
    return generate_latest(REGISTRY)


async def refresh_metrics_from_db(combined_repo, node_repo, reco_repo, savings_repo=None, summary_repo=None) -> None:
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

    # --- Combined / cluster / namespace / pod metrics ---
    try:
        # Read the last 20 minutes of data.  The scheduler stores metrics with
        # 5-minute-aligned timestamps (e.g. 21:10, 21:15, …) and may run up to
        # ~11 minutes before the current time, so a 10-minute window would miss
        # the most recent batch.  20 minutes guarantees we always cover at least
        # one full scheduler cycle.
        start = now - timedelta(minutes=20)
        metrics = await combined_repo.read_combined_metrics(start_time=start, end_time=now)
        # Deduplicate: keep only the latest snapshot per (namespace, pod_name) so
        # that Prometheus gauges have exactly one time-series per pod and Grafana
        # does not show the same pod multiple times.
        latest: dict[tuple[str, str], CombinedMetric] = {}
        for m in metrics:
            key = (m.namespace, m.pod_name)
            existing = latest.get(key)
            if existing is None:
                latest[key] = m
            elif m.timestamp is not None and (existing.timestamp is None or m.timestamp > existing.timestamp):
                latest[key] = m
        update_cluster_metrics(list(latest.values()))
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

    # --- Realized savings metrics (applied recommendations, all time) ---
    try:
        applied = await reco_repo.get_applied_recommendations()
        update_realized_savings_metrics(applied)
    except Exception as exc:
        logger.warning("refresh_metrics_from_db: failed to refresh realized savings metrics: %s", exc)

    # --- Window-aware attributed savings (DB-backed cumulative totals) ---
    if savings_repo is not None:
        try:
            from ..core.savings_attributor import SavingsAttributor

            cluster = _get_cluster_name()
            attributor = SavingsAttributor(savings_repo=savings_repo, cluster_name=cluster)
            totals = await attributor.get_cumulative_totals()
            update_attributed_savings_metrics(totals, cluster=cluster)
            clear_dashboard_savings_metrics()
            for window_slug, start_time, end_time in _dashboard_window_ranges(now):
                window_totals = await savings_repo.get_window_totals(
                    cluster_name=cluster,
                    start_time=start_time,
                    end_time=end_time,
                )
                update_dashboard_savings_metrics(window_slug, window_totals, cluster=cluster)
        except Exception as exc:
            logger.warning("refresh_metrics_from_db: failed to refresh attributed savings metrics: %s", exc)

    # --- Pre-computed dashboard summary windows ---
    if summary_repo is not None:
        try:
            rows = await summary_repo.get_rows(namespace=None)
            update_dashboard_summary_metrics(rows, reset=True)
            try:
                namespaces = await combined_repo.list_namespaces()
            except Exception as exc:
                logger.warning(
                    "refresh_metrics_from_db: failed to list namespaces for dashboard summary metrics: %s", exc
                )
                namespaces = []
            for namespace in namespaces:
                namespace_rows = await summary_repo.get_rows(namespace=namespace)
                update_dashboard_summary_metrics(namespace_rows, reset=False)
        except Exception as exc:
            logger.warning("refresh_metrics_from_db: failed to refresh dashboard summary metrics: %s", exc)
