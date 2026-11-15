# src/greenkube/verify/health_baseline.py
"""Health gates judged against the baseline window (MEA-07).

The health gates of a change measurement used to fail on absolute levels over
the whole post-window: one unrelated restart of any replica in a week, or CPU
throttling that already existed before the change, sent a realized change to
``rollback_review``. The same signals are now frozen for the baseline window in
the snapshot (:func:`freeze_health`) and the decision compares with them
(:func:`compare_with_baseline`):

* ``restarts`` fails only on a significant rise of the restart rate per
  replica-hour: exact conditional binomial test (if the rate is unchanged, the
  post-window share of all restarts is binomial with the post-window share of
  the replica-hours), one-sided ``p <= RESTART_ALPHA``, **and** at least
  ``max(1, VERIFICATION_MAX_RESTART_DELTA)`` more restarts than expected from
  the baseline rate, so the tolerance scales with replica-hours;
* ``cpu_throttling`` fails above the baseline ratio plus
  ``VERIFICATION_MAX_THROTTLE_RATIO`` (absolute);
* ``readiness`` fails below ``min(VERIFICATION_MIN_READINESS, baseline - 0.01)``.

Without a baseline value (snapshots frozen before this check, or a signal not
read) the absolute rule is kept and the gate detail says ``baseline_missing``.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence

from greenkube.models.verification import HealthGateResult, HealthGateStatus

#: One-sided significance of a restart-rate rise.
RESTART_ALPHA = 0.05
#: Readiness drop (absolute) tolerated below the baseline ratio.
READINESS_TOLERANCE = 0.01
#: Gates compared with the baseline window.
COMPARED_GATES = ("restarts", "cpu_throttling", "readiness")
#: Detail suffix of a gate judged on its absolute level because the baseline is unknown.
BASELINE_MISSING = "baseline_missing"
#: Key of the per-container values in ``BaselineSnapshot.health``.
CONTAINERS_KEY = "containers"


def container_key(container: Optional[str]) -> str:
    """Key of a container in the frozen health (``""`` for workload-level gates)."""
    return container or ""


def freeze_health(
    gates_by_container: Mapping[Optional[str], Sequence[HealthGateResult]],
    *,
    replica_hours: Optional[float],
    hours: float,
) -> Dict[str, Any]:
    """Baseline health values to store in the snapshot.

    ``gates_by_container`` are the gates the verifier evaluated over the
    baseline window (only their values are kept), ``replica_hours`` the sum of
    the hourly replica counts of that window and ``hours`` its length.
    """
    containers: Dict[str, Dict[str, Optional[float]]] = {}
    for container, gates in gates_by_container.items():
        values: Dict[str, Optional[float]] = {}
        for gate in gates:
            if gate.gate in (*COMPARED_GATES, "oom_kills"):
                values[gate.gate] = float(gate.value) if gate.value is not None else None
        containers[container_key(container)] = values
    return {
        CONTAINERS_KEY: containers,
        "replica_hours": round(replica_hours, 6) if replica_hours else None,
        "hours": round(hours, 6),
    }


def baseline_mean_replicas(health: Mapping[str, Any]) -> Optional[float]:
    """Mean replica count of the baseline window, when it was frozen."""
    replica_hours = health.get("replica_hours")
    hours = health.get("hours")
    if not replica_hours or not hours:
        return None
    return float(replica_hours) / float(hours)


def _binomial_upper_tail(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    return math.fsum(math.comb(n, i) * p**i * (1.0 - p) ** (n - i) for i in range(k, n + 1))


def _missing(gate: HealthGateResult) -> HealthGateResult:
    detail = f"{gate.detail}; {BASELINE_MISSING}" if gate.detail else BASELINE_MISSING
    return gate.model_copy(update={"detail": detail})


def _restarts(
    gate: HealthGateResult,
    baseline: Optional[float],
    baseline_exposure: Optional[float],
    post_exposure: Optional[float],
    max_restart_delta: float,
) -> HealthGateResult:
    if baseline is None or not baseline_exposure or not post_exposure or gate.value is None:
        return _missing(gate)
    post = int(round(gate.value))
    before = int(round(baseline))
    share = post_exposure / (post_exposure + baseline_exposure)
    expected = before * post_exposure / baseline_exposure
    tolerance = max(1.0, max_restart_delta)
    p_value = _binomial_upper_tail(post, post + before, share)
    failed = p_value <= RESTART_ALPHA and post - expected >= tolerance
    return HealthGateResult(
        gate=gate.gate,
        status=HealthGateStatus.FAILED if failed else HealthGateStatus.PASSED,
        value=post,
        threshold=round(expected + tolerance, 4),
        baseline_value=before,
        detail=(
            f"{post} restart(s) against {expected:.3g} expected from the baseline rate "
            f"({before} over {baseline_exposure:.4g} replica-hours; one-sided p={p_value:.3g})"
        ),
    )


def _throttling(gate: HealthGateResult, baseline: Optional[float], tolerance: float) -> HealthGateResult:
    if baseline is None or gate.value is None:
        return _missing(gate)
    threshold = baseline + tolerance
    failed = gate.value > threshold
    return HealthGateResult(
        gate=gate.gate,
        status=HealthGateStatus.FAILED if failed else HealthGateStatus.PASSED,
        value=gate.value,
        threshold=round(threshold, 6),
        baseline_value=baseline,
        detail=(
            f"CPU throttling ratio {gate.value:.3f} against {baseline:.3f} before the change "
            f"(fails above {threshold:.3f})"
        ),
    )


def _readiness(gate: HealthGateResult, baseline: Optional[float], minimum: float) -> HealthGateResult:
    if baseline is None or gate.value is None:
        return _missing(gate)
    threshold = min(minimum, baseline - READINESS_TOLERANCE)
    failed = gate.value < threshold
    return HealthGateResult(
        gate=gate.gate,
        status=HealthGateStatus.FAILED if failed else HealthGateStatus.PASSED,
        value=gate.value,
        threshold=round(threshold, 6),
        baseline_value=baseline,
        detail=(
            f"readiness ratio {gate.value:.2%} against {baseline:.2%} before the change (fails below {threshold:.2%})"
        ),
    )


def compare_with_baseline(
    gates: Sequence[HealthGateResult],
    container: Optional[str],
    health: Optional[Mapping[str, Any]],
    *,
    post_hours: float,
    post_replica_hours: Optional[float],
    max_restart_delta: float,
    max_throttle_ratio: float,
    min_readiness: float,
) -> List[HealthGateResult]:
    """Re-judge the restart, throttling and readiness gates of one container against its baseline.

    ``health`` is ``BaselineSnapshot.health`` (empty or ``None`` for snapshots
    frozen before MEA-07). ``post_replica_hours`` defaults to the baseline mean
    replica count times ``post_hours``; without baseline replica counts the
    restart rates are compared per hour. Gates that are not available, and every
    other gate, are returned unchanged.
    """
    containers = (health or {}).get(CONTAINERS_KEY) or {}
    values: Mapping[str, Optional[float]] = containers.get(container_key(container)) or {}
    baseline_exposure = (health or {}).get("replica_hours")
    if not baseline_exposure:
        # No replica counts in the baseline: compare per hour (the replica count is assumed unchanged).
        baseline_exposure = (health or {}).get("hours")
        post_replica_hours = post_hours
    elif post_replica_hours is None:
        replicas = baseline_mean_replicas(health or {})
        post_replica_hours = replicas * post_hours if replicas is not None else None
    judged: List[HealthGateResult] = []
    for gate in gates:
        if gate.gate not in COMPARED_GATES or gate.status is HealthGateStatus.NOT_AVAILABLE:
            judged.append(gate)
        elif gate.gate == "restarts":
            judged.append(
                _restarts(gate, values.get("restarts"), baseline_exposure, post_replica_hours, max_restart_delta)
            )
        elif gate.gate == "cpu_throttling":
            judged.append(_throttling(gate, values.get("cpu_throttling"), max_throttle_ratio))
        else:
            judged.append(_readiness(gate, values.get("readiness"), min_readiness))
    return judged
