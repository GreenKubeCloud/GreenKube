# src/greenkube/demo/data_generator.py
"""
Generates realistic sample data for the GreenKube demo mode.

The data simulates a typical Kubernetes cluster running a microservice
application (an e-commerce platform) across multiple namespaces, with
realistic carbon emission, cost, and resource usage patterns.
"""

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import List

from greenkube.models.metrics import (
    CombinedMetric,
    RecommendationRecord,
    RecommendationType,
)
from greenkube.models.node import NodeInfo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Realistic cluster topology
# ---------------------------------------------------------------------------

DEMO_NODES = [
    {
        "name": "node-prod-01",
        "instance_type": "m5.xlarge",
        "zone": "eu-west-1a",
        "region": "eu-west-1",
        "cloud_provider": "aws",
        "architecture": "amd64",
        "node_pool": "default-pool",
        "cpu_capacity_cores": 4.0,
        "memory_capacity_bytes": 16 * 1024**3,
        "embodied_emissions_kg": 320.0,
    },
    {
        "name": "node-prod-02",
        "instance_type": "m5.xlarge",
        "zone": "eu-west-1b",
        "region": "eu-west-1",
        "cloud_provider": "aws",
        "architecture": "amd64",
        "node_pool": "default-pool",
        "cpu_capacity_cores": 4.0,
        "memory_capacity_bytes": 16 * 1024**3,
        "embodied_emissions_kg": 320.0,
    },
    {
        "name": "node-prod-03",
        "instance_type": "m6g.large",
        "zone": "eu-west-1c",
        "region": "eu-west-1",
        "cloud_provider": "aws",
        "architecture": "arm64",
        "node_pool": "arm-pool",
        "cpu_capacity_cores": 2.0,
        "memory_capacity_bytes": 8 * 1024**3,
        "embodied_emissions_kg": 180.0,
    },
]

# Namespace → list of (pod_name, owner_kind, owner_name, base_cpu_req_m, base_mem_req_bytes)
DEMO_WORKLOADS: dict[str, list[tuple[str, str, str, int, int]]] = {
    "production": [
        ("api-gateway-6f7b8c-abc12", "Deployment", "api-gateway", 500, 512 * 1024**2),
        ("api-gateway-6f7b8c-def34", "Deployment", "api-gateway", 500, 512 * 1024**2),
        ("order-service-7d9e1f-ghi56", "Deployment", "order-service", 250, 256 * 1024**2),
        ("payment-service-8a2b3c-jkl78", "Deployment", "payment-service", 200, 256 * 1024**2),
        ("user-service-5c4d6e-mno90", "Deployment", "user-service", 200, 256 * 1024**2),
        ("catalog-service-3f1g2h-pqr12", "Deployment", "catalog-service", 300, 384 * 1024**2),
        ("search-engine-9i8j7k-stu34", "Deployment", "search-engine", 1000, 2 * 1024**3),
        ("postgres-primary-0", "StatefulSet", "postgres-primary", 500, 1024 * 1024**2),
        ("redis-cache-0", "StatefulSet", "redis-cache", 100, 128 * 1024**2),
    ],
    "staging": [
        ("api-gateway-staging-abc12", "Deployment", "api-gateway-staging", 250, 256 * 1024**2),
        ("order-service-staging-def34", "Deployment", "order-service-staging", 125, 128 * 1024**2),
        ("payment-service-staging-ghi56", "Deployment", "payment-service-staging", 100, 128 * 1024**2),
        ("load-test-runner-jkl78", "Job", "load-test-runner", 2000, 4 * 1024**3),
    ],
    "monitoring": [
        ("prometheus-server-0", "StatefulSet", "prometheus-server", 500, 2 * 1024**3),
        ("grafana-5a6b7c-mno90", "Deployment", "grafana", 200, 256 * 1024**2),
        ("alertmanager-0", "StatefulSet", "alertmanager", 100, 128 * 1024**2),
    ],
    "data-pipeline": [
        ("spark-driver-abc12", "Job", "spark-etl", 2000, 4 * 1024**3),
        ("spark-executor-0", "Job", "spark-etl", 4000, 8 * 1024**3),
        ("spark-executor-1", "Job", "spark-etl", 4000, 8 * 1024**3),
        ("kafka-broker-0", "StatefulSet", "kafka", 500, 2 * 1024**3),
    ],
    "ci-cd": [
        ("jenkins-agent-xyz99", "Deployment", "jenkins-agent", 500, 1024 * 1024**2),
        ("artifact-builder-abc01", "Job", "artifact-builder", 1000, 2 * 1024**3),
    ],
}

# Carbon intensity data (gCO2/kWh) - simulating France with some variation
DEMO_ZONE = "FR"
DEMO_BASE_INTENSITY = 58.0  # France average
DEMO_INTENSITY_VARIATION = 30.0  # Day/night and seasonal variation


def _generate_intensity_for_hour(hour: int) -> float:
    """Generate realistic carbon intensity based on hour of day (France grid)."""
    # Peak hours (8-20) have slightly higher intensity
    if 8 <= hour <= 20:
        base = DEMO_BASE_INTENSITY + 15
    elif 0 <= hour <= 5:
        base = DEMO_BASE_INTENSITY - 10
    else:
        base = DEMO_BASE_INTENSITY
    return max(10.0, base + random.uniform(-DEMO_INTENSITY_VARIATION / 2, DEMO_INTENSITY_VARIATION / 2))


def generate_node_snapshots(days: int = 7) -> List[NodeInfo]:
    """Generate node snapshot data for the demo period.

    Args:
        days: Number of days of history to generate.

    Returns:
        A list of NodeInfo objects.
    """
    nodes = []
    now = datetime.now(timezone.utc)

    for node_def in DEMO_NODES:
        for day_offset in range(days):
            ts = now - timedelta(days=day_offset, hours=random.randint(0, 3))
            nodes.append(
                NodeInfo(
                    name=node_def["name"],
                    instance_type=node_def["instance_type"],
                    zone=node_def["zone"],
                    region=node_def["region"],
                    cloud_provider=node_def["cloud_provider"],
                    architecture=node_def["architecture"],
                    node_pool=node_def["node_pool"],
                    cpu_capacity_cores=node_def["cpu_capacity_cores"],
                    memory_capacity_bytes=node_def["memory_capacity_bytes"],
                    timestamp=ts,
                    embodied_emissions_kg=node_def["embodied_emissions_kg"],
                )
            )

    logger.info("Generated %d node snapshots for %d nodes over %d days.", len(nodes), len(DEMO_NODES), days)
    return nodes


def generate_carbon_intensity_history(days: int = 7) -> list[dict]:
    """Generate hourly carbon intensity history for the demo period.

    Args:
        days: Number of days of history to generate.

    Returns:
        A list of dicts matching the ElectricityMaps history format.
    """
    records = []
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    for hours_ago in range(days * 24):
        ts = now - timedelta(hours=hours_ago)
        intensity = _generate_intensity_for_hour(ts.hour)
        records.append(
            {
                "zone": DEMO_ZONE,
                "carbonIntensity": round(intensity, 2),
                "datetime": ts.isoformat(),
                "updatedAt": ts.isoformat(),
                "createdAt": ts.isoformat(),
                "emissionFactorType": "lifecycle",
                "isEstimated": False,
                "estimationMethod": None,
            }
        )

    logger.info("Generated %d carbon intensity records.", len(records))
    return records


def generate_combined_metrics(days: int = 7) -> List[CombinedMetric]:
    """Generate realistic combined metrics for all demo workloads.

    Produces one metric record per pod per hour for the demo period,
    simulating day/night traffic patterns, over-provisioned pods, and
    idle workloads.

    Args:
        days: Number of days of history to generate.

    Returns:
        A list of CombinedMetric objects.
    """
    metrics = []
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    node_names = [n["name"] for n in DEMO_NODES]

    for hours_ago in range(days * 24):
        ts = now - timedelta(hours=hours_ago)
        hour = ts.hour
        intensity = _generate_intensity_for_hour(hour)

        # Traffic multiplier: peak during business hours, low at night
        if 9 <= hour <= 18:
            traffic_mult = 1.0 + random.uniform(-0.1, 0.2)
        elif 6 <= hour <= 22:
            traffic_mult = 0.6 + random.uniform(-0.1, 0.1)
        else:
            traffic_mult = 0.15 + random.uniform(-0.05, 0.1)

        for namespace, workloads in DEMO_WORKLOADS.items():
            for pod_name, owner_kind, owner_name, base_cpu, base_mem in workloads:
                # Determine usage based on pod type
                if "spark" in pod_name or "load-test" in pod_name:
                    # Batch jobs: high usage when running, zero otherwise
                    if random.random() < 0.3:  # 30% chance of being active
                        cpu_usage = int(base_cpu * random.uniform(0.7, 0.95))
                        mem_usage = int(base_mem * random.uniform(0.6, 0.9))
                    else:
                        cpu_usage = int(base_cpu * 0.01)
                        mem_usage = int(base_mem * 0.05)
                elif "jenkins" in pod_name or "artifact" in pod_name:
                    # CI/CD: intermittent usage
                    if 9 <= hour <= 18 and random.random() < 0.5:
                        cpu_usage = int(base_cpu * random.uniform(0.4, 0.8))
                        mem_usage = int(base_mem * random.uniform(0.3, 0.7))
                    else:
                        cpu_usage = int(base_cpu * 0.02)
                        mem_usage = int(base_mem * 0.05)
                elif namespace == "staging":
                    # Staging: mostly idle, occasional bursts
                    if 10 <= hour <= 16 and random.random() < 0.4:
                        cpu_usage = int(base_cpu * random.uniform(0.3, 0.6))
                        mem_usage = int(base_mem * random.uniform(0.2, 0.5))
                    else:
                        cpu_usage = int(base_cpu * 0.02)
                        mem_usage = int(base_mem * 0.05)
                else:
                    # Production services: follow traffic pattern
                    cpu_usage = int(base_cpu * traffic_mult * random.uniform(0.15, 0.5))
                    mem_usage = int(base_mem * traffic_mult * random.uniform(0.3, 0.7))

                # Ensure minimum values
                cpu_usage = max(1, cpu_usage)
                mem_usage = max(1024, mem_usage)

                # Energy: derived from CPU usage (simplified model)
                power_watts = (cpu_usage / 1000.0) * random.uniform(8.0, 15.0)
                joules = power_watts * 3600  # 1 hour in seconds

                # PUE varies by node
                pue = 1.1 + random.uniform(0, 0.15)

                # CO2 = (energy_kwh * pue * grid_intensity)
                energy_kwh = joules / 3.6e6
                co2e = energy_kwh * pue * intensity

                # Embodied: allocated proportionally to CPU usage from node total
                embodied_co2e = (cpu_usage / 4000.0) * (320.0 * 1000 / (4 * 365 * 24))  # grams per hour

                # Cost: simplified (CPU + memory)
                cpu_cost_hour = (cpu_usage / 1000.0) * 0.048  # ~$0.048/vCPU-hour (m5.xlarge)
                mem_cost_hour = (mem_usage / (1024**3)) * 0.006  # ~$0.006/GB-hour
                total_cost = cpu_cost_hour + mem_cost_hour

                node = random.choice(node_names)

                metrics.append(
                    CombinedMetric(
                        pod_name=pod_name,
                        namespace=namespace,
                        total_cost=round(total_cost, 6),
                        co2e_grams=round(co2e, 4),
                        pue=round(pue, 3),
                        grid_intensity=round(intensity, 2),
                        joules=round(joules, 2),
                        cpu_request=base_cpu,
                        memory_request=base_mem,
                        cpu_usage_millicores=cpu_usage,
                        memory_usage_bytes=mem_usage,
                        network_receive_bytes=round(random.uniform(100, 50000) * traffic_mult, 2),
                        network_transmit_bytes=round(random.uniform(50, 30000) * traffic_mult, 2),
                        disk_read_bytes=round(random.uniform(0, 10000) * traffic_mult, 2),
                        disk_write_bytes=round(random.uniform(0, 5000) * traffic_mult, 2),
                        storage_request_bytes=1 * 1024**3 if "postgres" in pod_name or "kafka" in pod_name else None,
                        storage_usage_bytes=int(0.4 * 1024**3)
                        if "postgres" in pod_name or "kafka" in pod_name
                        else None,
                        owner_kind=owner_kind,
                        owner_name=owner_name,
                        period=None,
                        timestamp=ts,
                        duration_seconds=3600,
                        grid_intensity_timestamp=ts,
                        node=node,
                        node_instance_type="m5.xlarge" if "arm" not in node else "m6g.large",
                        node_zone="eu-west-1a",
                        emaps_zone=DEMO_ZONE,
                        is_estimated=False,
                        estimation_reasons=[],
                        embodied_co2e_grams=round(embodied_co2e, 4),
                        calculation_version="0.2.3-demo",
                    )
                )

    logger.info("Generated %d combined metric records.", len(metrics))
    return metrics


def generate_recommendations() -> List[RecommendationRecord]:
    """Generate realistic optimization recommendations for the demo.

    Returns:
        A list of RecommendationRecord objects representing various
        optimization opportunities.
    """
    now = datetime.now(timezone.utc)

    recommendations = [
        # Zombie pod: staging load-test runner barely used
        RecommendationRecord(
            pod_name="load-test-runner-jkl78",
            namespace="staging",
            type=RecommendationType.ZOMBIE_POD,
            description="Pod 'load-test-runner-jkl78' in 'staging' has near-zero resource usage. "
            "Consider removing it to save $2.40/day and 12.5g CO2/day.",
            reason="Average CPU usage is 0.02 cores (1% of 2000m request) over the last 7 days.",
            priority="high",
            scope="pod",
            potential_savings_cost=2.40,
            potential_savings_co2e_grams=12.5,
            current_cpu_request_millicores=2000,
            current_memory_request_bytes=4 * 1024**3,
            created_at=now - timedelta(hours=1),
        ),
        # Rightsizing CPU: search-engine is over-provisioned
        RecommendationRecord(
            pod_name="search-engine-9i8j7k-stu34",
            namespace="production",
            type=RecommendationType.RIGHTSIZING_CPU,
            description="Pod 'search-engine' requests 1000m CPU but peak usage is 450m. "
            "Reduce to 550m (with 1.2x headroom) to save $1.08/day.",
            reason="P95 CPU usage over 7 days is 375m (37.5% of request).",
            priority="medium",
            scope="pod",
            potential_savings_cost=1.08,
            potential_savings_co2e_grams=8.3,
            current_cpu_request_millicores=1000,
            recommended_cpu_request_millicores=550,
            created_at=now - timedelta(hours=1),
        ),
        # Rightsizing memory: user-service
        RecommendationRecord(
            pod_name="user-service-5c4d6e-mno90",
            namespace="production",
            type=RecommendationType.RIGHTSIZING_MEMORY,
            description="Pod 'user-service' requests 256Mi memory but peak usage is 89Mi. "
            "Reduce to 110Mi to free cluster resources.",
            reason="P95 memory usage over 7 days is 89Mi (34.7% of request).",
            priority="low",
            scope="pod",
            potential_savings_cost=0.24,
            potential_savings_co2e_grams=1.2,
            current_memory_request_bytes=256 * 1024**2,
            recommended_memory_request_bytes=110 * 1024**2,
            created_at=now - timedelta(hours=1),
        ),
        # Autoscaling candidate: api-gateway has high traffic variance
        RecommendationRecord(
            pod_name="api-gateway-6f7b8c-abc12",
            namespace="production",
            type=RecommendationType.AUTOSCALING_CANDIDATE,
            description="Pod 'api-gateway' shows high CPU usage variance (CV=0.82). "
            "Consider adding an HPA to autoscale between 1-4 replicas.",
            reason="Coefficient of variation of CPU usage is 0.82, above threshold of 0.7.",
            priority="high",
            scope="pod",
            potential_savings_cost=3.50,
            potential_savings_co2e_grams=18.0,
            current_cpu_request_millicores=500,
            created_at=now - timedelta(hours=1),
        ),
        # Off-peak scaling: staging namespace
        RecommendationRecord(
            pod_name="api-gateway-staging-abc12",
            namespace="staging",
            type=RecommendationType.OFF_PEAK_SCALING,
            description="Staging namespace has very low usage between 20:00-08:00 UTC. "
            "Consider scaling down to 0 replicas during off-peak hours.",
            reason="Average CPU utilization drops below 5% for 12 consecutive hours daily.",
            priority="medium",
            scope="namespace",
            potential_savings_cost=1.80,
            potential_savings_co2e_grams=9.5,
            cron_schedule="0 20 * * * scale-down; 0 8 * * * scale-up",
            created_at=now - timedelta(hours=1),
        ),
        # Idle namespace: ci-cd at night
        RecommendationRecord(
            pod_name="jenkins-agent-xyz99",
            namespace="ci-cd",
            type=RecommendationType.IDLE_NAMESPACE,
            description="Namespace 'ci-cd' is idle 65% of the time. "
            "Total energy waste: ~850J/hour during idle periods.",
            reason="Namespace energy consumption below threshold during 65% of observed hours.",
            priority="medium",
            scope="namespace",
            potential_savings_cost=1.20,
            potential_savings_co2e_grams=6.0,
            created_at=now - timedelta(hours=1),
        ),
        # Carbon-aware scheduling
        RecommendationRecord(
            pod_name="spark-driver-abc12",
            namespace="data-pipeline",
            type=RecommendationType.CARBON_AWARE_SCHEDULING,
            description="Batch job 'spark-etl' runs during peak carbon intensity hours. "
            "Shifting to 02:00-06:00 UTC could reduce emissions by ~35%.",
            reason="Current run window (10:00-14:00) has 1.6x higher intensity than off-peak.",
            priority="high",
            scope="pod",
            potential_savings_co2e_grams=45.0,
            created_at=now - timedelta(hours=1),
        ),
        # Underutilized node
        RecommendationRecord(
            pod_name="node-prod-03",
            namespace="cluster",
            type=RecommendationType.UNDERUTILIZED_NODE,
            description="Node 'node-prod-03' (m6g.large) is underutilized at 18% average CPU. "
            "Consider consolidating workloads or downsizing.",
            reason="Average CPU utilization is 18% over the last 7 days.",
            priority="medium",
            scope="node",
            potential_savings_cost=2.16,
            potential_savings_co2e_grams=11.0,
            target_node="node-prod-03",
            created_at=now - timedelta(hours=1),
        ),
    ]

    logger.info("Generated %d demo recommendations.", len(recommendations))
    return recommendations
