"""Brute-force protection for the sign-in endpoint (roadmap F14).

Failed sign-ins are counted per client IP and per username with the ``limits``
library already used by the API rate limiter. Past the limit, a key is locked
for one rate-limit window, doubling with every further exhaustion (1, 2, 4 ...
minutes for ``5/minute``, up to 15 minutes); a successful sign-in resets it.
State is per API process: with several replicas the effective limit is
multiplied by the replica count, which keeps the bound small while avoiding a
database write per attempt.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from limits import parse as parse_rate_limit
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter

MAX_BACKOFF_SECONDS = 15 * 60
# The per-IP budget is wider than the per-username one, so that several people
# behind one NAT do not lock each other out with a few typos.
IP_LIMIT_MULTIPLIER = 4
_MAX_TRACKED_KEYS = 10_000


@dataclass
class _Backoff:
    over_budget: int = 0
    locked_until: float = 0.0


class LoginThrottle:
    def __init__(self, rate_limit: str = "5/minute", *, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = parse_rate_limit(rate_limit)
        self._ip_limit = parse_rate_limit(_scale(rate_limit, IP_LIMIT_MULTIPLIER))
        self._limiter = MovingWindowRateLimiter(MemoryStorage())
        self._clock = clock
        self._backoff: OrderedDict[str, _Backoff] = OrderedDict()

    def _keys(self, ip: str, username: str) -> list[tuple[str, object]]:
        return [(f"user:{username.lower()}", self._limit), (f"ip:{ip}", self._ip_limit)]

    def retry_after(self, ip: str, username: str) -> int:
        """Seconds until a new attempt is allowed, or 0 when it is allowed now."""
        now = self._clock()
        wait = 0.0
        for key, limit in self._keys(ip, username):
            backoff = self._backoff.get(key)
            if backoff is not None and backoff.locked_until > now:
                wait = max(wait, backoff.locked_until - now)
            if not self._limiter.test(limit, key):  # type: ignore[arg-type]
                stats = self._limiter.get_window_stats(limit, key)  # type: ignore[arg-type]
                wait = max(wait, stats.reset_time - time.time())
        return max(1, int(wait + 0.999)) if wait > 0 else 0

    def record_failure(self, ip: str, username: str) -> None:
        now = self._clock()
        for key, limit in self._keys(ip, username):
            self._limiter.hit(limit, key)  # type: ignore[arg-type]
            backoff = self._backoff.pop(key, None) or _Backoff()
            if not self._limiter.test(limit, key):  # type: ignore[arg-type]
                # The budget is exhausted: lock for one window, doubling with
                # every further exhaustion until a successful sign-in.
                backoff.over_budget = min(backoff.over_budget + 1, 20)
                window = float(limit.get_expiry())  # type: ignore[attr-defined]
                backoff.locked_until = now + min(MAX_BACKOFF_SECONDS, window * 2 ** (backoff.over_budget - 1))
            self._backoff[key] = backoff
        while len(self._backoff) > _MAX_TRACKED_KEYS:
            self._backoff.popitem(last=False)

    def record_success(self, ip: str, username: str) -> None:
        for key, limit in self._keys(ip, username):
            if key.startswith("user:"):
                self._backoff.pop(key, None)
                self._limiter.clear(limit, key)  # type: ignore[arg-type]


def _scale(rate_limit: str, factor: int) -> str:
    amount, _, period = rate_limit.partition("/")
    try:
        return f"{int(amount.strip()) * factor}/{period.strip()}"
    except ValueError:
        return rate_limit
