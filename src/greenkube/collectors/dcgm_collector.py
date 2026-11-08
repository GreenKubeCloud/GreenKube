# src/greenkube/collectors/dcgm_collector.py
"""GPU power and utilization from the NVIDIA DCGM exporter (roadmap B5).

The DCGM exporter publishes one series per GPU:

* ``DCGM_FI_DEV_POWER_USAGE`` — board power draw (W);
* ``DCGM_FI_DEV_GPU_UTIL`` — utilization (%).

With Kubernetes attribution enabled (the GPU Operator default) a GPU assigned
to a pod carries that pod's ``namespace``/``pod`` labels. When Prometheus
scrapes the exporter without ``honorLabels``, those labels are renamed
``exported_namespace``/``exported_pod`` and ``namespace``/``pod`` name the
exporter itself; the exported labels therefore win, and a GPU that would be
attributed to the exporter pod is treated as unattributed.

DCGM is optional: no series means no GPU power, and a query error never fails
a collection window (GPU power is then missing for that window, with a warning).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

DCGM_POWER_METRIC = "DCGM_FI_DEV_POWER_USAGE"
DCGM_UTIL_METRIC = "DCGM_FI_DEV_GPU_UTIL"

_EXPORTER_HINT = "dcgm-exporter"
#: Labels that identify a GPU on DCGM series.
_GPU_LABELS = ("UUID", "uuid", "gpu", "device", "modelName")


def power_query(step: str) -> str:
    """Average GPU power over each step, evaluated at the end of the step (half-open, A2)."""
    return f"avg_over_time({DCGM_POWER_METRIC}[{step}])"


def utilization_query(step: str) -> str:
    """Average GPU utilization over each step."""
    return f"avg_over_time({DCGM_UTIL_METRIC}[{step}])"


@dataclass(frozen=True)
class GpuSeriesIdentity:
    """Labels of one GPU series that matter for attribution."""

    device: str
    model_name: Optional[str]
    namespace: Optional[str]
    pod: Optional[str]
    #: Candidate node names, most explicit first (``node``, ``kubernetes_node``, ``Hostname``).
    node_hints: Tuple[str, ...]
    #: The exporter pod that scraped the GPU, when Prometheus renamed the workload labels.
    exporter_pod: Optional[str] = None


def series_identity(metric: Mapping[str, str]) -> GpuSeriesIdentity:
    """Extracts the GPU identity and attribution from DCGM series labels."""
    exported_ns = metric.get("exported_namespace")
    exported_pod = metric.get("exported_pod")
    if exported_pod:
        namespace, pod = exported_ns, exported_pod
        exporter_pod = metric.get("pod")
    else:
        namespace = metric.get("namespace") or metric.get("pod_namespace")
        pod = metric.get("pod") or metric.get("pod_name")
        exporter_pod = None
    if pod and _EXPORTER_HINT in pod:
        exporter_pod, namespace, pod = pod, None, None
    device = metric.get("UUID") or metric.get("uuid") or metric.get("gpu") or metric.get("device") or ""
    hints = tuple(
        value
        for value in (
            metric.get("node"),
            metric.get("kubernetes_node"),
            metric.get("nodename"),
            metric.get("Hostname"),
        )
        if value
    )
    return GpuSeriesIdentity(
        device=str(device),
        model_name=metric.get("modelName") or metric.get("model_name"),
        namespace=namespace or None,
        pod=pod or None,
        node_hints=hints,
        exporter_pod=exporter_pod,
    )


@dataclass
class GpuSample:
    """One GPU over one step: identity, average power and utilization."""

    identity: GpuSeriesIdentity
    power_watts: Optional[float] = None
    utilization_pct: Optional[float] = None


SampleIterator = Callable[[dict], Iterator[Tuple[int, float]]]


def samples_by_step(
    power_results: Optional[Iterable[dict]],
    utilization_results: Optional[Iterable[dict]],
    iter_samples: SampleIterator,
) -> Dict[int, List[GpuSample]]:
    """Joins power and utilization series per GPU and per step start."""
    merged: Dict[int, Dict[Tuple, GpuSample]] = {}

    def _add(results: Optional[Iterable[dict]], attribute: str) -> None:
        for series in results or []:
            labels = series.get("metric", {}) or {}
            if not any(labels.get(name) for name in _GPU_LABELS):
                # Not a DCGM GPU series (defensive against shared fakes or relabeling).
                continue
            identity = series_identity(labels)
            key = (identity.device, identity.node_hints, identity.namespace, identity.pod)
            for sample_start, value in iter_samples(series):
                step = merged.setdefault(sample_start, {})
                sample = step.setdefault(key, GpuSample(identity=identity))
                setattr(sample, attribute, value)

    _add(power_results, "power_watts")
    _add(utilization_results, "utilization_pct")
    return {start: list(samples.values()) for start, samples in merged.items()}
