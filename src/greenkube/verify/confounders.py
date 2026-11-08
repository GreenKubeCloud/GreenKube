# src/greenkube/verify/confounders.py
"""Confounder detection (C4).

A confounder is anything other than the measured change that may explain part
of the observed delta. Each finding has a severity:

* ``severe`` — the measurement cannot be attributed to the change; the outcome
  becomes ``contaminated`` (another change on the same workload between the
  merge and the end of the post-window, a pod-template change that is not
  this change, the workload recreated);
* ``moderate`` — lowers confidence (a change inside the baseline window, an
  HPA edit, replica counts beyond what the load explains, a price-card change
  seen on a resource whose requests the change did not touch, a node
  instance-type change);
* ``mild`` — lowers confidence slightly.

Pool-level confounders (changes to other workloads sharing the node pool) are
not detected yet: they belong to the pool-level realization allocator (C10).
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Sequence

from greenkube.utils.time import ensure_utc
from greenkube.verify.counterfactual import HourOfWeekCounterfactual, align
from greenkube.verify.load_driver import ResolvedLoad
from greenkube.verify.model import (
    ChangeEvent,
    Confounder,
    HourBucket,
    WindowPlan,
    WorkloadSpecObservation,
)
from greenkube.verify.series import quantity_series

#: Mean replicas beyond this relative deviation from the load-scaled counterfactual are flagged.
REPLICA_SHIFT_TOLERANCE = 0.25
#: Unit price changes beyond this relative deviation are flagged.
PRICE_CHANGE_TOLERANCE = 0.05
#: Instance-type mixes further apart than this (total-variation distance) are flagged.
INSTANCE_MIX_TOLERANCE = 0.25

_PENALTY = {"mild": 0.05, "moderate": 0.15}
#: Confidence penalty when the counterfactual has no load driver or only the weak CPU-usage driver.
WEAK_LOAD_PENALTY = 0.10


def _mean(values: Iterable[float]) -> Optional[float]:
    items = [value for value in values if value is not None and math.isfinite(value)]
    return math.fsum(items) / len(items) if items else None


def _unit_price(buckets: Sequence[HourBucket], cost_attr: str, quantity_attr: str) -> Optional[float]:
    cost = 0.0
    quantity = 0.0
    for bucket in buckets:
        value = getattr(bucket, cost_attr)
        amount = getattr(bucket, quantity_attr)
        if value is None or amount is None or amount <= 0 or bucket.cost_coverage < 0.5:
            continue
        cost += value
        quantity += amount
    return cost / quantity if quantity > 0 else None


def _per_replica(buckets: Sequence[HourBucket], quantity_attr: str) -> Optional[float]:
    """Mean request per replica (a change of replica count is not a change of request)."""
    return _mean(
        getattr(bucket, quantity_attr) / bucket.replicas
        for bucket in buckets
        if bucket.coverage >= 0.5 and bucket.replicas and getattr(bucket, quantity_attr) is not None
    )


def _instance_mix(buckets: Sequence[HourBucket]) -> Dict[str, float]:
    totals: Dict[str, float] = {}
    for bucket in buckets:
        for key, value in bucket.instance_types.items():
            totals[key] = totals.get(key, 0.0) + value
    total = math.fsum(totals.values())
    return {key: value / total for key, value in totals.items()} if total > 0 else {}


class ConfounderDetector:
    """Detects confounders of one change measurement."""

    def detect(
        self,
        change: ChangeEvent,
        plan: WindowPlan,
        *,
        other_changes: Sequence[ChangeEvent] = (),
        observations: Sequence[WorkloadSpecObservation] = (),
        pre: Sequence[HourBucket] = (),
        post: Sequence[HourBucket] = (),
        load: Optional[ResolvedLoad] = None,
    ) -> List[Confounder]:
        findings: List[Confounder] = []
        findings.extend(self._other_changes(change, plan, other_changes))
        findings.extend(self._spec_changes(change, plan, observations))
        findings.extend(self._replica_shift(pre, post, load))
        findings.extend(self._price_change(pre, post))
        findings.extend(self._instance_types(pre, post))
        return findings

    @staticmethod
    def _other_changes(change: ChangeEvent, plan: WindowPlan, others: Sequence[ChangeEvent]) -> List[Confounder]:
        findings: List[Confounder] = []
        decision = ensure_utc(change.baseline_window_end)
        post_end = ensure_utc(plan.post.end)
        pre_start = ensure_utc(plan.pre.start)
        for other in others:
            if other.change_id == change.change_id or other.target.key != change.target.key:
                continue
            landed = ensure_utc(other.rollout_completed_at)
            evidence = {"change_id": other.change_id, "rollout_completed_at": landed.isoformat()}
            if decision < landed < post_end:
                findings.append(
                    Confounder(
                        kind="overlapping_change",
                        severity="severe",
                        detail=f"change {other.change_id} landed on the same workload during the measurement",
                        evidence=evidence,
                    )
                )
            elif pre_start <= landed <= decision:
                findings.append(
                    Confounder(
                        kind="change_in_baseline",
                        severity="moderate",
                        detail=f"change {other.change_id} landed on the same workload inside the baseline window",
                        evidence=evidence,
                    )
                )
        return findings

    @staticmethod
    def _spec_changes(
        change: ChangeEvent, plan: WindowPlan, observations: Sequence[WorkloadSpecObservation]
    ) -> List[Confounder]:
        findings: List[Confounder] = []
        completed = ensure_utc(change.rollout_completed_at)
        post_end = ensure_utc(plan.post.end)
        pre_start = ensure_utc(plan.pre.start)
        ordered = sorted(observations, key=lambda item: ensure_utc(item.observed_at))
        flagged_spec = flagged_uid = False
        previous_hpa: Optional[str] = None
        seen_hpa = False
        for observation in ordered:
            moment = ensure_utc(observation.observed_at)
            if moment < pre_start or moment >= post_end:
                continue
            if moment > completed:
                if (
                    not flagged_uid
                    and change.workload_uid
                    and observation.workload_uid
                    and observation.workload_uid != change.workload_uid
                ):
                    flagged_uid = True
                    findings.append(
                        Confounder(
                            kind="workload_recreated",
                            severity="severe",
                            detail="the workload was deleted and recreated during the measurement",
                            evidence={"workload_uid": observation.workload_uid, "observed_at": moment.isoformat()},
                        )
                    )
                if (
                    not flagged_spec
                    and change.spec_digest_after
                    and observation.spec_digest
                    and observation.spec_digest != change.spec_digest_after
                ):
                    flagged_spec = True
                    findings.append(
                        Confounder(
                            kind="spec_change",
                            severity="severe",
                            detail="the pod template changed again (image or spec) during the measurement",
                            evidence={
                                "spec_digest": observation.spec_digest,
                                "generation": observation.generation,
                                "observed_at": moment.isoformat(),
                            },
                        )
                    )
            if seen_hpa and observation.hpa_digest != previous_hpa:
                findings.append(
                    Confounder(
                        kind="hpa_change",
                        severity="moderate",
                        detail="the HorizontalPodAutoscaler of the workload changed",
                        evidence={"observed_at": moment.isoformat()},
                    )
                )
            previous_hpa = observation.hpa_digest
            seen_hpa = True
        return findings

    @staticmethod
    def _replica_shift(
        pre: Sequence[HourBucket], post: Sequence[HourBucket], load: Optional[ResolvedLoad]
    ) -> List[Confounder]:
        pre_replicas = quantity_series(pre, "replicas")
        post_replicas = quantity_series(post, "replicas")
        if not pre_replicas or not post_replicas:
            return []
        model = HourOfWeekCounterfactual.fit(pre_replicas, load.pre if load is not None else None)
        aligned = align(model, post_replicas, load.post if load is not None else None)
        expected = _mean(item.counterfactual for item in aligned)
        actual = _mean(item.actual for item in aligned)
        if expected is None or actual is None or expected <= 0:
            return []
        deviation = actual / expected - 1.0
        if abs(deviation) <= REPLICA_SHIFT_TOLERANCE:
            return []
        return [
            Confounder(
                kind="replica_shift",
                severity="moderate",
                detail=f"mean replicas {actual:.2f} vs {expected:.2f} expected from the load ({deviation:+.0%})",
                evidence={"expected": round(expected, 4), "actual": round(actual, 4)},
            )
        ]

    @staticmethod
    def _price_change(pre: Sequence[HourBucket], post: Sequence[HourBucket]) -> List[Confounder]:
        """Unit price of a resource whose requests the change did not touch (it acts as a control)."""
        findings: List[Confounder] = []
        for resource, cost_attr, quantity_attr in (
            ("cpu", "cost_cpu", "core_hours"),
            ("memory", "cost_ram", "gib_hours"),
        ):
            requested_before = _per_replica(pre, quantity_attr)
            requested_after = _per_replica(post, quantity_attr)
            if (
                requested_before is None
                or requested_after is None
                or requested_before <= 0
                or abs(requested_after / requested_before - 1.0) > PRICE_CHANGE_TOLERANCE
            ):
                # The change moved this resource's per-replica requests: its unit price is no control.
                continue
            before = _unit_price(pre, cost_attr, quantity_attr)
            after = _unit_price(post, cost_attr, quantity_attr)
            if before is None or after is None or before <= 0:
                continue
            deviation = after / before - 1.0
            if abs(deviation) > PRICE_CHANGE_TOLERANCE:
                findings.append(
                    Confounder(
                        kind="price_card_change",
                        severity="moderate",
                        detail=f"{resource} unit price changed {deviation:+.0%} between the windows",
                        evidence={"resource": resource, "before": before, "after": after},
                    )
                )
        return findings

    @staticmethod
    def _instance_types(pre: Sequence[HourBucket], post: Sequence[HourBucket]) -> List[Confounder]:
        before = _instance_mix(pre)
        after = _instance_mix(post)
        if not before or not after:
            return []
        distance = 0.5 * math.fsum(abs(before.get(key, 0.0) - after.get(key, 0.0)) for key in before.keys() | after)
        if distance <= INSTANCE_MIX_TOLERANCE:
            return []
        return [
            Confounder(
                kind="instance_type_change",
                severity="moderate",
                detail=f"the node instance types running the workload changed ({distance:.0%} of pod-hours)",
                evidence={"before": before, "after": after},
            )
        ]


def is_contaminated(confounders: Sequence[Confounder]) -> bool:
    return any(item.severity == "severe" for item in confounders)


def confidence_score(confounders: Sequence[Confounder], coverage: float, load: Optional[ResolvedLoad]) -> float:
    """Confidence in a measurement: coverage, minus penalties for confounders and a weak load driver."""
    if is_contaminated(confounders):
        return 0.0
    score = max(0.0, min(coverage, 1.0))
    score -= math.fsum(_PENALTY.get(item.severity, 0.0) for item in confounders)
    if load is None or load.info.weak:
        score -= WEAK_LOAD_PENALTY
    return round(max(0.0, min(score, 1.0)), 4)
