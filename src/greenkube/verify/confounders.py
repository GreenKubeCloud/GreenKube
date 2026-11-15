# src/greenkube/verify/confounders.py
"""Confounder detection (C4).

A confounder is anything other than the measured change that may explain part
of the observed delta. Each finding has a severity:

* ``severe`` — the measurement cannot be attributed to the change; the outcome
  becomes ``contaminated`` (another change on the same workload between the
  merge and the end of the post-window, a pod-template change that is not
  this change, the workload recreated, an HPA edit during the measurement,
  the node pool's hardware replaced or its capacity reshaped, a unit price or
  instance-type shift large enough to explain a typical rightsizing effect on
  its own);
* ``moderate`` — lowers confidence (a change inside the baseline window, an
  HPA edit in the baseline, replica counts beyond what the load explains, a
  price-card change seen on a resource whose requests the change did not
  touch, a node instance-type change, the node pool's capacity scaled by the
  cluster autoscaler, a change to another workload running in the same pool);
* ``mild`` — lowers confidence slightly (nodes added or removed in the pool
  without a capacity shift).

Pool-level checks (C4) read the workload's node pools from the node
snapshots (:class:`PoolContext`, sampled every few hours over both windows):
they need nodes labeled with a pool. The pool's price card is a control too:
the CPU and memory unit prices of every pod on the pool's nodes (all
workloads, trusted cost only) before and after the change. A unit price is a
property of the nodes, not of the requests, so it moves only when prices or
hardware change, and it is checked even when the change resized both
resources (the workload's own untouched resource is then no control). A shift
beyond :data:`PRICE_CHANGE_TOLERANCE` lowers confidence, beyond
:data:`PRICE_CHANGE_SEVERE` it contaminates the measurement; it replaces the
workload-level price-card finding of the same resource. Allocated cost is requests x unit price,
so other workloads' changes and autoscaling only move this workload's idle
share of CO2e (moderate), whereas new hardware in the pool changes its unit
prices and power profile (severe).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

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
#: Unit price changes beyond this share can explain a typical rightsizing effect on their own (severe).
PRICE_CHANGE_SEVERE = 0.20
#: The energy-weighted carbon factor (intensity x PUE) moved by more than this between the windows (MEA-03).
INTENSITY_SHIFT_TOLERANCE = 0.10
#: Share of the baseline energy valued at the fallback carbon factor above which it is reported (mild).
CARBON_FACTOR_FALLBACK_TOLERANCE = 0.05
#: The workload's instance-type mix moved by more than this share of its pod-hours (severe).
INSTANCE_MIX_SEVERE = 0.5
#: The node pool's capacity-weighted instance-type mix moved by more than this (severe: new hardware).
POOL_INSTANCE_MIX_TOLERANCE = 0.25
#: The node pool's mean CPU capacity moved by more than this between the windows (autoscaling).
POOL_CAPACITY_TOLERANCE = 0.25
#: ... and by more than this, the pool was reshaped (severe).
POOL_CAPACITY_SEVERE = 0.5

_PENALTY = {"mild": 0.05, "moderate": 0.15}
#: Confounders reported without a confidence penalty: the CO2e saving is valued at the post-window
#: carbon factors hour by hour, so an intensity shift biases neither it nor the cost verdict (MEA-03),
#: and a carbon-factor fallback only touches the CO2e valuation, never the cost verdict.
INFORMATIVE_KINDS = frozenset({"intensity_shift", "co2e_factor_fallback"})
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


@dataclass(frozen=True)
class PoolNode:
    """One active node of the workload's node pools at a sample time."""

    name: str
    pool: str
    instance_type: Optional[str]
    cpu_cores: float


@dataclass(frozen=True)
class PoolSample:
    """The active nodes of the workload's node pools at ``at``."""

    at: datetime
    nodes: Tuple[PoolNode, ...]


@dataclass(frozen=True)
class PoolContext:
    """The workload's node pools over both windows, and the changes that landed in them (C4).

    ``other_changes`` are changes to other workloads whose pods ran on the
    pools' nodes and that landed between the decision and the end of the
    post-window.
    """

    pools: Tuple[str, ...]
    pre: Tuple[PoolSample, ...]
    post: Tuple[PoolSample, ...]
    other_changes: Tuple[ChangeEvent, ...] = ()
    #: More changes landed during the measurement than were read: the later ones were not checked.
    truncated: bool = False
    #: Pool-wide unit prices (``cpu`` per core-hour, ``memory`` per GiB-hour) in each window (price-card control).
    prices_pre: Mapping[str, float] = field(default_factory=dict)
    prices_post: Mapping[str, float] = field(default_factory=dict)
    #: ``pool`` (labelled node pools) or ``workload_nodes``: the nodes carry no pool label, so only the
    #: price control runs, over the pods of the nodes the workload ran on (MEA-02).
    scope: str = "pool"
    #: The nodes of a ``workload_nodes`` context.
    nodes: Tuple[str, ...] = ()


_GIB = 1024.0**3


def pool_unit_prices(
    rows: Iterable[Any],
    nodes: Set[str],
    step_seconds: int,
    *,
    exclude: Optional[Tuple[str, str, str]] = None,
) -> Dict[str, float]:
    """CPU and memory unit prices of the pods that ran on ``nodes`` (trusted cost with components only).

    ``exclude`` is the measured workload's target key: its own pods are left
    out, because an allocator that charges ``max(request, usage)`` moves the
    cost-per-request ratio of a workload resized below its usage although no
    price changed, which would read as a pool price shift in a small pool.
    """
    from greenkube.core.optimization.targeting import target_key
    from greenkube.core.optimization.verifier import _coverage_seconds, cost_is_trusted

    totals = {"cpu": [0.0, 0.0], "memory": [0.0, 0.0]}
    for row in rows:
        if getattr(row, "node", None) not in nodes or not cost_is_trusted(row):
            continue
        if exclude is not None and target_key(row) == exclude:
            continue
        hours = _coverage_seconds(row, step_seconds) / 3600.0
        for resource, cost_attr, amount in (
            ("cpu", "cost_cpu", float(getattr(row, "cpu_request", 0) or 0) / 1000.0),
            ("memory", "cost_ram", float(getattr(row, "memory_request", 0) or 0) / _GIB),
        ):
            cost = getattr(row, cost_attr, None)
            if cost is None or amount <= 0 or not math.isfinite(float(cost)):
                continue
            totals[resource][0] += float(cost)
            totals[resource][1] += amount * hours
    return {resource: cost / quantity for resource, (cost, quantity) in totals.items() if quantity > 0 and cost > 0}


def _pool_mix(samples: Sequence[PoolSample]) -> Dict[str, float]:
    """Capacity-weighted instance-type mix of the pool over the samples."""
    totals: Dict[str, float] = {}
    for sample in samples:
        for node in sample.nodes:
            key = node.instance_type or "unknown"
            totals[key] = totals.get(key, 0.0) + max(node.cpu_cores, 0.0)
    total = math.fsum(totals.values())
    return {key: value / total for key, value in totals.items()} if total > 0 else {}


def _pool_capacity(samples: Sequence[PoolSample]) -> Optional[float]:
    return _mean(math.fsum(max(node.cpu_cores, 0.0) for node in sample.nodes) for sample in samples)


def _distance(before: Dict[str, float], after: Dict[str, float]) -> float:
    """Total-variation distance between two mixes."""
    return 0.5 * math.fsum(abs(before.get(key, 0.0) - after.get(key, 0.0)) for key in before.keys() | after)


def _churn(samples: Sequence[PoolSample]) -> Tuple[Set[str], Set[str]]:
    """Nodes that appeared or disappeared between consecutive samples."""
    added: Set[str] = set()
    removed: Set[str] = set()
    ordered = sorted(samples, key=lambda sample: sample.at)
    for previous, current in zip(ordered, ordered[1:]):
        before = {node.name for node in previous.nodes}
        after = {node.name for node in current.nodes}
        added |= after - before
        removed |= before - after
    return added, removed


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
        pool: Optional[PoolContext] = None,
        price_card: Optional[Mapping[str, Any]] = None,
    ) -> List[Confounder]:
        """Every confounder of ``change``; ``price_card`` is the frozen baseline snapshot's price card."""
        findings: List[Confounder] = []
        findings.extend(self._other_changes(change, plan, other_changes))
        findings.extend(self._spec_changes(change, plan, observations))
        findings.extend(self._replica_shift(pre, post, load))
        findings.extend(self._price_change(pre, post))
        findings.extend(self._constant_prices(pre, post, price_card))
        findings.extend(self._intensity_shift(pre, post))
        findings.extend(self._carbon_factor_fallback(pre, post))
        findings.extend(self._instance_types(pre, post))
        if pool is not None:
            findings.extend(self._pool(change, pool))
            pool_priced = {item.evidence.get("resource") for item in findings if item.kind == "pool_price_card_change"}
            if pool_priced:
                # The pool-wide control covers the same price card: one finding per resource.
                findings = [
                    item
                    for item in findings
                    if not (item.kind == "price_card_change" and item.evidence.get("resource") in pool_priced)
                ]
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
                during = moment > completed
                findings.append(
                    Confounder(
                        kind="hpa_change",
                        # Replicas follow the HPA: an edit during the measurement moves the
                        # allocated cost by itself, one in the baseline only shifts its level.
                        severity="severe" if during else "moderate",
                        detail="the HorizontalPodAutoscaler of the workload changed "
                        + ("during the measurement" if during else "in the baseline window"),
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
        """Unit price of each resource on the workload's own rows, before and after (MEA-02).

        With the charged quantities (``max(request, usage)`` per sample) in both
        windows, the price per charged unit is a price even for a resource the
        change resized, so every resource is checked. Windows without them
        (snapshots frozen earlier) use the price per requested unit, which is a
        price too unless the allocator charged usage above the request: CPU is
        then checked only when its mean usage stays at or below the request in
        both windows (or the change did not resize it). Memory usage is not in
        those buckets, so a resized memory request is checked as it is.
        """
        from greenkube.verify.series import has_charged, unit_prices

        if has_charged(pre) and has_charged(post):
            findings: List[Confounder] = []
            before_prices = unit_prices(pre, charged=True)
            after_prices = unit_prices(post, charged=True)
            for resource in ("cpu", "memory"):
                finding = _price_finding(resource, before_prices.get(resource), after_prices.get(resource), "charged")
                if finding is not None:
                    findings.append(finding)
            return findings
        findings = []
        for resource, cost_attr, quantity_attr in (
            ("cpu", "cost_cpu", "core_hours"),
            ("memory", "cost_ram", "gib_hours"),
        ):
            requested_before = _per_replica(pre, quantity_attr)
            requested_after = _per_replica(post, quantity_attr)
            resized = (
                requested_before is None
                or requested_after is None
                or requested_before <= 0
                or abs(requested_after / requested_before - 1.0) > PRICE_CHANGE_TOLERANCE
            )
            if resized and resource == "cpu" and not (_usage_within_request(pre) and _usage_within_request(post)):
                # Usage above (or unknown against) a resized request: the cost-per-request ratio is no price.
                continue
            finding = _price_finding(
                resource,
                _unit_price(pre, cost_attr, quantity_attr),
                _unit_price(post, cost_attr, quantity_attr),
                "requested",
            )
            if finding is not None:
                findings.append(finding)
        return findings

    @staticmethod
    def _constant_prices(
        pre: Sequence[HourBucket], post: Sequence[HourBucket], price_card: Optional[Mapping[str, Any]]
    ) -> List[Confounder]:
        """``constant_prices_unavailable`` (moderate) when the cost cannot be revalued at baseline prices.

        Without CPU/RAM cost components and without a priced card the strategy
        measures the observed cost (``constant_price_basis = observed``), so a
        price move between the windows is booked as saving and no
        ``price_card_change`` can see it (review wp07-2, MEA-02).
        """
        from greenkube.verify.strategy import CONSTANT_PRICE_OBSERVED, constant_price_windows

        if not quantity_series(pre, "allocated_cost") or not quantity_series(post, "allocated_cost"):
            return []
        if constant_price_windows(pre, post, price_card).basis != CONSTANT_PRICE_OBSERVED:
            return []
        return [
            Confounder(
                kind="constant_prices_unavailable",
                severity="moderate",
                detail="no CPU/RAM cost components or baseline unit prices: the cost saving is measured at "
                "observed prices, so a price move between the windows cannot be told apart from the change",
                evidence={"constant_price_basis": CONSTANT_PRICE_OBSERVED},
            )
        ]

    @staticmethod
    def _carbon_factor_fallback(pre: Sequence[HourBucket], post: Sequence[HourBucket]) -> List[Confounder]:
        """``co2e_factor_fallback`` (mild) when over 5% of the baseline energy has no post factor at its hour of week.

        Such hours (the workload scaled to zero, post hours missing) are valued
        at the time-averaged post factor, not at the intensity of their hour.
        The finding costs no confidence (:data:`INFORMATIVE_KINDS`): it touches
        the CO2e valuation only, not the cost verdict.
        """
        from greenkube.verify.strategy import PostCarbonFactors

        factors = PostCarbonFactors.of(post)
        if factors is None:
            return []
        share = factors.fallback_share(pre)
        if share is None or share <= CARBON_FACTOR_FALLBACK_TOLERANCE:
            return []
        return [
            Confounder(
                kind="co2e_factor_fallback",
                severity="mild",
                detail=f"{share:.0%} of the baseline energy has no post-window carbon factor at its hour of week "
                f"and is valued at the time-averaged post factor ({factors.fallback:.4g} gCO2e/kWh)",
                evidence={"share": round(share, 6), "fallback": round(factors.fallback, 6)},
            )
        ]

    @staticmethod
    def _intensity_shift(pre: Sequence[HourBucket], post: Sequence[HourBucket]) -> List[Confounder]:
        """Carbon factor (grid intensity x PUE per kWh) of the two windows (MEA-03).

        The CO2e saving is valued at the post-window factors, so a move does not
        bias it; it is reported (moderate above 10%) because the baseline CO2e
        the change is compared with was emitted at another factor, and costs no
        confidence (:data:`INFORMATIVE_KINDS`). Without a
        post-window factor the raw CO2e difference is measured: moderate.
        """
        from greenkube.verify.series import carbon_factor, quantity_series

        before, after = carbon_factor(pre), carbon_factor(post)
        if after is None:
            if quantity_series(post, "co2e_g"):
                return [
                    Confounder(
                        kind="co2e_raw_delta",
                        severity="moderate",
                        detail="no energy behind the post-window CO2e: the CO2e saving is the raw difference of "
                        "the two windows' emissions, not normalised for grid intensity",
                        evidence={},
                    )
                ]
            return []
        if before is None or before <= 0:
            return []
        deviation = after / before - 1.0
        if abs(deviation) <= INTENSITY_SHIFT_TOLERANCE:
            return []
        return [
            Confounder(
                kind="intensity_shift",
                severity="moderate",
                detail=f"grid carbon intensity x PUE per kWh changed {deviation:+.0%} between the windows "
                f"({before:.4g} -> {after:.4g} gCO2e/kWh); the CO2e saving is valued at the post-window factor",
                evidence={"before": round(before, 6), "after": round(after, 6)},
            )
        ]

    @staticmethod
    def _instance_types(pre: Sequence[HourBucket], post: Sequence[HourBucket]) -> List[Confounder]:
        before = _instance_mix(pre)
        after = _instance_mix(post)
        if not before or not after:
            return []
        distance = _distance(before, after)
        if distance <= INSTANCE_MIX_TOLERANCE:
            return []
        return [
            Confounder(
                kind="instance_type_change",
                severity="severe" if distance > INSTANCE_MIX_SEVERE else "moderate",
                detail=f"the node instance types running the workload changed ({distance:.0%} of pod-hours)",
                evidence={"before": before, "after": after},
            )
        ]

    @staticmethod
    def _pool(change: ChangeEvent, pool: PoolContext) -> List[Confounder]:
        if pool.scope == SCOPE_WORKLOAD_NODES:
            return _node_prices(pool)
        findings: List[Confounder] = []
        pools = ", ".join(pool.pools)
        before, after = _pool_mix(pool.pre), _pool_mix(pool.post)
        if before and after:
            distance = _distance(before, after)
            if distance > POOL_INSTANCE_MIX_TOLERANCE:
                findings.append(
                    Confounder(
                        kind="pool_instance_type_change",
                        severity="severe",
                        detail=f"node pool {pools} changed hardware ({distance:.0%} of its CPU capacity moved to "
                        "other instance types): unit prices and power profiles changed under the workload",
                        evidence={"pools": list(pool.pools), "before": before, "after": after},
                    )
                )
        findings.extend(_pool_prices(pool, pools))
        capacity_before, capacity_after = _pool_capacity(pool.pre), _pool_capacity(pool.post)
        shifted = False
        if capacity_before and capacity_after is not None and capacity_before > 0:
            deviation = capacity_after / capacity_before - 1.0
            if abs(deviation) > POOL_CAPACITY_TOLERANCE:
                shifted = True
                findings.append(
                    Confounder(
                        kind="pool_capacity_change",
                        severity="severe" if abs(deviation) > POOL_CAPACITY_SEVERE else "moderate",
                        detail=f"node pool {pools} CPU capacity changed {deviation:+.0%} between the windows "
                        "(cluster autoscaler): the idle share allocated to the workload moved",
                        evidence={
                            "pools": list(pool.pools),
                            "before_cores": round(capacity_before, 4),
                            "after_cores": round(capacity_after, 4),
                        },
                    )
                )
        if not shifted:
            added, removed = _churn(pool.post)
            if added or removed:
                findings.append(
                    Confounder(
                        kind="autoscaler_activity",
                        severity="mild",
                        detail=f"{len(added)} node(s) added and {len(removed)} removed in node pool {pools} "
                        "during the post-window",
                        evidence={"added": sorted(added), "removed": sorted(removed)},
                    )
                )
        for other in pool.other_changes:
            if other.change_id == change.change_id or other.target.key == change.target.key:
                continue
            findings.append(
                Confounder(
                    kind="pool_change",
                    severity="moderate",
                    detail=f"change {other.change_id} to {'/'.join(other.target.key)} landed in node pool "
                    f"{pools} during the measurement",
                    evidence={
                        "change_id": other.change_id,
                        "target": "/".join(other.target.key),
                        "rollout_completed_at": ensure_utc(other.rollout_completed_at).isoformat(),
                    },
                )
            )
        if pool.truncated:
            findings.append(
                Confounder(
                    kind="pool_check_truncated",
                    severity="mild",
                    detail=f"more changes landed during the measurement than were checked for node pool {pools}: "
                    "a later change in the pool may be missed",
                    evidence={"checked": len(pool.other_changes), "limit_reached": True},
                )
            )
        return findings


def _usage_within_request(buckets: Sequence[HourBucket]) -> bool:
    """Whether the mean CPU usage stays at or below the CPU request in every costed hour that has both."""
    known = [
        bucket
        for bucket in buckets
        if bucket.cpu_usage_cores is not None and bucket.core_hours is not None and bucket.cost is not None
    ]
    return bool(known) and all((bucket.cpu_usage_cores or 0.0) <= (bucket.core_hours or 0.0) for bucket in known)


def _price_finding(resource: str, before: Optional[float], after: Optional[float], basis: str) -> Optional[Confounder]:
    """``price_card_change`` when a unit price moved more than 5% (severe above 20%)."""
    if before is None or after is None or before <= 0:
        return None
    deviation = after / before - 1.0
    if abs(deviation) <= PRICE_CHANGE_TOLERANCE:
        return None
    return Confounder(
        kind="price_card_change",
        severity="severe" if abs(deviation) > PRICE_CHANGE_SEVERE else "moderate",
        detail=f"{resource} unit price changed {deviation:+.0%} between the windows (the saving is measured at "
        "the baseline prices; the observed difference is reported as allocated_cost_observed)",
        evidence={"resource": resource, "before": before, "after": after, "basis": basis},
    )


def _pool_prices(pool: PoolContext, pools: str) -> List[Confounder]:
    findings: List[Confounder] = []
    for resource, unit in (("cpu", "core-hour"), ("memory", "GiB-hour")):
        before, after = pool.prices_pre.get(resource), pool.prices_post.get(resource)
        if before is None or after is None or before <= 0:
            continue
        deviation = after / before - 1.0
        if abs(deviation) <= PRICE_CHANGE_TOLERANCE:
            continue
        findings.append(
            Confounder(
                kind="pool_price_card_change",
                severity="severe" if abs(deviation) > PRICE_CHANGE_SEVERE else "moderate",
                detail=f"{resource} unit price per {unit} in node pool {pools} changed {deviation:+.0%} between "
                "the windows (all workloads of the pool)",
                evidence={"resource": resource, "pools": list(pool.pools), "before": before, "after": after},
            )
        )
    return findings


#: :attr:`PoolContext.scope` of a price control built from the workload's nodes (no pool label).
SCOPE_WORKLOAD_NODES = "workload_nodes"


def _node_prices(pool: PoolContext) -> List[Confounder]:
    """Price control over every pod of the workload's nodes, for clusters without pool labels (MEA-02)."""
    findings: List[Confounder] = []
    nodes = ", ".join(pool.nodes)
    for resource, unit in (("cpu", "core-hour"), ("memory", "GiB-hour")):
        before, after = pool.prices_pre.get(resource), pool.prices_post.get(resource)
        if before is None or after is None or before <= 0:
            continue
        deviation = after / before - 1.0
        if abs(deviation) <= PRICE_CHANGE_TOLERANCE:
            continue
        findings.append(
            Confounder(
                kind="pool_price_card_change",
                severity="severe" if abs(deviation) > PRICE_CHANGE_SEVERE else "moderate",
                detail=f"{resource} unit price per {unit} on the nodes the workload ran on ({nodes}; no node-pool "
                f"label) changed {deviation:+.0%} between the windows (all workloads of those nodes)",
                evidence={
                    "resource": resource,
                    "scope": SCOPE_WORKLOAD_NODES,
                    "nodes": list(pool.nodes),
                    "before": before,
                    "after": after,
                },
            )
        )
    return findings


def is_contaminated(confounders: Sequence[Confounder]) -> bool:
    return any(item.severity == "severe" for item in confounders)


def confidence_score(confounders: Sequence[Confounder], coverage: float, load: Optional[ResolvedLoad]) -> float:
    """Confidence in a measurement: coverage, minus penalties for confounders and a weak load driver.

    Kinds of :data:`INFORMATIVE_KINDS` are reported but cost no confidence:
    the verdict is the cost verdict, which they do not touch.
    """
    if is_contaminated(confounders):
        return 0.0
    score = max(0.0, min(coverage, 1.0))
    score -= math.fsum(_PENALTY.get(item.severity, 0.0) for item in confounders if item.kind not in INFORMATIVE_KINDS)
    if load is None or load.info.weak:
        score -= WEAK_LOAD_PENALTY
    return round(max(0.0, min(score, 1.0)), 4)
