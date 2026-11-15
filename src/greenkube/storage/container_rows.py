"""Rules shared by the repositories when a collected window rewrites container rows (COL-17)."""

from __future__ import annotations

from typing import List

from greenkube.models.metrics import CombinedMetric

#: Estimation reason of container rows of a pod where some containers had no sample at the step:
#: the sampled containers carry the whole pod row.
CONTAINER_SAMPLE_INCOMPLETE = "container_sample_incomplete"


def _key(row: CombinedMetric) -> tuple:
    return (row.namespace, row.pod_name, row.timestamp)


def complete_container_rows(container_rows: List[CombinedMetric]) -> List[CombinedMetric]:
    """Container rows that describe every container of their pod (not flagged incomplete)."""
    return [row for row in container_rows if CONTAINER_SAMPLE_INCOMPLETE not in (row.estimation_reasons or [])]


def container_rows_of(pod_rows: List[CombinedMetric], container_rows: List[CombinedMetric]) -> List[CombinedMetric]:
    """Drop incomplete container rows whose pod row is not written.

    An incomplete batch (one container without a sample) never replaces the
    container rows already stored for that pod and step: the repository keeps
    the stored pod row, so the incomplete container rows of the batch are
    dropped with it and pod and container rows still agree.
    """
    written = {_key(row) for row in pod_rows}
    return [
        row
        for row in container_rows
        if CONTAINER_SAMPLE_INCOMPLETE not in (row.estimation_reasons or []) or _key(row) in written
    ]
