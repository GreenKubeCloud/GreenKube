# src/greenkube/verify/statistics.py
"""Moving-block bootstrap and the fixed-horizon outcome rule (C4).

Hourly savings are autocorrelated (a busy afternoon follows a busy morning),
so resampling single hours would understate the uncertainty. The moving-block
bootstrap resamples contiguous blocks of one day (24 hourly values, starting
at any hour), concatenates them back to the series length and takes the mean;
the 5th and 95th percentiles of those means form the 90% interval.

The resampling is seeded from the measurement identity, so the same evidence
always yields the same interval (an auditor can reproduce it).
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from typing import Optional, Sequence

from greenkube.verify.model import ChangeOutcome

#: Block length of the moving-block bootstrap: one day of hourly values.
DAY_BLOCK = 24
#: Two-sided confidence level of the reported interval.
CONFIDENCE = 0.90


@dataclass(frozen=True)
class BootstrapInterval:
    mean: float
    low: Optional[float]
    high: Optional[float]
    samples: int
    block: int

    def scaled(self, factor: float) -> "BootstrapInterval":
        """The interval of ``factor × value`` (``factor > 0``, e.g. a per-replica rate times replicas)."""
        return BootstrapInterval(
            mean=self.mean * factor,
            low=self.low * factor if self.low is not None else None,
            high=self.high * factor if self.high is not None else None,
            samples=self.samples,
            block=self.block,
        )


def seed_for(*parts: object) -> int:
    """Deterministic 64-bit seed from the measurement identity."""
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def percentile(ordered: Sequence[float], q: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def moving_block_bootstrap(
    values: Sequence[float],
    *,
    seed: int,
    samples: int = 1000,
    block: int = DAY_BLOCK,
    confidence: float = CONFIDENCE,
) -> BootstrapInterval:
    """Mean of ``values`` and its moving-block bootstrap interval.

    With fewer than two values there is no interval (``low``/``high`` are
    ``None``). A series shorter than one block is resampled with blocks of its
    own length minus one hour, so it still varies.
    """
    data = [float(value) for value in values]
    n = len(data)
    if n == 0:
        return BootstrapInterval(mean=0.0, low=None, high=None, samples=0, block=block)
    mean = math.fsum(data) / n
    if n < 2 or samples <= 0:
        return BootstrapInterval(mean=mean, low=None, high=None, samples=0, block=block)
    size = max(1, min(block, n - 1))
    starts = n - size + 1
    blocks_needed = math.ceil(n / size)
    rng = random.Random(seed)
    means = []
    for _ in range(samples):
        resampled: list[float] = []
        for _ in range(blocks_needed):
            begin = rng.randrange(starts)
            resampled.extend(data[begin : begin + size])
        means.append(math.fsum(resampled[:n]) / n)
    means.sort()
    tail = (1.0 - confidence) / 2.0
    return BootstrapInterval(
        mean=mean,
        low=percentile(means, tail),
        high=percentile(means, 1.0 - tail),
        samples=samples,
        block=size,
    )


def outcome_rule(ci_low: Optional[float], ci_high: Optional[float], epsilon: float) -> ChangeOutcome:
    """Fixed-horizon outcome of one quantity (no repeated testing).

    ``realized`` when the interval's lower bound is above zero,
    ``not_realized`` when its upper bound is at most ``epsilon`` (the saving is
    at best negligible), otherwise ``inconclusive``.
    """
    if ci_low is None or ci_high is None:
        return ChangeOutcome.INCONCLUSIVE
    if ci_low > 0:
        return ChangeOutcome.REALIZED
    if ci_high <= epsilon:
        return ChangeOutcome.NOT_REALIZED
    return ChangeOutcome.INCONCLUSIVE
