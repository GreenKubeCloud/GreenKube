# src/greenkube/api/metrics_endpoint.py
"""
Prometheus metrics exposition for GreenKube.

Exposes comprehensive cluster, namespace, pod, node, and recommendation
metrics as Prometheus Gauges so they can be scraped by Prometheus and
visualized in Grafana dashboards.
"""

import logging
from collections import defaultdict
from typing import List

from prometheus_client import CollectorRegistry, Gauge, generate_latest

from greenkube.models.metrics import CombinedMetric, Recommendation
from greenkube.models.node import NodeInfo

logger = logging.getLogger(__name__)

# Use a custom registry to avoid polluting the default registry with
# process/platform collectors that are irrelevant in a FastAPI context.
REGISTRY = CollectorRegistry()

# ---------------------------------------------------------------------------
# Pod-level gauges
# ---------------------------------------------------------------------------
POD_LABELS = ["namespace", "pod", "node"]

POD_CO2 = Gauge(
    "greenkube_pod_co2e_grams",
    "Operational CO2e emissions per pod in grams",
    POD_LABELS,
    registry=REGISTRY,
)
POD_EMBODIED_CO2 = Gauge(
    "greenkube_pod_embodied_co2e_grams",
    "Embodied CO2e emissions per pod in grams",
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
NS_LABELS = ["namespace"]

NS_CO2_TOTAL = Gauge(
    "greenkube_namespace_co2e_grams_total",
    "Total operational CO2e per namespace in grams",
    NS_LABELS,
    registry=REGISTRY,
)
NS_EMBODIED_CO2_TOTAL = Gauge(
    "greenkube_namespace_embodied_co2e_grams_total",
    "Total embodied CO2e per namespace in grams",
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
# Cluster-level summary gauges (no labels)
# ---------------------------------------------------------------------------
CLUSTER_CO2_TOTAL = Gauge(
    "greenkube_cluster_co2e_grams_total",
    "Total operational CO2e across all pods in grams",
    registry=REGISTRY,
)
CLUSTER_EMBODIED_CO2_TOTAL = Gauge(
    "greenkube_cluster_embodied_co2e_grams_total",
    "Total embodied CO2e across all pods in grams",
    registry=REGISTRY,
)
CLUSTER_COST_TOTAL = Gauge(
    "greenkube_cluster_cost_dollars_total",
    "Total cost across all pods in dollars",
    registry=REGISTRY,
)
CLUSTER_ENERGY_TOTAL = Gauge(
    "greenkube_cluster_energy_joules_total",
    "Total energy across all pods in Joules",
    registry=REGISTRY,
)
CLUSTER_POD_COUNT = Gauge(
    "greenkube_cluster_pod_count",
    "Total number of unique pods in latest collection",
    registry=REGISTRY,
)
CLUSTER_NAMESPACE_COUNT = Gauge(
    "greenkube_cluster_namespace_count",
    "Total number of unique namespaces in latest collection",
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
    "Embodied emissions per node in kgCO2eq",
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
    ["type", "priority"],
    registry=REGISTRY,
)
RECOMMENDATION_SAVINGS_COST = Gauge(
    "greenkube_recommendations_savings_cost_dollars",
    "Total potential cost savings from recommendations by type",
    ["type"],
    registry=REGISTRY,
)
RECOMMENDATION_SAVINGS_CO2 = Gauge(
    "greenkube_recommendations_savings_co2e_grams",
    "Total potential CO2e savings from recommendations by type",
    ["type"],
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# Update helpers
# ---------------------------------------------------------------------------


def _clear_gauge(gauge: Gauge) -> None:
    """Clear all label combinations from a gauge."""
    gauge._metrics.clear()


def update_cluster_metrics(metrics: List[CombinedMetric]) -> None:
    """Update all pod-level, namespace-level, and cluster-level Prometheus gauges.

    Args:
        metrics: The latest list of CombinedMetric objects from all pods.
    """
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
        NS_CO2_TOTAL,
        NS_EMBODIED_CO2_TOTAL,
        NS_COST_TOTAL,
        NS_ENERGY_TOTAL,
        NS_POD_COUNT,
    ):
        _clear_gauge(g)

    if not metrics:
        CLUSTER_CO2_TOTAL.set(0)
        CLUSTER_EMBODIED_CO2_TOTAL.set(0)
        CLUSTER_COST_TOTAL.set(0)
        CLUSTER_ENERGY_TOTAL.set(0)
        CLUSTER_POD_COUNT.set(0)
        CLUSTER_NAMESPACE_COUNT.set(0)
        return

    # Namespace aggregations
    ns_co2: dict[str, float] = defaultdict(float)
    ns_embodied: dict[str, float] = defaultdict(float)
    ns_cost: dict[str, float] = defaultdict(float)
    ns_energy: dict[str, float] = defaultdict(float)
    ns_pods: dict[str, set] = defaultdict(set)

    for m in metrics:
        labels = {
            "namespace": m.namespace,
            "pod": m.pod_name,
            "node": m.node or "unknown",
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

        # Accumulate namespace totals
        ns = m.namespace
        ns_co2[ns] += m.co2e_grams
        ns_embodied[ns] += m.embodied_co2e_grams or 0.0
        ns_cost[ns] += m.total_cost
        ns_energy[ns] += m.joules
        ns_pods[ns].add(m.pod_name)

    # Set namespace gauges
    for ns in ns_co2:
        NS_CO2_TOTAL.labels(namespace=ns).set(ns_co2[ns])
        NS_EMBODIED_CO2_TOTAL.labels(namespace=ns).set(ns_embodied[ns])
        NS_COST_TOTAL.labels(namespace=ns).set(ns_cost[ns])
        NS_ENERGY_TOTAL.labels(namespace=ns).set(ns_energy[ns])
        NS_POD_COUNT.labels(namespace=ns).set(len(ns_pods[ns]))

    # Cluster totals
    CLUSTER_CO2_TOTAL.set(sum(ns_co2.values()))
    CLUSTER_EMBODIED_CO2_TOTAL.set(sum(ns_embodied.values()))
    CLUSTER_COST_TOTAL.set(sum(ns_cost.values()))
    CLUSTER_ENERGY_TOTAL.set(sum(ns_energy.values()))
    CLUSTER_POD_COUNT.set(len({m.pod_name for m in metrics}))
    CLUSTER_NAMESPACE_COUNT.set(len(ns_co2))

    logger.debug("Updated Prometheus cluster metrics with %d pod metrics.", len(metrics))


def update_node_metrics(nodes: List[NodeInfo]) -> None:
    """Update node-level Prometheus gauges with current node information.

    Args:
        nodes: The latest list of NodeInfo objects.
    """
    for g in (NODE_CPU_CAPACITY, NODE_MEMORY_CAPACITY, NODE_EMBODIED, NODE_INFO):
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
        if node.embodied_emissions_kg is not None:
            NODE_EMBODIED.labels(**labels).set(node.embodied_emissions_kg)

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

    if not recommendations:
        return

    count_by_type_priority: dict[tuple[str, str], int] = defaultdict(int)
    savings_cost_by_type: dict[str, float] = defaultdict(float)
    savings_co2_by_type: dict[str, float] = defaultdict(float)

    for rec in recommendations:
        rec_type = rec.type.value if hasattr(rec.type, "value") else str(rec.type)
        count_by_type_priority[(rec_type, rec.priority)] += 1
        savings_cost_by_type[rec_type] += rec.potential_savings_cost or 0.0
        savings_co2_by_type[rec_type] += rec.potential_savings_co2e_grams or 0.0

    for (rec_type, priority), count in count_by_type_priority.items():
        RECOMMENDATION_COUNT.labels(type=rec_type, priority=priority).set(count)

    for rec_type, cost in savings_cost_by_type.items():
        RECOMMENDATION_SAVINGS_COST.labels(type=rec_type).set(cost)

    for rec_type, co2 in savings_co2_by_type.items():
        RECOMMENDATION_SAVINGS_CO2.labels(type=rec_type).set(co2)

    logger.debug("Updated Prometheus metrics with %d recommendations.", len(recommendations))


def get_metrics_output() -> bytes:
    """Generates the Prometheus text exposition format output.

    Returns:
        Bytes containing the Prometheus text format metrics.
    """
    return generate_latest(REGISTRY)


async def refresh_metrics_from_db(combined_repo, node_repo, reco_repo) -> None:
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
    from datetime import datetime, timedelta, timezone

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

    # --- Recommendation metrics ---
    try:
        # Fetch the last 24h of recommendations
        start_reco = now - timedelta(hours=24)
        records = await reco_repo.get_recommendations(start=start_reco, end=now)
        if records:
            # RecommendationRecord has the same .type, .priority,
            # .potential_savings_cost, .potential_savings_co2e_grams that
            # update_recommendation_metrics() reads, so we can pass them directly.
            update_recommendation_metrics(records)  # type: ignore[arg-type]
    except Exception as exc:
        logger.warning("refresh_metrics_from_db: failed to refresh recommendation metrics: %s", exc)
