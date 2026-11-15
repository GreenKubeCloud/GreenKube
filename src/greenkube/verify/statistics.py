# src/greenkube/verify/statistics.py
"""Interval of a mean saving and the fixed-horizon outcome rule (C4, MEA-01, MEA-09).

The interval of the mean hourly saving has two variance terms
(docs/methodology/savings-measurement.md, section 5):

* **within the windows**: hourly savings are autocorrelated (a busy afternoon
  follows a busy morning), so the standard error of their mean comes from a
  *circular* block bootstrap of one-day blocks (blocks may wrap around the end
  of the series, so every hour is resampled equally often). The bootstrap
  variance is corrected by ``b / (b - 1)`` for the ``b`` independent blocks and
  carries ``b - 1`` degrees of freedom;
* **between weeks**: anything that moves the level of a whole week (traffic,
  prices, grid intensity, a holiday) is a constant offset in every hourly
  saving of that week, which no resampling inside the windows can see. Its
  variance is ``σ_w² × (1/K + 1/P)`` for ``K`` baseline weeks and ``P`` post
  weeks, with ``σ_w`` the standard deviation of the week-level mean residual of
  the baseline (``K - 1`` degrees of freedom) or, with fewer than two baseline
  weeks, a configured prior coefficient of variation times the counterfactual.

The interval is ``mean ± t(q, ν) × √(se_within² + se_between²)`` with ``ν``
from the Welch-Satterthwaite formula. The resampling is seeded from the
measurement identity, so the same evidence always yields the same interval
(an auditor can reproduce it).
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from statistics import NormalDist
from typing import Dict, List, Mapping, Optional, Sequence

from greenkube.verify.model import ChangeOutcome

#: Block length of the block bootstrap: one day of hourly values.
DAY_BLOCK = 24
#: Two-sided confidence level of the reported interval.
CONFIDENCE = 0.90
#: Hours of one week (the period of the hour-of-week counterfactual).
WEEK_HOURS = 168
#: Share of a week's hours a baseline week needs to count for the between-week term.
MIN_WEEK_SHARE = 0.5
#: Relative spread below which a baseline series is deterministic (requests x a fixed price).
DETERMINISTIC_SPREAD = 1e-6
#: Name recorded in ``counterfactual_params.interval_method``.
INTERVAL_METHOD = "circular_block_bootstrap_t_with_between_week"


@dataclass(frozen=True)
class BootstrapInterval:
    mean: float
    low: Optional[float]
    high: Optional[float]
    samples: int
    block: int
    #: Standard error of the mean (within-window term, plus the between-week term once added).
    se: Optional[float] = None
    #: Degrees of freedom of ``se`` (``math.inf`` when it is known, not estimated).
    dof: Optional[float] = None
    #: Between-week standard error included in ``se`` (0 when none was added).
    between_se: float = 0.0

    def scaled(self, factor: float) -> "BootstrapInterval":
        """The interval of ``factor × value`` (``factor > 0``, e.g. a per-replica rate times replicas)."""
        return replace(
            self,
            mean=self.mean * factor,
            low=self.low * factor if self.low is not None else None,
            high=self.high * factor if self.high is not None else None,
            se=self.se * factor if self.se is not None else None,
            between_se=self.between_se * factor,
        )


def seed_for(*parts: object) -> int:
    """Deterministic 64-bit seed from the measurement identity."""
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def percentile(ordered: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile ``q`` of an already sorted sequence."""
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


# --------------------------------------------------------------------------- #
# Student's t distribution (no SciPy dependency)                               #
# --------------------------------------------------------------------------- #

_TINY = 1e-300


def _guard(value: float) -> float:
    return value if abs(value) > _TINY else _TINY


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction of the regularised incomplete beta function (modified Lentz)."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 / _guard(1.0 - qab * x / qap)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 / _guard(1.0 + aa * d)
        c = _guard(1.0 + aa / c)
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 / _guard(1.0 + aa * d)
        c = _guard(1.0 + aa / c)
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return h


def _incomplete_beta(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(log_front) * _betacf(a, b, x) / a
    return 1.0 - math.exp(log_front) * _betacf(b, a, 1.0 - x) / b


def student_t_cdf(t: float, dof: float) -> float:
    """P(T <= t) for Student's t with ``dof`` degrees of freedom (normal when ``dof`` is infinite)."""
    if math.isinf(dof):
        return NormalDist().cdf(t)
    tail = 0.5 * _incomplete_beta(dof / 2.0, 0.5, dof / (dof + t * t))
    return 1.0 - tail if t >= 0 else tail


def student_t_quantile(p: float, dof: float) -> float:
    """Quantile ``p`` (0 < p < 1) of Student's t with ``dof`` > 0 degrees of freedom."""
    if not 0.0 < p < 1.0 or dof <= 0:
        raise ValueError("student_t_quantile needs 0 < p < 1 and dof > 0")
    if math.isinf(dof) or dof > 1e7:
        return NormalDist().inv_cdf(p)
    if p < 0.5:
        return -student_t_quantile(1.0 - p, dof)
    low, high = 0.0, 1.0
    while student_t_cdf(high, dof) < p:
        high *= 2.0
    for _ in range(200):
        middle = (low + high) / 2.0
        if student_t_cdf(middle, dof) < p:
            low = middle
        else:
            high = middle
        if high - low < 1e-12:
            break
    return (low + high) / 2.0


def _t_interval(mean: float, se: float, dof: float, confidence: float) -> tuple[float, float]:
    if se <= 0.0:
        return mean, mean
    half = student_t_quantile(1.0 - (1.0 - confidence) / 2.0, dof) * se
    return mean - half, mean + half


# --------------------------------------------------------------------------- #
# Within-window term: circular block bootstrap                                 #
# --------------------------------------------------------------------------- #


def circular_resample(data: Sequence[float], rng: random.Random, size: int) -> List[float]:
    """One circular block resample of ``data``: blocks of ``size`` start anywhere and wrap around."""
    n = len(data)
    resampled: List[float] = []
    while len(resampled) < n:
        begin = rng.randrange(n)
        resampled.extend(data[(begin + offset) % n] for offset in range(size))
    return resampled[:n]


def block_size(n: int, block: int = DAY_BLOCK) -> int:
    """Block length used for ``n`` values: one day, or half the series when it is shorter than two days."""
    return max(1, min(block, n // 2))


def bootstrap_se(means: Sequence[float], n: int, size: int) -> tuple[float, float]:
    """Standard error and degrees of freedom of a mean from its block-bootstrap replicates.

    The replicate variance is corrected by ``b / (b - 1)`` for ``b = n / size``
    blocks (the bootstrap divides by ``b``) and has ``b - 1`` degrees of freedom.
    """
    blocks = n / size
    dof = max(blocks - 1.0, 1.0)
    centre = math.fsum(means) / len(means)
    variance = math.fsum((value - centre) ** 2 for value in means) / max(len(means) - 1, 1)
    return math.sqrt(variance * blocks / dof), dof


def circular_block_bootstrap(
    values: Sequence[float],
    *,
    seed: int,
    samples: int = 1000,
    block: int = DAY_BLOCK,
    confidence: float = CONFIDENCE,
) -> BootstrapInterval:
    """Mean of ``values`` and its within-window interval (circular block bootstrap, Student t).

    The interval is ``mean ± t(q, b - 1) × se`` (see :func:`bootstrap_se`).
    With fewer than two values there is no interval (``low``/``high`` are
    ``None``); a series shorter than two blocks uses blocks of half its length.
    """
    data = [float(value) for value in values]
    n = len(data)
    if n == 0:
        return BootstrapInterval(mean=0.0, low=None, high=None, samples=0, block=block)
    mean = math.fsum(data) / n
    if n < 2 or samples <= 0:
        return BootstrapInterval(mean=mean, low=None, high=None, samples=0, block=block)
    size = block_size(n, block)
    rng = random.Random(seed)
    means = [math.fsum(circular_resample(data, rng, size)) / n for _ in range(samples)]
    se, dof = bootstrap_se(means, n, size)
    if se <= 1e-12 * max(abs(mean), 1.0):
        se = 0.0
    low, high = _t_interval(mean, se, dof, confidence)
    return BootstrapInterval(mean=mean, low=low, high=high, samples=samples, block=size, se=se, dof=dof)


#: The C4 name, kept for callers: the bootstrap is circular and studentised since MEA-01.
moving_block_bootstrap = circular_block_bootstrap


# --------------------------------------------------------------------------- #
# Between-week term                                                            #
# --------------------------------------------------------------------------- #


def week_level_deviations(
    residuals: Mapping[datetime, float], end: datetime, *, min_share: float = MIN_WEEK_SHARE
) -> List[float]:
    """Mean baseline residual of each week counted back from ``end``, oldest week first.

    A residual is an hour's value minus its fitted counterfactual. Only weeks
    holding at least ``min_share`` of their 168 hours count. The spread of
    these means is the week-to-week variation the hourly bootstrap cannot see.
    """
    weeks: Dict[int, List[float]] = {}
    span = timedelta(hours=WEEK_HOURS)
    for hour, value in residuals.items():
        if hour >= end:
            continue
        weeks.setdefault(int((end - hour - timedelta(hours=1)) // span), []).append(float(value))
    complete = [
        (index, math.fsum(items) / len(items)) for index, items in weeks.items() if len(items) >= min_share * WEEK_HOURS
    ]
    return [mean for _, mean in sorted(complete, reverse=True)]


@dataclass(frozen=True)
class BetweenWeekTerm:
    """Standard error a week-level shift adds to the mean saving (see the module docstring)."""

    se: float
    dof: float
    baseline_weeks: int
    post_weeks: float
    #: ``estimated`` (from K >= 2 baseline weeks), ``prior`` or ``deterministic_baseline``.
    basis: str
    #: ``σ_w`` relative to the counterfactual mean (``None`` when the counterfactual is 0).
    cv: Optional[float]

    def as_params(self) -> dict:
        """Values recorded in ``counterfactual_params``."""
        return {
            "baseline_weeks": self.baseline_weeks,
            "post_weeks": round(self.post_weeks, 4),
            "between_week_basis": self.basis,
            "between_week_cv": round(self.cv, 6) if self.cv is not None else None,
        }


def between_week_term(
    residuals: Mapping[datetime, float],
    baseline_values: Sequence[float],
    end: datetime,
    *,
    counterfactual: float,
    post_hours: int,
    prior_cv: float,
) -> BetweenWeekTerm:
    """Between-week standard error of a mean saving.

    ``residuals`` are the baseline hours minus their fitted counterfactual,
    ``end`` the baseline end and ``post_hours`` the aligned post-window hours.
    With ``K >= 2`` complete baseline weeks, ``σ_w`` is the sample standard
    deviation of their mean residuals (``K - 1`` degrees of freedom). With
    fewer, ``σ_w = prior_cv × |counterfactual|`` (taken as known), except for
    a deterministic baseline (every value equal within 1e-6 relative): its
    level is set by configuration (requests times a price card), whose changes
    the confounder checks report, so no week-level variation is added.
    """
    deviations = week_level_deviations(residuals, end)
    weeks = len(deviations)
    post_weeks = max(1.0, post_hours / WEEK_HOURS)
    reference = abs(counterfactual)
    if weeks >= 2:
        centre = math.fsum(deviations) / weeks
        sigma = math.sqrt(math.fsum((item - centre) ** 2 for item in deviations) / (weeks - 1))
        dof = float(weeks - 1)
        basis = "estimated"
    else:
        spread = (max(baseline_values) - min(baseline_values)) if baseline_values else 0.0
        if spread <= DETERMINISTIC_SPREAD * max(reference, 1e-12):
            sigma, basis = 0.0, "deterministic_baseline"
        else:
            sigma, basis = prior_cv * reference, "prior"
        dof = math.inf
    se = sigma * math.sqrt(1.0 / max(weeks, 1) + 1.0 / post_weeks)
    return BetweenWeekTerm(
        se=se,
        dof=dof,
        baseline_weeks=weeks,
        post_weeks=post_weeks,
        basis=basis,
        cv=sigma / reference if reference > 0 else None,
    )


def with_between_week(
    interval: BootstrapInterval, term: BetweenWeekTerm, *, confidence: float = CONFIDENCE
) -> BootstrapInterval:
    """``interval`` widened by the between-week term (Welch-Satterthwaite degrees of freedom).

    An *estimated* term comes from week means of the baseline, which already
    carry the within-week noise the bootstrap measures: only its excess over
    the within-window variance is added (a method-of-moments split, so the
    noise is not counted twice). A *prior* term is pure week-to-week variation
    and is added in full.
    """
    if interval.low is None or interval.high is None or interval.se is None:
        return interval
    within_var = interval.se**2
    between_var = term.se**2
    if term.basis == "estimated":
        between_var = max(0.0, between_var - within_var)
    total = within_var + between_var
    if total <= 0.0:
        return replace(interval, low=interval.mean, high=interval.mean, se=0.0, between_se=0.0)
    within_dof = interval.dof if interval.dof is not None else math.inf
    denominator = 0.0
    if not math.isinf(within_dof):
        denominator += within_var**2 / within_dof
    if not math.isinf(term.dof):
        denominator += between_var**2 / term.dof
    dof = total**2 / denominator if denominator > 0 else math.inf
    se = math.sqrt(total)
    low, high = _t_interval(interval.mean, se, dof, confidence)
    return replace(interval, low=low, high=high, se=se, dof=dof, between_se=term.se)


def outcome_rule(ci_low: Optional[float], ci_high: Optional[float], epsilon: float) -> ChangeOutcome:
    """Fixed-horizon outcome of one quantity (no repeated testing, MEA-09).

    ``realized`` when the interval's lower bound is above the noise floor
    ``epsilon``, ``not_realized`` when its upper bound is at most ``epsilon``
    (the saving is at best negligible), otherwise ``inconclusive`` (the
    interval straddles the floor).
    """
    if ci_low is None or ci_high is None:
        return ChangeOutcome.INCONCLUSIVE
    if ci_low > epsilon:
        return ChangeOutcome.REALIZED
    if ci_high <= epsilon:
        return ChangeOutcome.NOT_REALIZED
    return ChangeOutcome.INCONCLUSIVE
