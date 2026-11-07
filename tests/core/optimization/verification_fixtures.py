# tests/core/optimization/verification_fixtures.py
"""Shared fakes for verifier tests: a window-aware metrics repository and provenance-tagged rows."""

from datetime import datetime, timedelta
from typing import Iterable, List, Optional

from greenkube.models.metrics import CombinedMetric, RecommendationRecord
from greenkube.storage.base_repository import ContainerMetricsRepository


class ProvenancedMetric(CombinedMetric):
    """A metric row carrying the A11 provenance and A4 cost-component columns."""

    cost_source: Optional[str] = None
    usage_source: Optional[str] = None
    intensity_source: Optional[str] = None
    power_source: Optional[str] = None
    cost_cpu: Optional[float] = None
    cost_ram: Optional[float] = None


class WindowedMetricsRepo(ContainerMetricsRepository):
    """Serves pod and container rows whose timestamp falls in ``[start, end)``."""

    def __init__(self, pod_rows: Iterable[CombinedMetric] = (), container_rows: Iterable[CombinedMetric] = ()):
        self.pod_rows = list(pod_rows)
        self.container_rows = list(container_rows)
        self.reads: List[tuple[str, datetime, datetime]] = []

    @staticmethod
    def _window(rows, start, end, namespace):
        return [
            row
            for row in rows
            if row.timestamp is not None
            and start <= row.timestamp < end
            and (namespace is None or row.namespace == namespace)
        ]

    async def read_combined_metrics_smart(self, start_time, end_time, namespace=None):
        self.reads.append(("pod", start_time, end_time))
        return self._window(self.pod_rows, start_time, end_time, namespace)

    async def read_container_metrics_smart(self, start_time, end_time, namespace=None):
        self.reads.append(("container", start_time, end_time))
        return self._window(self.container_rows, start_time, end_time, namespace)

    async def write_container_metrics(self, metrics):
        return len(metrics)

    async def read_container_metrics(self, start_time, end_time, namespace=None):
        return self._window(self.container_rows, start_time, end_time, namespace)

    async def read_hourly_container_metrics(self, start_time, end_time, namespace=None):
        return []


def rows(
    start: datetime,
    end: datetime,
    *,
    pods: Iterable[str] = ("api-a",),
    cost: float = 0.01,
    co2: float = 1.0,
    cpu: Optional[int] = 100,
    memory: Optional[int] = None,
    restart: Optional[int] = 0,
    container: Optional[str] = None,
    step: timedelta = timedelta(minutes=5),
    model=CombinedMetric,
    **extra,
) -> List[CombinedMetric]:
    """Five-minute rows for each pod of the ``api`` Deployment over ``[start, end)``."""
    out: List[CombinedMetric] = []
    moment = start
    while moment < end:
        for pod in pods:
            out.append(
                model(
                    pod_name=pod,
                    namespace="prod",
                    owner_kind="Deployment",
                    owner_name="api",
                    container_name=container,
                    total_cost=cost,
                    co2e_grams=co2,
                    cpu_usage_millicores=cpu,
                    memory_usage_bytes=memory,
                    restart_count=restart,
                    timestamp=moment,
                    duration_seconds=int(step.total_seconds()),
                    is_estimated=False,
                    **extra,
                )
            )
        moment += step
    return out


def rec_id(record: RecommendationRecord) -> int:
    """The persisted ID of a record (typed as ``int``)."""
    assert record.id is not None
    return record.id


def baseline_of(record: RecommendationRecord) -> dict:
    """The frozen baseline of a record as a plain dict."""
    assert record.baseline is not None
    return record.baseline.model_dump()
