# src/greenkube/verify/regression.py
"""Load-regression counterfactual (C3 v2, IPMVP Option B style).

The model is fitted on the baseline window only::

    metric(t) = α[slot(t)] + β × load(t) + ε(t)

``slot(t)`` is a seasonal slot: the hour of the week when the baseline holds
at least two observations of every hour of the week (14 days or more),
otherwise the hour of the day split into weekdays and weekends (48 slots). A
7-day baseline has a single observation per hour of the week, so hour-of-week
dummies plus a load term would be unidentifiable; the coarser partition keeps
enough degrees of freedom to estimate ``β`` from within-slot load variation.

Because the slots partition the hours, the least-squares fit has a closed form
(Frisch-Waugh-Lovell): ``β`` is the slope of the within-slot demeaned metric on
the within-slot demeaned load, and ``α[s] = mean_s(metric) - β mean_s(load)``.
The counterfactual of a post-change hour is ``α[slot(t)] + β load(t)``.

The 90% interval of the mean saving combines two sources of uncertainty, each
resampled in one-day moving blocks so autocorrelation is kept:

* the post-window noise (the hourly savings, as in the MVP);
* the baseline fit (a residual bootstrap: the fitted baseline plus resampled,
  degrees-of-freedom-inflated residuals is refitted, which shifts the
  counterfactual of every post-change hour). The shift is linear in the
  resampled residuals, so it is computed with one weight per baseline hour
  instead of a refit.

The fit is used only when its diagnostics pass; otherwise the measurement
falls back to the hour-of-week MVP (:mod:`greenkube.verify.counterfactual`)
and records why.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from greenkube.verify.counterfactual import AlignedHour
from greenkube.verify.model import hour_of_week
from greenkube.verify.statistics import (
    CONFIDENCE,
    DAY_BLOCK,
    BootstrapInterval,
    block_size,
    bootstrap_se,
    circular_resample,
    student_t_quantile,
)

METHOD_OLS = "ols_load_seasonal"
SEASONALITY_HOUR_OF_WEEK = "hour_of_week"
SEASONALITY_DAY_TYPE = "hour_of_day_x_day_type"

#: Minimum paired baseline hours (metric and load) to fit the regression.
MIN_POINTS = 48
#: Minimum residual degrees of freedom (observations minus slots minus the load term).
MIN_DOF = 24
#: Minimum observations per hour-of-week slot to use hour-of-week seasonality.
MIN_PER_HOUR_OF_WEEK = 2
#: Share of the 168 hours of the week that must reach that minimum.
HOUR_OF_WEEK_COVERAGE = 0.9
#: Within-slot load variation (coefficient of variation) below which ``β`` is not identifiable.
MIN_LOAD_VARIATION = 1e-3
#: A negative load coefficient more significant than this t-statistic fails the fit.
NEGATIVE_SLOPE_T = -2.0
#: Post-change hours whose load lies outside the baseline range widened by this share of it are extrapolated.
EXTRAPOLATION_MARGIN = 0.25
#: The fit fails when more than this share of the post-change hours is extrapolated.
MAX_EXTRAPOLATED_SHARE = 0.2


def seasonal_slot(moment: datetime, seasonality: str) -> int:
    """Slot index of ``moment``: hour of the week, or hour of the day x weekday/weekend."""
    if seasonality == SEASONALITY_HOUR_OF_WEEK:
        return hour_of_week(moment)
    return moment.hour + (24 if moment.weekday() >= 5 else 0)


def choose_seasonality(hours: Sequence[datetime]) -> str:
    counts: Dict[int, int] = {}
    for hour in hours:
        how = hour_of_week(hour)
        counts[how] = counts.get(how, 0) + 1
    enough = sum(1 for count in counts.values() if count >= MIN_PER_HOUR_OF_WEEK)
    if enough >= HOUR_OF_WEEK_COVERAGE * 168:
        return SEASONALITY_HOUR_OF_WEEK
    return SEASONALITY_DAY_TYPE


@dataclass
class OlsDiagnostics:
    """Fit statistics of the regression, recorded with the measurement."""

    seasonality: str = SEASONALITY_DAY_TYPE
    observations: int = 0
    slots: int = 0
    dof: int = 0
    r2: Optional[float] = None
    adjusted_r2: Optional[float] = None
    residual_std: Optional[float] = None
    load_coefficient: Optional[float] = None
    load_std_error: Optional[float] = None
    load_t: Optional[float] = None
    durbin_watson: Optional[float] = None
    extrapolated_share: Optional[float] = None
    failures: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict:
        def rounded(value: Optional[float]) -> Optional[float]:
            return None if value is None else round(value, 6)

        return {
            "seasonality": self.seasonality,
            "observations": self.observations,
            "slots": self.slots,
            "dof": self.dof,
            "r2": rounded(self.r2),
            "adjusted_r2": rounded(self.adjusted_r2),
            "residual_std": rounded(self.residual_std),
            "load_coefficient": rounded(self.load_coefficient),
            "load_std_error": rounded(self.load_std_error),
            "load_t": rounded(self.load_t),
            "durbin_watson": rounded(self.durbin_watson),
            "extrapolated_share": rounded(self.extrapolated_share),
            "passed": self.passed,
            "failures": list(self.failures),
        }


@dataclass
class OlsCounterfactual:
    """``metric ~ slot + load`` fitted on the baseline (see the module docstring)."""

    seasonality: str
    beta: float
    slot_means: Dict[int, float]
    slot_load_means: Dict[int, float]
    slot_counts: Dict[int, int]
    #: Baseline hours in time order with their slot, demeaned load and residual.
    baseline: List[Tuple[datetime, int, float, float]]
    sxx: float
    diagnostics: OlsDiagnostics
    load_min: float = 0.0
    load_max: float = 0.0

    method = METHOD_OLS

    @classmethod
    def fit(
        cls,
        values: Mapping[datetime, float],
        load: Mapping[datetime, float],
        post_load: Optional[Mapping[datetime, float]] = None,
    ) -> "OlsCounterfactual":
        """Fit on the baseline hours that have both a value and a load; diagnostics say if it may be used."""
        hours = sorted(hour for hour in values if load.get(hour) is not None and math.isfinite(load[hour]))
        seasonality = choose_seasonality(hours)
        diagnostics = OlsDiagnostics(seasonality=seasonality, observations=len(hours))
        groups: Dict[int, List[datetime]] = {}
        for hour in hours:
            groups.setdefault(seasonal_slot(hour, seasonality), []).append(hour)
        slot_means = {slot: math.fsum(values[h] for h in members) / len(members) for slot, members in groups.items()}
        slot_load = {slot: math.fsum(load[h] for h in members) / len(members) for slot, members in groups.items()}
        counts = {slot: len(members) for slot, members in groups.items()}
        diagnostics.slots = len(groups)
        diagnostics.dof = len(hours) - len(groups) - 1

        sxx = sxy = 0.0
        for hour in hours:
            slot = seasonal_slot(hour, seasonality)
            dx = load[hour] - slot_load[slot]
            sxx += dx * dx
            sxy += dx * (values[hour] - slot_means[slot])
        mean_load = math.fsum(load[h] for h in hours) / len(hours) if hours else 0.0
        beta = sxy / sxx if sxx > 0 else 0.0

        residuals: List[Tuple[datetime, int, float, float]] = []
        sse = 0.0
        for hour in hours:
            slot = seasonal_slot(hour, seasonality)
            dx = load[hour] - slot_load[slot]
            residual = values[hour] - slot_means[slot] - beta * dx
            sse += residual * residual
            residuals.append((hour, slot, dx, residual))
        model = cls(
            seasonality=seasonality,
            beta=beta,
            slot_means=slot_means,
            slot_load_means=slot_load,
            slot_counts=counts,
            baseline=residuals,
            sxx=sxx,
            diagnostics=diagnostics,
            load_min=min((load[h] for h in hours), default=0.0),
            load_max=max((load[h] for h in hours), default=0.0),
        )

        failures = diagnostics.failures
        if len(hours) < MIN_POINTS:
            failures.append(f"only {len(hours)} baseline hours with load (at least {MIN_POINTS} needed)")
        if diagnostics.dof < MIN_DOF:
            failures.append(f"{diagnostics.dof} residual degrees of freedom (at least {MIN_DOF} needed)")
        within_sd = math.sqrt(sxx / len(hours)) if hours else 0.0
        if not hours or within_sd <= MIN_LOAD_VARIATION * max(abs(mean_load), 1e-12):
            failures.append("the load does not vary within seasonal slots: its coefficient is not identifiable")
        if failures:
            return model

        mean_y = math.fsum(values[h] for h in hours) / len(hours)
        sst = math.fsum((values[h] - mean_y) ** 2 for h in hours)
        dof = diagnostics.dof
        sigma2 = sse / dof
        diagnostics.residual_std = math.sqrt(sigma2)
        diagnostics.load_coefficient = beta
        diagnostics.load_std_error = math.sqrt(sigma2 / sxx)
        diagnostics.load_t = beta / diagnostics.load_std_error if diagnostics.load_std_error > 0 else None
        if sst > 0:
            diagnostics.r2 = 1.0 - sse / sst
            diagnostics.adjusted_r2 = 1.0 - (sse / dof) / (sst / (len(hours) - 1))
        if sse > 0:
            diffs = math.fsum(
                (residuals[i][3] - residuals[i - 1][3]) ** 2
                for i in range(1, len(residuals))
                if (residuals[i][0] - residuals[i - 1][0]).total_seconds() == 3600
            )
            diagnostics.durbin_watson = diffs / sse
        if diagnostics.load_t is not None and beta < 0 and diagnostics.load_t < NEGATIVE_SLOPE_T:
            failures.append(
                f"the metric falls as the load grows (coefficient {beta:.6g}, t={diagnostics.load_t:.1f}): "
                "the load does not explain it"
            )
        if post_load:
            extrapolated = model.extrapolated_share(post_load.values())
            diagnostics.extrapolated_share = extrapolated
            if extrapolated > MAX_EXTRAPOLATED_SHARE:
                failures.append(
                    f"{extrapolated:.0%} of the post-change hours have a load outside the baseline range "
                    f"(at most {MAX_EXTRAPOLATED_SHARE:.0%} may be extrapolated)"
                )
        return model

    def extrapolated_share(self, loads: Iterable[float]) -> float:
        items = [float(value) for value in loads if value is not None and math.isfinite(float(value))]
        if not items:
            return 0.0
        margin = EXTRAPOLATION_MARGIN * max(self.load_max - self.load_min, abs(self.load_max) * 0.1)
        low, high = self.load_min - margin, self.load_max + margin
        return sum(1 for value in items if value < low or value > high) / len(items)

    @property
    def elasticity(self) -> float:
        """Load elasticity at the baseline means (reported only)."""
        values = [mean for mean in self.slot_means.values()]
        loads = [mean for mean in self.slot_load_means.values()]
        mean_y = math.fsum(values) / len(values) if values else 0.0
        mean_x = math.fsum(loads) / len(loads) if loads else 0.0
        return self.beta * mean_x / mean_y if mean_y else 0.0

    def predict(self, hour: datetime, load: Optional[float]) -> Optional[float]:
        slot = seasonal_slot(hour, self.seasonality)
        if slot not in self.slot_means or load is None or not math.isfinite(load):
            return None
        return self.slot_means[slot] + self.beta * (load - self.slot_load_means[slot])

    def align(self, actual: Mapping[datetime, float], load: Mapping[datetime, float]) -> List[AlignedHour]:
        aligned: List[AlignedHour] = []
        for hour in sorted(actual):
            predicted = self.predict(hour, load.get(hour))
            if predicted is None:
                continue
            aligned.append(AlignedHour(hour=hour, counterfactual=predicted, actual=actual[hour]))
        return aligned

    def residual_weights(self, aligned: Sequence[AlignedHour], load: Mapping[datetime, float]) -> List[float]:
        """Weight of each baseline residual in the mean counterfactual of ``aligned``.

        Refitting on ``fitted + e*`` moves the mean counterfactual by
        ``Σ weight_i e*_i``: the slot means move by the slot mean of ``e*`` and
        ``β`` by ``Σ dx_i e*_i / Sxx``.
        """
        m = len(aligned)
        if m == 0:
            return [0.0 for _ in self.baseline]
        post_slots: Dict[int, int] = {}
        deviation = 0.0
        for item in aligned:
            slot = seasonal_slot(item.hour, self.seasonality)
            post_slots[slot] = post_slots.get(slot, 0) + 1
            deviation += float(load[item.hour]) - self.slot_load_means[slot]
        deviation /= m
        weights = []
        for _, slot, dx, _ in self.baseline:
            weight = post_slots.get(slot, 0) / (m * self.slot_counts[slot])
            if self.sxx > 0:
                weight += dx * deviation / self.sxx
            weights.append(weight)
        return weights

    def params(self) -> dict:
        return {"model": METHOD_OLS, **self.diagnostics.as_dict(), "matched_slots": len(self.slot_means)}


def residual_bootstrap(
    savings: Sequence[float],
    residuals: Sequence[float],
    weights: Sequence[float],
    *,
    dof: int,
    seed: int,
    samples: int = 1000,
    block: int = DAY_BLOCK,
    confidence: float = CONFIDENCE,
) -> BootstrapInterval:
    """Mean saving and its within-window interval: post-window plus baseline residual bootstrap.

    Both series are resampled with circular day blocks; the standard error of
    the replicates is corrected for the number of post-window blocks and the
    interval uses Student's t (see :mod:`greenkube.verify.statistics`).
    """
    data = [float(value) for value in savings]
    n = len(data)
    if n == 0:
        return BootstrapInterval(mean=0.0, low=None, high=None, samples=0, block=block)
    mean = math.fsum(data) / n
    if n < 2 or samples <= 0:
        return BootstrapInterval(mean=mean, low=None, high=None, samples=0, block=block)
    inflation = math.sqrt(len(residuals) / dof) if dof > 0 else 1.0
    pre = [float(value) * inflation for value in residuals]
    post_size = block_size(n, block)
    pre_size = block_size(len(pre), block) if pre else 1
    rng = random.Random(seed)
    means = []
    for _ in range(samples):
        post_mean = math.fsum(circular_resample(data, rng, post_size)) / n
        shift = 0.0
        if pre:
            shift = math.fsum(w * e for w, e in zip(weights, circular_resample(pre, rng, pre_size)))
        # The counterfactual moves up by ``shift``, so the saving does too.
        means.append(post_mean + shift)
    se, se_dof = bootstrap_se(means, n, post_size)
    if se <= 1e-12 * max(abs(mean), 1.0):
        se = 0.0
    half = student_t_quantile(1.0 - (1.0 - confidence) / 2.0, se_dof) * se
    return BootstrapInterval(
        mean=mean,
        low=mean - half,
        high=mean + half,
        samples=samples,
        block=post_size,
        se=se,
        dof=se_dof,
    )


__all__ = [
    "METHOD_OLS",
    "OlsCounterfactual",
    "OlsDiagnostics",
    "residual_bootstrap",
    "seasonal_slot",
]
