# src/greenkube/core/optimization/restarts.py
"""Restarts inside an observation window, from the stored lifetime restart counter (COL-20).

``restart_count`` on metric rows is kube-state-metrics'
``kube_pod_container_status_restarts_total``: a counter since the pod was
created, repeated on every row.  Its maximum over a window says nothing about
when the restarts happened: a pod restarted once months ago (node reboot)
would look unstable for ever.  The restarts of the window are the increases of
the counter between consecutive rows of the same pod (and container); a drop
(counter reset) counts as no restart.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _key(row: Any) -> tuple[str, str, str]:
    return (
        str(getattr(row, "namespace", "") or ""),
        str(getattr(row, "pod_name", "") or ""),
        str(getattr(row, "container_name", "") or ""),
    )


def _timestamp(row: Any) -> datetime:
    value = getattr(row, "timestamp", None)
    if not isinstance(value, datetime):
        return _EPOCH
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def restarts_by_pod(rows: Iterable[Any]) -> dict[tuple[str, str, str], int]:
    """Restarts inside the rows' span per ``(namespace, pod, container)``; only series with a known counter."""
    series: dict[tuple[str, str, str], list[tuple[datetime, int]]] = defaultdict(list)
    for row in rows:
        counter = getattr(row, "restart_count", None)
        if isinstance(counter, bool) or not isinstance(counter, (int, float)):
            continue
        series[_key(row)].append((_timestamp(row), int(counter)))
    result: dict[tuple[str, str, str], int] = {}
    for key, points in series.items():
        points.sort(key=lambda point: point[0])
        result[key] = sum(max(later[1] - earlier[1], 0) for earlier, later in zip(points, points[1:]))
    return result


def restarts_in_window(rows: Iterable[Any]) -> Optional[int]:
    """Total restarts inside the rows' span over all pods, or ``None`` when no row has a counter."""
    per_pod = restarts_by_pod(rows)
    return sum(per_pod.values()) if per_pod else None
