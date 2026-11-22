# src/greenkube/verify/counterfactual.py
"""Hour-of-week counterfactual scaled by the load driver (C3, MVP).

What would the workload have cost after the change if the change had not been
made? For each post-change hour ``t`` the counterfactual is the mean of the
pre-change hours with the same hour of the week, scaled by how much busier or
quieter ``t`` was than those hours::

    counterfactual(t) = mean_pre[how(t)] × (load(t) / load_pre[how(t)]) ** β

``β`` is the load elasticity of the quantity, fitted on the pre-change window
as the least-squares slope of ``log(quantity)`` on ``log(load)`` and clamped
to ``[0, 1]``. A workload whose cost follows its load (autoscaled replicas)
gets ``β ≈ 1``; a fixed-capacity workload, whose requested resources do not
move with load, gets ``β ≈ 0`` and is not rescaled. Scaling a fixed-capacity
workload by the raw load ratio would credit any traffic growth as a saving,
which is the defect BUG-27 fixed in the interim verifier.

Without a usable load driver ``β = 0`` and the counterfactual is the plain
hour-of-week mean.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Mapping, Optional

from greenkube.verify.model import hour_of_week

#: Below this many paired pre-change hours the elasticity is not fitted (β = 0).
MIN_ELASTICITY_POINTS = 24
#: Load variance (in log space) below which the elasticity is not identifiable.
MIN_LOG_LOAD_VARIANCE = 1e-4


def load_elasticity(values: Mapping[datetime, float], load: Mapping[datetime, float]) -> float:
    """Least-squares slope of log(value) on log(load), clamped to [0, 1]."""
    pairs = [
        (math.log(load[hour]), math.log(value))
        for hour, value in values.items()
        if value > 0 and load.get(hour) is not None and load[hour] > 0
    ]
    if len(pairs) < MIN_ELASTICITY_POINTS:
        return 0.0
    mean_x = math.fsum(x for x, _ in pairs) / len(pairs)
    mean_y = math.fsum(y for _, y in pairs) / len(pairs)
    var_x = math.fsum((x - mean_x) ** 2 for x, _ in pairs) / len(pairs)
    if var_x < MIN_LOG_LOAD_VARIANCE:
        return 0.0
    cov = math.fsum((x - mean_x) * (y - mean_y) for x, y in pairs) / len(pairs)
    return max(0.0, min(1.0, cov / var_x))


@dataclass
class HourOfWeekCounterfactual:
    """Hour-of-week means of a quantity (and of its load driver) before a change."""

    means: Dict[int, float]
    load_means: Dict[int, float] = field(default_factory=dict)
    elasticity: float = 0.0

    @classmethod
    def fit(
        cls,
        values: Mapping[datetime, float],
        load: Optional[Mapping[datetime, float]] = None,
    ) -> "HourOfWeekCounterfactual":
        sums: Dict[int, List[float]] = {}
        for hour, value in values.items():
            sums.setdefault(hour_of_week(hour), []).append(value)
        means = {how: math.fsum(items) / len(items) for how, items in sums.items()}
        load_means: Dict[int, float] = {}
        elasticity = 0.0
        if load:
            load_sums: Dict[int, List[float]] = {}
            for hour, value in load.items():
                load_sums.setdefault(hour_of_week(hour), []).append(value)
            load_means = {how: math.fsum(items) / len(items) for how, items in load_sums.items()}
            elasticity = load_elasticity(values, load)
        return cls(means=means, load_means=load_means, elasticity=elasticity)

    def predict(self, hour: datetime, load: Optional[float] = None) -> Optional[float]:
        """Counterfactual value of ``hour``; ``None`` when it cannot be matched."""
        how = hour_of_week(hour)
        mean = self.means.get(how)
        if mean is None:
            return None
        if self.elasticity <= 0:
            return mean
        reference = self.load_means.get(how)
        if load is None or reference is None:
            # A load-sensitive quantity cannot be predicted without the hour's load.
            return None
        if reference <= 0 or load <= 0:
            return mean if reference == load else None
        return mean * (load / reference) ** self.elasticity

    def params(self) -> dict:
        return {
            "model": "hour_of_week_mean",
            "elasticity": round(self.elasticity, 6),
            "matched_hours_of_week": len(self.means),
        }


@dataclass(frozen=True)
class AlignedHour:
    hour: datetime
    counterfactual: float
    actual: float

    @property
    def saving(self) -> float:
        return self.counterfactual - self.actual


def align(
    model: HourOfWeekCounterfactual,
    actual: Mapping[datetime, float],
    load: Optional[Mapping[datetime, float]] = None,
) -> List[AlignedHour]:
    """Post-change hours that have both an actual value and a counterfactual, in time order."""
    aligned: List[AlignedHour] = []
    for hour in sorted(actual):
        predicted = model.predict(hour, load.get(hour) if load is not None else None)
        if predicted is None:
            continue
        aligned.append(AlignedHour(hour=hour, counterfactual=predicted, actual=actual[hour]))
    return aligned
