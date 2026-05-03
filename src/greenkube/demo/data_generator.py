# src/greenkube/demo/data_generator.py
"""Generate GreenOptic demo data for GreenKube demo mode.

The generated dataset tells a two-year optimization story for GreenOptic,
an eyewear company running its full digital value chain on AWS. The story
highlights three GreenKube outcomes:

1. Carbon-aware region migrations from dirtier AWS regions to cleaner ones.
2. Removal of three overprovisioned nodes, reducing the fleet from 15 to 12.
3. A broad set of applied recommendations with measurable annual savings.
"""

from __future__ import annotations

import logging
import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from greenkube.models.metrics import (
    CombinedMetric,
    RecommendationRecord,
    RecommendationStatus,
    RecommendationType,
)
from greenkube.models.node import NodeInfo

logger = logging.getLogger(__name__)

MIB = 1024**2
GIB = 1024**3

DEMO_ZONE = "FR"
DEMO_HISTORY_DAYS = 730
DEMO_CALCULATION_VERSION = "0.3.0-demo-greenoptic"

INSTANCE_PROFILES: dict[str, dict[str, float]] = {
    "m8g.xlarge": {
        "cpu_capacity_cores": 4.0,
        "memory_capacity_bytes": float(16 * GIB),
        "hourly_cost": 0.154,
        "idle_watts": 58.0,
        "peak_watts": 112.0,
        "embodied_emissions_kg": 285.0,
    },
    "m8g.2xlarge": {
        "cpu_capacity_cores": 8.0,
        "memory_capacity_bytes": float(32 * GIB),
        "hourly_cost": 0.308,
        "idle_watts": 82.0,
        "peak_watts": 156.0,
        "embodied_emissions_kg": 430.0,
    },
}

ZONE_PROFILES: dict[str, dict[str, float]] = {
    "FR": {"base": 25.0, "seasonal": 3.0, "peak": 2.0, "night": -1.5, "weekend": -1.0, "utc_offset": 1.0},
    "ES": {"base": 72.0, "seasonal": 8.0, "peak": 5.0, "night": -4.0, "weekend": -3.0, "utc_offset": 1.0},
    "IE": {
        "base": 274.0,
        "seasonal": 18.0,
        "peak": 12.0,
        "night": -8.0,
        "weekend": -5.0,
        "utc_offset": 0.0,
    },
    "DE": {
        "base": 151.0,
        "seasonal": 14.0,
        "peak": 9.0,
        "night": -7.0,
        "weekend": -4.0,
        "utc_offset": 1.0,
    },
    "US-MIDA-PJM": {
        "base": 392.0,
        "seasonal": 24.0,
        "peak": 15.0,
        "night": -12.0,
        "weekend": -8.0,
        "utc_offset": -5.0,
    },
    "US-MIDW-MISO": {
        "base": 438.0,
        "seasonal": 30.0,
        "peak": 18.0,
        "night": -14.0,
        "weekend": -9.0,
        "utc_offset": -5.0,
    },
    "US-CAL-CISO": {
        "base": 308.0,
        "seasonal": 20.0,
        "peak": 14.0,
        "night": -11.0,
        "weekend": -8.0,
        "utc_offset": -8.0,
    },
    "US-NW-PACW": {
        "base": 114.0,
        "seasonal": 16.0,
        "peak": 8.0,
        "night": -6.0,
        "weekend": -6.0,
        "utc_offset": -8.0,
    },
    "IN-WE": {
        "base": 727.0,
        "seasonal": 40.0,
        "peak": 28.0,
        "night": -18.0,
        "weekend": -10.0,
        "utc_offset": 5.5,
    },
    "JP-TK": {
        "base": 516.0,
        "seasonal": 26.0,
        "peak": 18.0,
        "night": -12.0,
        "weekend": -8.0,
        "utc_offset": 9.0,
    },
    "SG": {
        "base": 438.0,
        "seasonal": 10.0,
        "peak": 12.0,
        "night": -10.0,
        "weekend": -6.0,
        "utc_offset": 8.0,
    },
    "KR": {
        "base": 412.0,
        "seasonal": 22.0,
        "peak": 16.0,
        "night": -11.0,
        "weekend": -7.0,
        "utc_offset": 9.0,
    },
    "JP-KY": {
        "base": 312.0,
        "seasonal": 18.0,
        "peak": 10.0,
        "night": -8.0,
        "weekend": -6.0,
        "utc_offset": 9.0,
    },
}


def _workload(
    pod_name: str,
    owner_kind: str,
    owner_name: str,
    cpu_request: int,
    memory_request_bytes: int,
    *,
    baseline_cpu: int,
    baseline_memory_bytes: int,
    placement: str = "global",
    profile: str = "steady",
    storage_request_bytes: int | None = None,
    storage_usage_ratio: float = 0.65,
    historical_cpu_request: int | None = None,
    historical_memory_request_bytes: int | None = None,
    optimized_days_ago: int | None = None,
    off_peak_after_days_ago: int | None = None,
    batch_shift_after_days_ago: int | None = None,
    ephemeral_storage_request_bytes: int | None = None,
) -> dict[str, Any]:
    return {
        "pod_name": pod_name,
        "owner_kind": owner_kind,
        "owner_name": owner_name,
        "cpu_request": cpu_request,
        "memory_request_bytes": memory_request_bytes,
        "baseline_cpu": baseline_cpu,
        "baseline_memory_bytes": baseline_memory_bytes,
        "placement": placement,
        "profile": profile,
        "storage_request_bytes": storage_request_bytes,
        "storage_usage_ratio": storage_usage_ratio,
        "historical_cpu_request": historical_cpu_request,
        "historical_memory_request_bytes": historical_memory_request_bytes,
        "optimized_days_ago": optimized_days_ago,
        "off_peak_after_days_ago": off_peak_after_days_ago,
        "batch_shift_after_days_ago": batch_shift_after_days_ago,
        "ephemeral_storage_request_bytes": ephemeral_storage_request_bytes,
    }


NODE_STORIES: list[dict[str, Any]] = [
    {
        "name": "eu-optic-core-01",
        "geo": "europe",
        "node_pool": "eu-prod-pool",
        "initial": {"region": "eu-south-2", "zone": "eu-south-2a", "emaps_zone": "ES"},
        "final": {"region": "eu-west-3", "zone": "eu-west-3a", "emaps_zone": "FR"},
        "migration_days_ago": 640,
        "downsized_days_ago": None,
        "final_instance_type": "m8g.2xlarge",
        "retired_days_ago": None,
    },
    {
        "name": "eu-optic-core-02",
        "geo": "europe",
        "node_pool": "eu-prod-pool",
        "initial": {"region": "eu-west-1", "zone": "eu-west-1a", "emaps_zone": "IE"},
        "final": {"region": "eu-west-3", "zone": "eu-west-3b", "emaps_zone": "FR"},
        "migration_days_ago": 632,
        "downsized_days_ago": None,
        "final_instance_type": "m8g.2xlarge",
        "retired_days_ago": None,
    },
    {
        "name": "eu-optic-core-03",
        "geo": "europe",
        "node_pool": "eu-prod-pool",
        "initial": {"region": "eu-central-1", "zone": "eu-central-1a", "emaps_zone": "DE"},
        "final": {"region": "eu-west-3", "zone": "eu-west-3c", "emaps_zone": "FR"},
        "migration_days_ago": 624,
        "downsized_days_ago": None,
        "final_instance_type": "m8g.2xlarge",
        "retired_days_ago": None,
    },
    {
        "name": "eu-optic-core-04",
        "geo": "europe",
        "node_pool": "eu-prod-pool",
        "initial": {"region": "eu-central-1", "zone": "eu-central-1b", "emaps_zone": "DE"},
        "final": {"region": "eu-west-3", "zone": "eu-west-3a", "emaps_zone": "FR"},
        "migration_days_ago": 616,
        "downsized_days_ago": 190,
        "final_instance_type": "m8g.xlarge",
        "retired_days_ago": None,
    },
    {
        "name": "us-optic-edge-01",
        "geo": "us",
        "node_pool": "us-prod-pool",
        "initial": {"region": "us-east-1", "zone": "us-east-1a", "emaps_zone": "US-MIDA-PJM"},
        "final": {"region": "us-west-2", "zone": "us-west-2a", "emaps_zone": "US-NW-PACW"},
        "migration_days_ago": 560,
        "downsized_days_ago": None,
        "final_instance_type": "m8g.2xlarge",
        "retired_days_ago": None,
    },
    {
        "name": "us-optic-edge-02",
        "geo": "us",
        "node_pool": "us-prod-pool",
        "initial": {"region": "us-east-2", "zone": "us-east-2a", "emaps_zone": "US-MIDW-MISO"},
        "final": {"region": "us-west-2", "zone": "us-west-2b", "emaps_zone": "US-NW-PACW"},
        "migration_days_ago": 548,
        "downsized_days_ago": None,
        "final_instance_type": "m8g.2xlarge",
        "retired_days_ago": None,
    },
    {
        "name": "us-optic-edge-03",
        "geo": "us",
        "node_pool": "us-prod-pool",
        "initial": {"region": "us-west-1", "zone": "us-west-1a", "emaps_zone": "US-CAL-CISO"},
        "final": {"region": "us-west-2", "zone": "us-west-2c", "emaps_zone": "US-NW-PACW"},
        "migration_days_ago": 536,
        "downsized_days_ago": 220,
        "final_instance_type": "m8g.xlarge",
        "retired_days_ago": None,
    },
    {
        "name": "us-optic-edge-04",
        "geo": "us",
        "node_pool": "us-prod-pool",
        "initial": {"region": "us-east-1", "zone": "us-east-1b", "emaps_zone": "US-MIDA-PJM"},
        "final": {"region": "us-west-2", "zone": "us-west-2d", "emaps_zone": "US-NW-PACW"},
        "migration_days_ago": 528,
        "downsized_days_ago": 205,
        "final_instance_type": "m8g.xlarge",
        "retired_days_ago": None,
    },
    {
        "name": "ap-optic-edge-01",
        "geo": "asia",
        "node_pool": "ap-prod-pool",
        "initial": {"region": "ap-south-1", "zone": "ap-south-1a", "emaps_zone": "IN-WE"},
        "final": {"region": "ap-northeast-3", "zone": "ap-northeast-3a", "emaps_zone": "JP-KY"},
        "migration_days_ago": 500,
        "downsized_days_ago": None,
        "final_instance_type": "m8g.2xlarge",
        "retired_days_ago": None,
    },
    {
        "name": "ap-optic-edge-02",
        "geo": "asia",
        "node_pool": "ap-prod-pool",
        "initial": {"region": "ap-northeast-1", "zone": "ap-northeast-1a", "emaps_zone": "JP-TK"},
        "final": {"region": "ap-northeast-3", "zone": "ap-northeast-3b", "emaps_zone": "JP-KY"},
        "migration_days_ago": 488,
        "downsized_days_ago": 180,
        "final_instance_type": "m8g.xlarge",
        "retired_days_ago": None,
    },
    {
        "name": "ap-optic-edge-03",
        "geo": "asia",
        "node_pool": "ap-prod-pool",
        "initial": {"region": "ap-southeast-1", "zone": "ap-southeast-1a", "emaps_zone": "SG"},
        "final": {"region": "ap-northeast-3", "zone": "ap-northeast-3c", "emaps_zone": "JP-KY"},
        "migration_days_ago": 476,
        "downsized_days_ago": 175,
        "final_instance_type": "m8g.xlarge",
        "retired_days_ago": None,
    },
    {
        "name": "ap-optic-edge-04",
        "geo": "asia",
        "node_pool": "ap-prod-pool",
        "initial": {"region": "ap-northeast-2", "zone": "ap-northeast-2a", "emaps_zone": "KR"},
        "final": {"region": "ap-northeast-3", "zone": "ap-northeast-3a", "emaps_zone": "JP-KY"},
        "migration_days_ago": 464,
        "downsized_days_ago": 160,
        "final_instance_type": "m8g.xlarge",
        "retired_days_ago": None,
    },
    {
        "name": "eu-optic-buffer-05",
        "geo": "europe",
        "node_pool": "eu-buffer-pool",
        "initial": {"region": "eu-west-1", "zone": "eu-west-1b", "emaps_zone": "IE"},
        "final": {"region": "eu-west-3", "zone": "eu-west-3b", "emaps_zone": "FR"},
        "migration_days_ago": 610,
        "downsized_days_ago": None,
        "final_instance_type": "m8g.2xlarge",
        "retired_days_ago": 320,
    },
    {
        "name": "us-optic-buffer-05",
        "geo": "us",
        "node_pool": "us-buffer-pool",
        "initial": {"region": "us-east-2", "zone": "us-east-2b", "emaps_zone": "US-MIDW-MISO"},
        "final": {"region": "us-west-2", "zone": "us-west-2a", "emaps_zone": "US-NW-PACW"},
        "migration_days_ago": 520,
        "downsized_days_ago": None,
        "final_instance_type": "m8g.2xlarge",
        "retired_days_ago": 300,
    },
    {
        "name": "ap-optic-buffer-05",
        "geo": "asia",
        "node_pool": "ap-buffer-pool",
        "initial": {"region": "ap-south-1", "zone": "ap-south-1b", "emaps_zone": "IN-WE"},
        "final": {"region": "ap-northeast-3", "zone": "ap-northeast-3b", "emaps_zone": "JP-KY"},
        "migration_days_ago": 452,
        "downsized_days_ago": None,
        "final_instance_type": "m8g.2xlarge",
        "retired_days_ago": 280,
    },
]


def _build_demo_workloads() -> dict[str, list[dict[str, Any]]]:
    return {
        "argocd": [
            _workload(
                "argocd-application-controller-0",
                "StatefulSet",
                "argocd-application-controller",
                300,
                512 * MIB,
                baseline_cpu=12,
                baseline_memory_bytes=302 * MIB,
                profile="steady",
            ),
            _workload(
                "argocd-repo-server-0",
                "Deployment",
                "argocd-repo-server",
                250,
                256 * MIB,
                baseline_cpu=15,
                baseline_memory_bytes=122 * MIB,
                profile="steady",
            ),
            _workload(
                "argocd-server-0",
                "Deployment",
                "argocd-server",
                200,
                160 * MIB,
                baseline_cpu=8,
                baseline_memory_bytes=56 * MIB,
                profile="office",
            ),
            _workload(
                "argocd-dex-server-0",
                "Deployment",
                "argocd-dex-server",
                150,
                192 * MIB,
                baseline_cpu=6,
                baseline_memory_bytes=115 * MIB,
                profile="steady",
            ),
        ],
        "monitoring": [
            _workload(
                "prometheus-k8s-0",
                "StatefulSet",
                "prometheus-k8s",
                800,
                2 * GIB,
                baseline_cpu=70,
                baseline_memory_bytes=980 * MIB,
                profile="steady",
                storage_request_bytes=500 * GIB,
                storage_usage_ratio=0.72,
            ),
            _workload(
                "prometheus-k8s-1",
                "StatefulSet",
                "prometheus-k8s",
                800,
                2 * GIB,
                baseline_cpu=62,
                baseline_memory_bytes=985 * MIB,
                profile="steady",
                storage_request_bytes=500 * GIB,
                storage_usage_ratio=0.7,
            ),
            _workload(
                "grafana-0",
                "Deployment",
                "grafana",
                300,
                256 * MIB,
                baseline_cpu=35,
                baseline_memory_bytes=150 * MIB,
                profile="office",
            ),
            _workload(
                "alertmanager-main-0",
                "StatefulSet",
                "alertmanager-main",
                150,
                128 * MIB,
                baseline_cpu=12,
                baseline_memory_bytes=64 * MIB,
                profile="steady",
            ),
            _workload(
                "kube-state-metrics-0",
                "Deployment",
                "kube-state-metrics",
                150,
                192 * MIB,
                baseline_cpu=14,
                baseline_memory_bytes=118 * MIB,
                profile="steady",
            ),
            _workload(
                "prometheus-adapter-0",
                "Deployment",
                "prometheus-adapter",
                150,
                192 * MIB,
                baseline_cpu=10,
                baseline_memory_bytes=90 * MIB,
                profile="steady",
            ),
        ],
        "kube-system": [
            _workload(
                "aws-node-0",
                "DaemonSet",
                "aws-node",
                180,
                192 * MIB,
                baseline_cpu=20,
                baseline_memory_bytes=160 * MIB,
                profile="steady",
            ),
            _workload(
                "aws-node-1",
                "DaemonSet",
                "aws-node",
                180,
                192 * MIB,
                baseline_cpu=18,
                baseline_memory_bytes=155 * MIB,
                profile="steady",
            ),
            _workload(
                "ebs-csi-controller-0",
                "Deployment",
                "ebs-csi-controller",
                220,
                192 * MIB,
                baseline_cpu=18,
                baseline_memory_bytes=120 * MIB,
                profile="steady",
            ),
            _workload(
                "ebs-csi-node-0",
                "DaemonSet",
                "ebs-csi-node",
                150,
                160 * MIB,
                baseline_cpu=12,
                baseline_memory_bytes=110 * MIB,
                profile="steady",
            ),
            _workload(
                "coredns-0",
                "Deployment",
                "coredns",
                100,
                128 * MIB,
                baseline_cpu=8,
                baseline_memory_bytes=58 * MIB,
                profile="steady",
            ),
            _workload(
                "metrics-server-0",
                "Deployment",
                "metrics-server",
                120,
                128 * MIB,
                baseline_cpu=10,
                baseline_memory_bytes=82 * MIB,
                profile="steady",
            ),
        ],
        "minio": [
            _workload(
                "minio-0",
                "StatefulSet",
                "minio",
                500,
                4 * GIB,
                baseline_cpu=68,
                baseline_memory_bytes=3123 * MIB,
                profile="storage",
                storage_request_bytes=1024 * GIB,
                storage_usage_ratio=0.68,
            ),
            _workload(
                "minio-1",
                "StatefulSet",
                "minio",
                500,
                4 * GIB,
                baseline_cpu=54,
                baseline_memory_bytes=3342 * MIB,
                profile="storage",
                storage_request_bytes=1024 * GIB,
                storage_usage_ratio=0.7,
            ),
            _workload(
                "minio-2",
                "StatefulSet",
                "minio",
                500,
                4 * GIB,
                baseline_cpu=58,
                baseline_memory_bytes=3303 * MIB,
                profile="storage",
                storage_request_bytes=1024 * GIB,
                storage_usage_ratio=0.69,
            ),
            _workload(
                "minio-3",
                "StatefulSet",
                "minio",
                500,
                4 * GIB,
                baseline_cpu=92,
                baseline_memory_bytes=3158 * MIB,
                profile="storage",
                storage_request_bytes=1024 * GIB,
                storage_usage_ratio=0.67,
            ),
        ],
        "dremio": [
            _workload(
                "dremio-master-0",
                "StatefulSet",
                "dremio-master",
                700,
                3 * GIB,
                baseline_cpu=50,
                baseline_memory_bytes=2630 * MIB,
                profile="analytics",
                placement="global",
                storage_request_bytes=250 * GIB,
                storage_usage_ratio=0.55,
            ),
            _workload(
                "dremio-coordinator-0",
                "StatefulSet",
                "dremio-coordinator",
                600,
                3 * GIB,
                baseline_cpu=40,
                baseline_memory_bytes=2174 * MIB,
                profile="analytics",
                placement="global",
                ephemeral_storage_request_bytes=40 * GIB,
            ),
            _workload(
                "dremio-executor-0",
                "StatefulSet",
                "dremio-executor",
                900,
                4 * GIB,
                baseline_cpu=55,
                baseline_memory_bytes=3793 * MIB,
                profile="batch",
                batch_shift_after_days_ago=210,
                placement="global",
                ephemeral_storage_request_bytes=60 * GIB,
            ),
            _workload(
                "dremio-executor-1",
                "StatefulSet",
                "dremio-executor",
                900,
                4 * GIB,
                baseline_cpu=48,
                baseline_memory_bytes=3165 * MIB,
                profile="batch",
                batch_shift_after_days_ago=210,
                placement="global",
                ephemeral_storage_request_bytes=60 * GIB,
            ),
            _workload(
                "zk-0",
                "StatefulSet",
                "zk",
                120,
                256 * MIB,
                baseline_cpu=18,
                baseline_memory_bytes=137 * MIB,
                profile="steady",
                placement="global",
            ),
        ],
        "mageai": [
            _workload(
                "mageai-0",
                "Deployment",
                "mageai",
                1200,
                20 * GIB,
                baseline_cpu=80,
                baseline_memory_bytes=16277 * MIB,
                profile="batch",
                placement="global",
                historical_cpu_request=1800,
                historical_memory_request_bytes=24 * GIB,
                optimized_days_ago=210,
                batch_shift_after_days_ago=210,
                ephemeral_storage_request_bytes=80 * GIB,
            ),
            _workload(
                "mageai-trigger-0",
                "Deployment",
                "mageai-trigger",
                200,
                256 * MIB,
                baseline_cpu=10,
                baseline_memory_bytes=96 * MIB,
                profile="office",
            ),
        ],
        "superset": [
            _workload(
                "superset-0",
                "Deployment",
                "superset",
                400,
                1 * GIB,
                baseline_cpu=18,
                baseline_memory_bytes=688 * MIB,
                profile="office",
            ),
            _workload(
                "superset-worker-0",
                "Deployment",
                "superset-worker",
                600,
                1 * GIB,
                baseline_cpu=42,
                baseline_memory_bytes=709 * MIB,
                profile="office",
                off_peak_after_days_ago=260,
            ),
            _workload(
                "superset-worker-1",
                "Deployment",
                "superset-worker",
                600,
                1 * GIB,
                baseline_cpu=46,
                baseline_memory_bytes=706 * MIB,
                profile="office",
                off_peak_after_days_ago=260,
            ),
            _workload(
                "superset-postgresql-0",
                "StatefulSet",
                "superset-postgresql",
                200,
                256 * MIB,
                baseline_cpu=18,
                baseline_memory_bytes=92 * MIB,
                profile="steady",
                storage_request_bytes=150 * GIB,
                storage_usage_ratio=0.58,
            ),
            _workload(
                "superset-redis-master-0",
                "StatefulSet",
                "superset-redis-master",
                100,
                128 * MIB,
                baseline_cpu=8,
                baseline_memory_bytes=20 * MIB,
                profile="steady",
            ),
        ],
        "datahub": [
            _workload(
                "datahub-datahub-gms-0",
                "StatefulSet",
                "datahub-gms",
                1200,
                1536 * MIB,
                baseline_cpu=45,
                baseline_memory_bytes=1098 * MIB,
                profile="steady",
                historical_cpu_request=1800,
                historical_memory_request_bytes=2 * GIB,
                optimized_days_ago=480,
            ),
            _workload(
                "datahub-datahub-frontend-0",
                "Deployment",
                "datahub-frontend",
                400,
                768 * MIB,
                baseline_cpu=20,
                baseline_memory_bytes=454 * MIB,
                profile="web",
            ),
            _workload(
                "datahub-acryl-datahub-actions-0",
                "Deployment",
                "datahub-actions",
                250,
                384 * MIB,
                baseline_cpu=12,
                baseline_memory_bytes=263 * MIB,
                profile="steady",
            ),
            _workload(
                "datahub-prerequisites-kafka-0",
                "StatefulSet",
                "datahub-kafka",
                500,
                1536 * MIB,
                baseline_cpu=55,
                baseline_memory_bytes=1226 * MIB,
                profile="steady",
                storage_request_bytes=400 * GIB,
                storage_usage_ratio=0.62,
            ),
            _workload(
                "datahub-prerequisites-kafka-1",
                "StatefulSet",
                "datahub-kafka",
                500,
                1536 * MIB,
                baseline_cpu=72,
                baseline_memory_bytes=1223 * MIB,
                profile="steady",
                storage_request_bytes=400 * GIB,
                storage_usage_ratio=0.66,
            ),
            _workload(
                "datahub-prerequisites-mysql-0",
                "StatefulSet",
                "datahub-mysql",
                250,
                1 * GIB,
                baseline_cpu=28,
                baseline_memory_bytes=755 * MIB,
                profile="steady",
                storage_request_bytes=200 * GIB,
                storage_usage_ratio=0.6,
            ),
            _workload(
                "opensearch-cluster-master-0",
                "StatefulSet",
                "opensearch-cluster-master",
                600,
                3 * GIB,
                baseline_cpu=40,
                baseline_memory_bytes=2850 * MIB,
                profile="search",
                storage_request_bytes=500 * GIB,
                storage_usage_ratio=0.64,
            ),
        ],
        "website": [
            _workload(
                "storefront-web-eu-0",
                "Deployment",
                "storefront-web-eu",
                700,
                768 * MIB,
                baseline_cpu=180,
                baseline_memory_bytes=430 * MIB,
                profile="web",
                placement="europe",
                historical_cpu_request=900,
                optimized_days_ago=310,
            ),
            _workload(
                "storefront-web-us-0",
                "Deployment",
                "storefront-web-us",
                700,
                768 * MIB,
                baseline_cpu=190,
                baseline_memory_bytes=445 * MIB,
                profile="web",
                placement="us",
                historical_cpu_request=950,
                optimized_days_ago=310,
            ),
            _workload(
                "storefront-web-ap-0",
                "Deployment",
                "storefront-web-ap",
                600,
                768 * MIB,
                baseline_cpu=150,
                baseline_memory_bytes=410 * MIB,
                profile="web",
                placement="asia",
                historical_cpu_request=850,
                optimized_days_ago=310,
            ),
        ],
        "commerce": [
            _workload(
                "checkout-api-eu-0",
                "Deployment",
                "checkout-api-eu",
                600,
                768 * MIB,
                baseline_cpu=120,
                baseline_memory_bytes=360 * MIB,
                profile="api",
                placement="europe",
            ),
            _workload(
                "checkout-api-us-0",
                "Deployment",
                "checkout-api-us",
                600,
                768 * MIB,
                baseline_cpu=125,
                baseline_memory_bytes=370 * MIB,
                profile="api",
                placement="us",
            ),
            _workload(
                "checkout-api-ap-0",
                "Deployment",
                "checkout-api-ap",
                550,
                768 * MIB,
                baseline_cpu=105,
                baseline_memory_bytes=350 * MIB,
                profile="api",
                placement="asia",
            ),
            _workload(
                "pricing-service-0",
                "Deployment",
                "pricing-service",
                550,
                512 * MIB,
                baseline_cpu=88,
                baseline_memory_bytes=300 * MIB,
                profile="api",
                placement="global",
                historical_cpu_request=900,
                optimized_days_ago=140,
            ),
        ],
        "manufacturing": [
            _workload(
                "lens-configurator-0",
                "Deployment",
                "lens-configurator",
                500,
                768 * MIB,
                baseline_cpu=82,
                baseline_memory_bytes=350 * MIB,
                profile="office",
                placement="europe",
                historical_memory_request_bytes=1024 * MIB,
                optimized_days_ago=330,
            ),
            _workload(
                "factory-scheduler-0",
                "Deployment",
                "factory-scheduler",
                450,
                512 * MIB,
                baseline_cpu=70,
                baseline_memory_bytes=280 * MIB,
                profile="office",
                placement="europe",
                off_peak_after_days_ago=240,
            ),
            _workload(
                "qa-traceability-0",
                "Deployment",
                "qa-traceability",
                350,
                512 * MIB,
                baseline_cpu=56,
                baseline_memory_bytes=260 * MIB,
                profile="steady",
                placement="europe",
            ),
        ],
        "retail": [
            _workload(
                "pos-sync-0",
                "Deployment",
                "pos-sync",
                400,
                512 * MIB,
                baseline_cpu=82,
                baseline_memory_bytes=280 * MIB,
                profile="office",
                placement="us",
            ),
            _workload(
                "stock-reservation-0",
                "Deployment",
                "stock-reservation",
                350,
                512 * MIB,
                baseline_cpu=74,
                baseline_memory_bytes=300 * MIB,
                profile="api",
                placement="global",
            ),
        ],
        "platform": [
            _workload(
                "api-gateway-0",
                "Deployment",
                "api-gateway",
                900,
                768 * MIB,
                baseline_cpu=220,
                baseline_memory_bytes=420 * MIB,
                profile="web",
                placement="global",
                historical_cpu_request=1200,
                optimized_days_ago=210,
            ),
            _workload(
                "identity-service-0",
                "Deployment",
                "identity-service",
                400,
                512 * MIB,
                baseline_cpu=66,
                baseline_memory_bytes=260 * MIB,
                profile="api",
                placement="global",
            ),
            _workload(
                "notification-service-0",
                "Deployment",
                "notification-service",
                300,
                384 * MIB,
                baseline_cpu=36,
                baseline_memory_bytes=220 * MIB,
                profile="burst",
                placement="global",
            ),
            _workload(
                "catalog-search-0",
                "Deployment",
                "catalog-search",
                650,
                1024 * MIB,
                baseline_cpu=95,
                baseline_memory_bytes=600 * MIB,
                profile="search",
                placement="global",
                historical_cpu_request=1000,
                historical_memory_request_bytes=1280 * MIB,
                optimized_days_ago=None,
            ),
        ],
    }


DEMO_WORKLOADS = _build_demo_workloads()


def _stable_rng(*parts: object) -> random.Random:
    return random.Random("|".join(str(part) for part in parts))


def _node_state_for_days_ago(story: dict[str, Any], days_ago: float) -> dict[str, Any] | None:
    retired_days_ago = story.get("retired_days_ago")
    if retired_days_ago is not None and days_ago < retired_days_ago:
        return None

    location_key = "initial" if days_ago >= story["migration_days_ago"] else "final"
    location = story[location_key]

    instance_type = "m8g.2xlarge"
    downsized_days_ago = story.get("downsized_days_ago")
    if downsized_days_ago is not None and days_ago < downsized_days_ago:
        instance_type = story["final_instance_type"]
    elif downsized_days_ago is None:
        instance_type = story["final_instance_type"]

    profile = INSTANCE_PROFILES[instance_type]
    return {
        "name": story["name"],
        "geo": story["geo"],
        "instance_type": instance_type,
        "zone": location["zone"],
        "region": location["region"],
        "emaps_zone": location["emaps_zone"],
        "cloud_provider": "aws",
        "architecture": "arm64",
        "node_pool": story["node_pool"],
        "cpu_capacity_cores": profile["cpu_capacity_cores"],
        "memory_capacity_bytes": int(profile["memory_capacity_bytes"]),
        "hourly_cost": profile["hourly_cost"],
        "idle_watts": profile["idle_watts"],
        "peak_watts": profile["peak_watts"],
        "embodied_emissions_kg": profile["embodied_emissions_kg"],
    }


def _current_node_topology() -> list[dict[str, Any]]:
    current_nodes = []
    for story in NODE_STORIES:
        state = _node_state_for_days_ago(story, 0.0)
        if state is None:
            continue
        current_nodes.append(
            {
                "name": state["name"],
                "instance_type": state["instance_type"],
                "zone": state["zone"],
                "region": state["region"],
                "cloud_provider": state["cloud_provider"],
                "architecture": state["architecture"],
                "node_pool": state["node_pool"],
                "cpu_capacity_cores": state["cpu_capacity_cores"],
                "memory_capacity_bytes": state["memory_capacity_bytes"],
                "embodied_emissions_kg": state["embodied_emissions_kg"],
            }
        )
    return current_nodes


DEMO_NODES = _current_node_topology()


def _local_hour(ts: datetime, placement: str) -> int:
    offsets = {"global": 0.0, "europe": 1.0, "us": -7.0, "asia": 9.0}
    local_ts = ts + timedelta(hours=offsets.get(placement, 0.0))
    return local_ts.hour


def _zone_intensity(zone: str, ts: datetime) -> float:
    profile = ZONE_PROFILES[zone]
    local_ts = ts + timedelta(hours=profile["utc_offset"])
    hour = local_ts.hour
    seasonal = math.sin(2 * math.pi * local_ts.timetuple().tm_yday / 365.25) * profile["seasonal"]
    peak = profile["peak"] if 8 <= hour < 21 else 0.0
    night = profile["night"] if hour < 6 else 0.0
    weekend = profile["weekend"] if local_ts.weekday() >= 5 else 0.0
    jitter = _stable_rng(zone, local_ts.isoformat()).uniform(-6.0, 6.0)
    return round(max(12.0, profile["base"] + seasonal + peak + night + weekend + jitter), 2)


def _build_metric_timestamps(days: int) -> list[tuple[datetime, int, bool]]:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    timestamps: list[tuple[datetime, int, bool]] = []

    for hours_ago in range(days * 24):
        timestamps.append((now - timedelta(hours=hours_ago), 3600, False))

    for day_offset in range(days, DEMO_HISTORY_DAYS + 1):
        timestamps.append(((now - timedelta(days=day_offset)).replace(hour=12), 86400, True))

    return timestamps


def _request_state(workload: dict[str, Any], days_ago: float, local_hour: int) -> tuple[int, int]:
    cpu_request = int(workload["cpu_request"])
    memory_request = int(workload["memory_request_bytes"])

    optimized_days_ago = workload.get("optimized_days_ago")
    if optimized_days_ago is not None and days_ago >= optimized_days_ago:
        cpu_request = int(workload.get("historical_cpu_request") or cpu_request)
        memory_request = int(workload.get("historical_memory_request_bytes") or memory_request)

    off_peak_after_days_ago = workload.get("off_peak_after_days_ago")
    if (
        off_peak_after_days_ago is not None
        and days_ago < off_peak_after_days_ago
        and (local_hour >= 20 or local_hour < 7)
    ):
        cpu_request = max(50, int(cpu_request * 0.4))
        memory_request = max(256 * MIB, int(memory_request * 0.55))

    return cpu_request, memory_request


def _activity_multiplier(workload: dict[str, Any], ts: datetime, days_ago: float) -> float:
    profile = str(workload["profile"])
    placement = str(workload["placement"])
    local_hour = _local_hour(ts, placement)
    offsets = {"global": 0, "europe": 1, "us": -7, "asia": 9}
    weekend = (ts + timedelta(hours=offsets[placement])).weekday() >= 5
    jitter = _stable_rng(workload["pod_name"], ts.isoformat()).uniform(0.92, 1.08)

    if profile == "web":
        if 7 <= local_hour < 23:
            base = 0.9
            if 11 <= local_hour < 14 or 18 <= local_hour < 22:
                base = 1.25
        else:
            base = 0.38
        if weekend:
            base *= 1.05
    elif profile == "api":
        if 6 <= local_hour < 23:
            base = 0.85
            if 10 <= local_hour < 14:
                base = 1.0
        else:
            base = 0.35
    elif profile == "search":
        base = 0.92 if 7 <= local_hour < 23 else 0.4
    elif profile == "office":
        base = 0.95 if (7 <= local_hour < 19 and not weekend) else 0.22
    elif profile == "analytics":
        base = 0.85 if (8 <= local_hour < 20 and not weekend) else 0.42
    elif profile == "batch":
        shifted_after = workload.get("batch_shift_after_days_ago")
        active_window = range(1, 5) if shifted_after is not None and days_ago < shifted_after else range(10, 14)
        base = 1.45 if local_hour in active_window else 0.08
    elif profile == "storage":
        base = 0.72 if 7 <= local_hour < 23 else 0.58
    elif profile == "burst":
        base = 1.35 if local_hour in {8, 9, 12, 18, 19} else 0.55
    else:
        base = 0.75 if not weekend else 0.68

    off_peak_after_days_ago = workload.get("off_peak_after_days_ago")
    if (
        off_peak_after_days_ago is not None
        and days_ago < off_peak_after_days_ago
        and (local_hour >= 20 or local_hour < 7)
    ):
        base *= 0.45

    return max(0.05, base * jitter)


def _candidate_nodes(active_nodes: list[dict[str, Any]], placement: str) -> list[dict[str, Any]]:
    if placement == "global":
        return active_nodes
    return [node for node in active_nodes if node["geo"] == placement]


def _select_node(active_nodes: list[dict[str, Any]], workload: dict[str, Any], ts: datetime) -> dict[str, Any]:
    placement = str(workload["placement"])
    candidates = _candidate_nodes(active_nodes, placement) or active_nodes
    bucket = f"{ts.isocalendar().year}-{ts.isocalendar().week}-{placement}"
    return candidates[_stable_rng(workload["pod_name"], bucket).randrange(len(candidates))]


def _resource_rates(
    profile: str, activity: float, storage_request_bytes: int | None
) -> tuple[float, float, float, float]:
    network_base = {
        "web": (75000.0, 68000.0),
        "api": (42000.0, 36000.0),
        "search": (52000.0, 43000.0),
        "analytics": (18000.0, 16000.0),
        "batch": (9000.0, 9000.0),
        "storage": (30000.0, 28000.0),
        "office": (12000.0, 9000.0),
        "burst": (28000.0, 25000.0),
        "steady": (10000.0, 8000.0),
    }
    disk_base = {
        "web": (1200.0, 900.0),
        "api": (900.0, 700.0),
        "search": (3500.0, 2200.0),
        "analytics": (9000.0, 7000.0),
        "batch": (14000.0, 12000.0),
        "storage": (16000.0, 18000.0),
        "office": (700.0, 500.0),
        "burst": (2200.0, 1800.0),
        "steady": (800.0, 700.0),
    }
    rx_base, tx_base = network_base.get(profile, network_base["steady"])
    read_base, write_base = disk_base.get(profile, disk_base["steady"])
    if storage_request_bytes:
        read_base *= 1.2
        write_base *= 1.3
    return rx_base * activity, tx_base * activity, read_base * activity, write_base * activity


def generate_node_snapshots(days: int = 30) -> list[NodeInfo]:
    """Generate SCD2 change-point snapshots for the GreenOptic story."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    snapshots: list[NodeInfo] = []

    for story in NODE_STORIES:
        transition_points = [
            (DEMO_HISTORY_DAYS, DEMO_HISTORY_DAYS, True),
            (story["migration_days_ago"], max(0.0, story["migration_days_ago"] - 0.01), True),
        ]

        downsized_days_ago = story.get("downsized_days_ago")
        if downsized_days_ago is not None:
            transition_points.append((downsized_days_ago, max(0.0, downsized_days_ago - 0.01), True))

        retired_days_ago = story.get("retired_days_ago")
        if retired_days_ago is not None:
            transition_points.append((retired_days_ago, retired_days_ago, False))

        for event_days_ago, state_days_ago, is_active in transition_points:
            state = _node_state_for_days_ago(story, state_days_ago)
            if state is None:
                continue

            snapshots.append(
                NodeInfo(
                    name=state["name"],
                    instance_type=state["instance_type"],
                    zone=state["zone"],
                    region=state["region"],
                    cloud_provider=state["cloud_provider"],
                    architecture=state["architecture"],
                    node_pool=state["node_pool"],
                    cpu_capacity_cores=state["cpu_capacity_cores"],
                    memory_capacity_bytes=state["memory_capacity_bytes"],
                    is_active=is_active,
                    timestamp=now - timedelta(days=event_days_ago),
                    embodied_emissions_kg=state["embodied_emissions_kg"],
                )
            )

    snapshots.sort(key=lambda snapshot: snapshot.timestamp or now)

    logger.info(
        "Generated %d node snapshots across %d current nodes and %d historical nodes.",
        len(snapshots),
        len(DEMO_NODES),
        len(NODE_STORIES),
    )
    return snapshots


def generate_carbon_intensity_history(days: int = 30) -> list[dict[str, Any]]:
    """Generate two years of multi-zone carbon intensity history."""
    records: list[dict[str, Any]] = []
    for ts, _, is_estimated in _build_metric_timestamps(days):
        for zone in ZONE_PROFILES:
            intensity = _zone_intensity(zone, ts)
            records.append(
                {
                    "zone": zone,
                    "carbonIntensity": intensity,
                    "datetime": ts.isoformat(),
                    "updatedAt": ts.isoformat(),
                    "createdAt": ts.isoformat(),
                    "emissionFactorType": "lifecycle",
                    "isEstimated": is_estimated,
                    "estimationMethod": "story_profile" if is_estimated else None,
                }
            )

    logger.info("Generated %d carbon intensity records across %d zones.", len(records), len(ZONE_PROFILES))
    return records


def generate_combined_metrics(days: int = 30) -> list[CombinedMetric]:
    """Generate realistic pod metrics for the GreenOptic demo story."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    total_workloads = sum(len(workloads) for workloads in DEMO_WORKLOADS.values())
    metrics: list[CombinedMetric] = []

    for ts, duration_seconds, is_estimated in _build_metric_timestamps(days):
        days_ago = (now - ts).total_seconds() / 86400.0
        active_nodes = [
            state for story in NODE_STORIES if (state := _node_state_for_days_ago(story, days_ago)) is not None
        ]
        idle_power_per_workload = sum(node["idle_watts"] for node in active_nodes) / total_workloads
        idle_cost_per_workload = sum(node["hourly_cost"] * 0.38 for node in active_nodes) / total_workloads
        duration_hours = duration_seconds / 3600.0

        for namespace, workloads in DEMO_WORKLOADS.items():
            for workload in workloads:
                placement = str(workload["placement"])
                local_hour = _local_hour(ts, placement)
                cpu_request, memory_request = _request_state(workload, days_ago, local_hour)
                activity = _activity_multiplier(workload, ts, days_ago)
                node_state = _select_node(active_nodes, workload, ts)
                cpu_capacity_m = int(node_state["cpu_capacity_cores"] * 1000)
                memory_capacity = int(node_state["memory_capacity_bytes"])

                baseline_cpu = int(workload["baseline_cpu"])
                baseline_memory = int(workload["baseline_memory_bytes"])
                cpu_usage = max(1, min(cpu_request, int(baseline_cpu * activity)))
                memory_usage = max(64 * MIB, min(memory_request, int(baseline_memory * (0.72 + 0.18 * activity))))

                cpu_fraction = min(1.0, cpu_usage / cpu_capacity_m)
                memory_fraction = min(1.0, memory_usage / memory_capacity)
                request_share = min(1.0, max(cpu_request / cpu_capacity_m, memory_request / memory_capacity))

                variable_watts = max(
                    0.0,
                    (node_state["peak_watts"] - node_state["idle_watts"] * 0.55) * cpu_fraction
                    + node_state["idle_watts"] * 0.12 * memory_fraction,
                )
                power_watts = idle_power_per_workload + variable_watts
                joules = power_watts * duration_seconds

                pue_base = {"europe": 1.14, "us": 1.16, "asia": 1.18}.get(node_state["geo"], 1.16)
                pue = round(pue_base + _stable_rng(node_state["name"], ts.date()).uniform(-0.02, 0.05), 3)
                intensity = _zone_intensity(node_state["emaps_zone"], ts)
                co2e_grams = (joules / 3.6e6) * pue * intensity

                embodied_hourly = node_state["embodied_emissions_kg"] * 1000 / (4 * 365.25 * 24)
                embodied_co2e = embodied_hourly * duration_hours * max(0.08, request_share)

                storage_request_bytes = workload.get("storage_request_bytes")
                storage_usage_bytes = None
                if storage_request_bytes:
                    storage_request_bytes = int(storage_request_bytes)
                    storage_usage_bytes = int(storage_request_bytes * float(workload.get("storage_usage_ratio", 0.65)))

                rx_rate, tx_rate, read_rate, write_rate = _resource_rates(
                    str(workload["profile"]), activity, storage_request_bytes
                )
                storage_cost = ((storage_request_bytes or 0) / GIB) * 0.00012 * duration_hours
                network_cost = (rx_rate + tx_rate) / (1024**3) * 0.015 * duration_hours
                compute_cost = (
                    idle_cost_per_workload * duration_hours
                    + node_state["hourly_cost"] * (0.55 * request_share + 0.35 * cpu_fraction) * duration_hours
                )
                total_cost = compute_cost + storage_cost + network_cost

                metrics.append(
                    CombinedMetric(
                        pod_name=str(workload["pod_name"]),
                        namespace=namespace,
                        total_cost=round(total_cost, 6),
                        co2e_grams=round(co2e_grams, 4),
                        pue=pue,
                        grid_intensity=intensity,
                        joules=round(joules, 2),
                        cpu_request=cpu_request,
                        memory_request=memory_request,
                        sample_count=max(1, int(duration_hours)),
                        cpu_usage_millicores=cpu_usage,
                        cpu_usage_max_millicores=min(cpu_request, int(cpu_usage * 1.16)),
                        memory_usage_bytes=memory_usage,
                        memory_usage_max_bytes=min(memory_request, int(memory_usage * 1.08)),
                        network_receive_bytes=round(rx_rate, 2),
                        network_transmit_bytes=round(tx_rate, 2),
                        disk_read_bytes=round(read_rate, 2),
                        disk_write_bytes=round(write_rate, 2),
                        storage_request_bytes=storage_request_bytes,
                        storage_usage_bytes=storage_usage_bytes,
                        ephemeral_storage_request_bytes=workload.get("ephemeral_storage_request_bytes"),
                        ephemeral_storage_usage_bytes=(
                            int(int(workload["ephemeral_storage_request_bytes"]) * 0.55)
                            if workload.get("ephemeral_storage_request_bytes")
                            else None
                        ),
                        restart_count=_stable_rng(workload["pod_name"], ts.date()).randint(0, 1),
                        owner_kind=str(workload["owner_kind"]),
                        owner_name=str(workload["owner_name"]),
                        period=None,
                        timestamp=ts,
                        duration_seconds=duration_seconds,
                        grid_intensity_timestamp=ts,
                        node=node_state["name"],
                        node_instance_type=node_state["instance_type"],
                        node_zone=node_state["zone"],
                        emaps_zone=node_state["emaps_zone"],
                        is_estimated=is_estimated,
                        estimation_reasons=["daily_story_bucket"] if is_estimated else [],
                        embodied_co2e_grams=round(embodied_co2e, 4),
                        calculation_version=DEMO_CALCULATION_VERSION,
                    )
                )

    logger.info("Generated %d combined metric records.", len(metrics))
    return metrics


def _applied_recommendation(**kwargs: Any) -> RecommendationRecord:
    return RecommendationRecord(status=RecommendationStatus.APPLIED, updated_at=kwargs.get("applied_at"), **kwargs)


def _active_recommendation(**kwargs: Any) -> RecommendationRecord:
    return RecommendationRecord(status=RecommendationStatus.ACTIVE, **kwargs)


def generate_recommendations() -> list[RecommendationRecord]:
    """Generate a historical recommendation ledger for the GreenOptic story."""
    now = datetime.now(timezone.utc)

    recommendations = [
        _applied_recommendation(
            pod_name="lens-review-sandbox-0",
            namespace="design-review",
            type=RecommendationType.ZOMBIE_POD,
            description="The abandoned design-review sandbox was removed after 45 idle days.",
            reason="The pod had no inbound traffic, no completed user sessions, and <1% CPU for six weeks.",
            priority="high",
            scope="pod",
            potential_savings_cost=620.0,
            potential_savings_co2e_grams=1850000.0,
            current_cpu_request_millicores=800,
            current_memory_request_bytes=2 * GIB,
            actual_cpu_request_millicores=0,
            actual_memory_request_bytes=0,
            carbon_saved_co2e_grams=1720000.0,
            cost_saved=580.0,
            created_at=now - timedelta(days=710),
            applied_at=now - timedelta(days=700),
        ),
        _applied_recommendation(
            pod_name="datahub-datahub-gms-0",
            namespace="datahub",
            type=RecommendationType.RIGHTSIZING_CPU,
            description="DataHub GMS was rightsized from 1800m to 1200m after six months of sub-50% usage.",
            reason="P95 CPU stayed below 930m while the service footprint was stable.",
            priority="high",
            scope="pod",
            potential_savings_cost=1480.0,
            potential_savings_co2e_grams=1050000.0,
            current_cpu_request_millicores=1800,
            recommended_cpu_request_millicores=1200,
            actual_cpu_request_millicores=1200,
            carbon_saved_co2e_grams=980000.0,
            cost_saved=1320.0,
            created_at=now - timedelta(days=500),
            applied_at=now - timedelta(days=480),
        ),
        _applied_recommendation(
            pod_name="lens-configurator-0",
            namespace="manufacturing",
            type=RecommendationType.RIGHTSIZING_MEMORY,
            description="The lens configurator now runs with 768Mi instead of 1Gi without swap pressure.",
            reason="P95 memory stabilized at 512Mi after the rendering cache was externalized to MinIO.",
            priority="medium",
            scope="pod",
            potential_savings_cost=460.0,
            potential_savings_co2e_grams=255000.0,
            current_memory_request_bytes=1024 * MIB,
            recommended_memory_request_bytes=768 * MIB,
            actual_memory_request_bytes=768 * MIB,
            carbon_saved_co2e_grams=220000.0,
            cost_saved=410.0,
            created_at=now - timedelta(days=360),
            applied_at=now - timedelta(days=330),
        ),
        _applied_recommendation(
            pod_name="storefront-web-us-0",
            namespace="website",
            type=RecommendationType.AUTOSCALING_CANDIDATE,
            description="A latency-aware HPA was rolled out on the US storefront to absorb lunchtime spikes.",
            reason="The service had a coefficient of variation above 0.9 with sharp regional peaks around 18:00 UTC.",
            priority="high",
            scope="pod",
            potential_savings_cost=920.0,
            potential_savings_co2e_grams=365000.0,
            current_cpu_request_millicores=950,
            actual_cpu_request_millicores=700,
            carbon_saved_co2e_grams=310000.0,
            cost_saved=860.0,
            created_at=now - timedelta(days=340),
            applied_at=now - timedelta(days=310),
        ),
        _applied_recommendation(
            pod_name="superset-worker-1",
            namespace="superset",
            type=RecommendationType.OFF_PEAK_SCALING,
            description=(
                "Superset workers now scale down overnight and on weekends outside executive reporting windows."
            ),
            reason="The BI workload was idle for 14 hours per weekday and almost fully idle on Saturdays.",
            priority="medium",
            scope="pod",
            potential_savings_cost=1120.0,
            potential_savings_co2e_grams=620000.0,
            cron_schedule="0 20 * * * scale-down; 0 6 * * 1-5 scale-up",
            carbon_saved_co2e_grams=540000.0,
            cost_saved=970.0,
            created_at=now - timedelta(days=300),
            applied_at=now - timedelta(days=260),
        ),
        _applied_recommendation(
            pod_name=None,
            namespace="design-review",
            type=RecommendationType.IDLE_NAMESPACE,
            description="The design-review namespace is now started only for approved product review sessions.",
            reason="It stayed mostly idle after the product launch, but still reserved memory and PVC capacity.",
            priority="medium",
            scope="namespace",
            potential_savings_cost=740.0,
            potential_savings_co2e_grams=410000.0,
            carbon_saved_co2e_grams=355000.0,
            cost_saved=680.0,
            created_at=now - timedelta(days=260),
            applied_at=now - timedelta(days=240),
        ),
        _applied_recommendation(
            pod_name="mageai-0",
            namespace="mageai",
            type=RecommendationType.CARBON_AWARE_SCHEDULING,
            description="Mage AI demand forecasts were shifted from midday to low-carbon night windows in Osaka.",
            reason=(
                "The batch consumed the same compute but emitted far less when "
                "scheduled between 01:00 and 04:00 local time."
            ),
            priority="high",
            scope="pod",
            potential_savings_cost=140.0,
            potential_savings_co2e_grams=710000.0,
            carbon_saved_co2e_grams=640000.0,
            cost_saved=120.0,
            created_at=now - timedelta(days=240),
            applied_at=now - timedelta(days=210),
        ),
        _applied_recommendation(
            pod_name="eu-optic-buffer-05",
            namespace="cluster",
            type=RecommendationType.OVERPROVISIONED_NODE,
            description=(
                "The extra European buffer node was removed after regional "
                "failover tests confirmed sufficient headroom."
            ),
            reason="Average CPU stayed below 14% and memory below 22% even during catalog refreshes.",
            priority="high",
            scope="node",
            potential_savings_cost=3120.0,
            potential_savings_co2e_grams=1760000.0,
            target_node="eu-optic-buffer-05",
            carbon_saved_co2e_grams=1500000.0,
            cost_saved=2700.0,
            created_at=now - timedelta(days=390),
            applied_at=now - timedelta(days=320),
        ),
        _applied_recommendation(
            pod_name="ap-optic-edge-03",
            namespace="cluster",
            type=RecommendationType.UNDERUTILIZED_NODE,
            description="One APAC node was downsized from m8g.2xlarge to m8g.xlarge after sustained low utilization.",
            reason="The local checkout and retail services rarely exceeded 38% CPU outside campaign weeks.",
            priority="medium",
            scope="node",
            potential_savings_cost=860.0,
            potential_savings_co2e_grams=455000.0,
            target_node="ap-optic-edge-03",
            carbon_saved_co2e_grams=410000.0,
            cost_saved=780.0,
            created_at=now - timedelta(days=200),
            applied_at=now - timedelta(days=175),
        ),
        _active_recommendation(
            pod_name="catalog-search-0",
            namespace="platform",
            type=RecommendationType.RIGHTSIZING_MEMORY,
            description="Catalog search still keeps more heap than needed outside the Black Friday replay tests.",
            reason="P95 memory stayed below 780Mi during the last 90 days while 1280Mi remains reserved.",
            priority="medium",
            scope="pod",
            potential_savings_cost=260.0,
            potential_savings_co2e_grams=145000.0,
            current_memory_request_bytes=1280 * MIB,
            recommended_memory_request_bytes=1024 * MIB,
            created_at=now - timedelta(days=12),
        ),
        _active_recommendation(
            pod_name="dremio-executor-1",
            namespace="dremio",
            type=RecommendationType.CARBON_AWARE_SCHEDULING,
            description=(
                "Weekly data rebalancing still runs too early in the afternoon "
                "instead of the lowest-carbon overnight slot."
            ),
            reason=(
                "The executor consumes mostly the same energy, but its run "
                "window still overlaps with the regional peak intensity period."
            ),
            priority="medium",
            scope="pod",
            potential_savings_co2e_grams=185000.0,
            created_at=now - timedelta(days=7),
        ),
        _active_recommendation(
            pod_name="stock-reservation-0",
            namespace="retail",
            type=RecommendationType.OFF_PEAK_SCALING,
            description=(
                "Nightly reservation reconciliation could scale down between store closure and ERP batch import."
            ),
            reason="The service drops below 6% utilization for almost nine hours every night.",
            priority="low",
            scope="pod",
            potential_savings_cost=120.0,
            potential_savings_co2e_grams=68000.0,
            cron_schedule="0 0 * * * scale-down; 0 6 * * * scale-up",
            created_at=now - timedelta(days=4),
        ),
    ]

    logger.info("Generated %d demo recommendations.", len(recommendations))
    return recommendations
