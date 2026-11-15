# tests/verify/fixtures.py
"""Shared builders for change-measurement tests (C1-C5).

Times are always derived from ``datetime.now(timezone.utc)`` (never fixed
calendar dates), so the windows stay in the past whatever day the tests run.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, cast

from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.applied_detector import WorkloadSnapshot
from greenkube.core.optimization.lifecycle import RecommendationLifecycle
from greenkube.core.optimization.verifier import RecommendationVerifier
from greenkube.models.metrics import CombinedMetric, RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.storage.change_repository import ChangeRepository
from greenkube.storage.measurement_repository import SavingsMeasurementRepository
from greenkube.storage.postgres.recommendation_repository import PostgresRecommendationRepository
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository
from greenkube.verify.model import ChangeEvent, TimeWindow, WorkloadRef
from greenkube.verify.service import ChangeMeasurementService
from tests.core.optimization.verification_fixtures import WindowedMetricsRepo  # pyrefly: ignore[missing-import]


def hour_now() -> datetime:
    """The current UTC hour (so hour buckets line up with the generated rows)."""
    return datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


@dataclass
class Settings:
    """A minimal configuration object (the services read it with ``getattr``)."""

    PROMETHEUS_QUERY_RANGE_STEP: str = "5m"
    PROMETHEUS_URL: str = ""
    VERIFICATION_WINDOW_HOURS: float = 72
    VERIFICATION_MIN_SAMPLES: int = 36
    VERIFICATION_MIN_SAVINGS_RATIO: float = 0.5
    VERIFICATION_USAGE_HEADROOM: float = 1.1
    VERIFICATION_MAX_RESTART_DELTA: int = 0
    VERIFICATION_MIN_READINESS: float = 0.99
    VERIFICATION_MAX_THROTTLE_RATIO: float = 0.05
    CHANGE_MEASUREMENT_ENABLED: bool = True
    CHANGE_MEASUREMENT_PRE_WINDOW_DAYS: float = 7
    CHANGE_MEASUREMENT_POST_WINDOW_DAYS: float = 7
    CHANGE_MEASUREMENT_WARMUP_HOURS: float = 1
    CHANGE_MEASUREMENT_MIN_COVERAGE: float = 0.8
    CHANGE_MEASUREMENT_BOOTSTRAP_SAMPLES: int = 300
    CHANGE_MEASUREMENT_EPSILON_RATIO: float = 0.02
    CHANGE_MEASUREMENT_ACCELERATED: bool = False
    RECOMMENDATION_APPLY_TOLERANCE: float = 0.25
    CURRENCY: str = "USD"
    extra: dict = field(default_factory=dict)


def change_event(
    completed_at: datetime,
    *,
    change_id: str = "change-1",
    decision: Optional[datetime] = None,
    recommendation_ids: Iterable[int] = (1,),
    name: str = "api",
    spec_digest_after: Optional[str] = "digest-after",
    load_metric: Optional[str] = None,
    generation_after: Optional[int] = 2,
) -> ChangeEvent:
    return ChangeEvent(
        change_id=change_id,
        recommendation_ids=list(recommendation_ids),
        target=WorkloadRef(namespace="prod", kind="Deployment", name=name),
        workload_uid="uid-api",
        generation_before=1,
        generation_after=generation_after,
        spec_digest_before="digest-before",
        spec_digest_after=spec_digest_after,
        rollout_started_at=completed_at - timedelta(minutes=5),
        rollout_completed_at=completed_at,
        baseline_window_end=decision or completed_at - timedelta(minutes=30),
        load_metric=load_metric,
        created_at=completed_at,
    )


def workload_rows(
    window: TimeWindow,
    *,
    pods: Iterable[str] = ("api-a", "api-b"),
    cost: Callable[[datetime], float] | float | None = None,
    cpu_price: float = 0.08,
    ram_price: float = 0.01,
    cpu_request: int = 1000,
    memory_request: int = 2 * 1024**3,
    cpu_usage: int = 200,
    co2e: float = 12.0,
    embodied: float = 0.0,
    step: timedelta = timedelta(hours=1),
    components: bool = True,
    container: Optional[str] = None,
    instance_type: Optional[str] = None,
    noise: float = 0.0,
    seed: int = 7,
    is_estimated: bool = False,
) -> List[CombinedMetric]:
    """Rows of the ``api`` Deployment: one per pod per ``step`` over ``window``.

    ``cost`` is the cost of one pod over one ``step`` (a constant or a
    function of the row time), split 70/30 between CPU and RAM with
    ``components``. Without ``cost`` the allocation is request-based like
    OpenCost: requested cores x ``cpu_price`` plus requested GiB x
    ``ram_price`` per hour, CPU charged on ``max(request, usage)``.
    """
    rng = random.Random(seed)
    rows: List[CombinedMetric] = []
    moment = window.start
    while moment < window.end:
        hours = step.total_seconds() / 3600
        for pod in pods:
            if cost is None:
                # OpenCost charges max(request, usage) (MEA-02).
                cpu_cost = max(cpu_request, cpu_usage) / 1000 * cpu_price * hours
                ram_cost = memory_request / 1024**3 * ram_price * hours
            else:
                value = cost(moment) if callable(cost) else cost
                cpu_cost, ram_cost = value * 0.7, value * 0.3
            if noise:
                factor = 1.0 + rng.uniform(-noise, noise)
                cpu_cost, ram_cost = cpu_cost * factor, ram_cost * factor
            value = cpu_cost + ram_cost
            rows.append(
                CombinedMetric(
                    pod_name=pod,
                    namespace="prod",
                    owner_kind="Deployment",
                    owner_name="api",
                    container_name=container,
                    total_cost=value,
                    cost_cpu=cpu_cost if components else None,
                    cost_ram=ram_cost if components else None,
                    co2e_grams=co2e * step.total_seconds() / 3600,
                    embodied_co2e_grams=embodied * step.total_seconds() / 3600,
                    # Energy consistent with the CO2e at 300 gCO2e/kWh (PUE 1): CO2e = kWh x intensity (MEA-03).
                    joules=co2e * step.total_seconds() / 3600 / 300.0 * 3_600_000.0,
                    grid_intensity=300.0,
                    cpu_request=cpu_request,
                    memory_request=memory_request,
                    cpu_usage_millicores=cpu_usage,
                    memory_usage_bytes=512 * 1024**2,
                    restart_count=0,
                    timestamp=moment,
                    duration_seconds=int(step.total_seconds()),
                    sample_count=max(int(step.total_seconds() // 300), 1),
                    is_estimated=is_estimated,
                    node_instance_type=instance_type,
                )
            )
        moment += step
    return rows


def recommendation_repo(manager):
    if manager.db_type == "postgres":
        return PostgresRecommendationRepository(manager)
    return SQLiteRecommendationRepository(manager)


class Scenario:
    """Two recommendations (CPU and memory of container ``api``) applied by one change."""

    def __init__(self, manager: DatabaseManager, settings: Optional[Settings] = None):
        self.manager = manager
        self.settings = settings or Settings()
        self.reco_repo = recommendation_repo(manager)
        self.changes = ChangeRepository(manager)
        self.measurements = SavingsMeasurementRepository(manager)
        self.metrics = WindowedMetricsRepo()
        self.lifecycle = RecommendationLifecycle(self.reco_repo)
        self.verifier = RecommendationVerifier(
            self.lifecycle, self.metrics, config=cast(Any, self.settings), measurement_repo=self.measurements
        )
        self.service = ChangeMeasurementService(
            self.lifecycle, self.metrics, self.changes, verifier=self.verifier, config=self.settings
        )
        self.completed = hour_now() - timedelta(days=10)
        self.decision = self.completed - timedelta(hours=2)

    def add_rows(self, window: TimeWindow, **values) -> None:
        self.metrics.pod_rows.extend(workload_rows(window, **values))
        self.metrics.container_rows.extend(workload_rows(window, container="api", **values))

    async def apply(
        self,
        change_id: str = "change-a",
        completed: Optional[datetime] = None,
        generation: int = 2,
        fields: Optional[Dict[RecommendationType, Dict[str, Any]]] = None,
        resources_digest: Optional[str] = None,
        image_digest: Optional[str] = None,
        with_snapshot: bool = True,
    ):
        """``fields`` adds or overrides fields of the CPU or memory recommendation.

        ``resources_digest``/``image_digest`` are the rolled-out template's digests (``None``: a legacy change).
        """
        completed = completed or self.completed
        fields = fields or {}
        common = dict(
            pod_name="api",
            namespace="prod",
            scope="workload",
            owner_kind="Deployment",
            owner_name="api",
            container_name="api",
            status=RecommendationStatus.ACTIVE,
            description="oversized",
            source="greenkube",
        )
        await self.reco_repo.save_recommendations(
            [
                RecommendationRecord(
                    type=RecommendationType.RIGHTSIZING_CPU,
                    current_cpu_request_millicores=1000,
                    recommended_cpu_request_millicores=300,
                    potential_savings_cost=900.0,
                    **common,
                ).model_copy(update=fields.get(RecommendationType.RIGHTSIZING_CPU, {})),
                RecommendationRecord(
                    type=RecommendationType.RIGHTSIZING_MEMORY,
                    current_memory_request_bytes=2 * 1024**3,
                    recommended_memory_request_bytes=1024**3,
                    potential_savings_cost=150.0,
                    **common,
                ).model_copy(update=fields.get(RecommendationType.RIGHTSIZING_MEMORY, {})),
            ]
        )
        active = await self.reco_repo.get_recommendations_by_statuses(["active"])
        applied: List[RecommendationRecord] = []
        for record in sorted(active, key=lambda item: item.id or 0):
            assert record.id is not None
            cpu = 300 if record.type is RecommendationType.RIGHTSIZING_CPU else None
            memory = 1024**3 if record.type is RecommendationType.RIGHTSIZING_MEMORY else None
            applied.append(
                await self.lifecycle.apply(
                    record.id,
                    actual_cpu=cpu,
                    actual_memory=memory,
                    application_method="detected",
                    actor="detector",
                    applied_at=completed,
                    baseline_window_end=self.decision if completed == self.completed else completed,
                    rollout={"completed_at": completed.isoformat(), "completion_source": "progressing_condition"},
                    change_id=change_id,
                )
            )
        snapshot = WorkloadSnapshot(
            namespace="prod",
            kind="Deployment",
            name="api",
            uid="uid-api",
            generation=generation,
            observed_generation=generation,
            spec_digest=f"digest-{generation}",
            resources_digest=resources_digest,
            image_digest=image_digest,
        )
        change = await self.service.record_change(
            change_id, applied, snapshot=snapshot if with_snapshot else None, now=completed
        )
        assert change is not None
        return change, applied

    async def records(self) -> List[RecommendationRecord]:
        records = await self.reco_repo.get_recommendations_by_statuses(
            [status.value for status in RecommendationStatus]
        )
        return sorted(records, key=lambda item: item.id or 0)


def record_status(record: RecommendationRecord) -> str:
    return getattr(record.status, "value", str(record.status))
