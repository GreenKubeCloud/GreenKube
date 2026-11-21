"""Brute-force protection for the sign-in endpoint (roadmap F14).

Failed sign-ins are counted with the ``limits`` library already used by the
API rate limiter, under two keys:

* **username and client IP** (``AUTH_LOGIN_RATE_LIMIT``, default 5/minute):
  past the limit, that pair is locked for one rate-limit window, doubling with
  every further exhaustion (1, 2, 4 ... minutes for ``5/minute``, up to 15
  minutes); a successful sign-in resets it. The lockout never covers the
  username alone: an attacker guessing from one address cannot lock the real
  user (who signs in from another address) out of the account.
* **client IP** (4x the limit): a plain moving window, with no exponential
  lockout. It bounds password spraying across usernames from one address, and
  a client behind a shared address (an ingress without ``TRUSTED_PROXY_IPS``,
  a NAT) can deny others for one window at most.

The client IP is :func:`greenkube.auth.http.client_ip` (the rightmost
``X-Forwarded-For`` hop that is not a trusted proxy). State is per API
process: with several replicas the effective limit is multiplied by the
replica count, which keeps the bound small while avoiding a database write per
attempt.

:class:`CredentialThrottle` applies the same per-IP budget (4x the sign-in
limit, no exponential lockout) to failed machine credentials: ``Authorization:
Bearer`` keys and API-key browser cookies (roadmap A.9).
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

    @staticmethod
    def _pair_key(ip: str, username: str) -> str:
        return f"user:{username.strip().lower()}@{ip}"

    def _keys(self, ip: str, username: str) -> list[tuple[str, object, bool]]:
        """``(key, limit, escalates)``: only the username+IP pair has an exponential lockout."""
        return [(self._pair_key(ip, username), self._limit, True), (f"ip:{ip}", self._ip_limit, False)]

    def retry_after(self, ip: str, username: str) -> int:
        """Seconds until a new attempt is allowed, or 0 when it is allowed now."""
        now = self._clock()
        wait = 0.0
        for key, limit, _escalates in self._keys(ip, username):
            backoff = self._backoff.get(key)
            if backoff is not None and backoff.locked_until > now:
                wait = max(wait, backoff.locked_until - now)
            if not self._limiter.test(limit, key):  # type: ignore[arg-type]
                stats = self._limiter.get_window_stats(limit, key)  # type: ignore[arg-type]
                wait = max(wait, stats.reset_time - time.time())
        return max(1, int(wait + 0.999)) if wait > 0 else 0

    def record_failure(self, ip: str, username: str) -> None:
        now = self._clock()
        for key, limit, escalates in self._keys(ip, username):
            self._limiter.hit(limit, key)  # type: ignore[arg-type]
            if not escalates:
                continue
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
        key = self._pair_key(ip, username)
        self._backoff.pop(key, None)
        self._limiter.clear(self._limit, key)  # type: ignore[arg-type]


class CredentialThrottle:
    """Non-escalating per-IP budget for failed ``Authorization: Bearer`` and API-key cookie attempts (A.9).

    Failures are counted per client address and per *bucket* (``api`` for the
    API routes, ``scrape`` for ``/prometheus/metrics``), in a moving window of
    ``IP_LIMIT_MULTIPLIER`` times ``AUTH_LOGIN_RATE_LIMIT`` (20 a minute by
    default). Once the budget is spent, every credential from that address and
    bucket is refused until the window frees up, the correct one included:
    checking it first would turn the ``429`` into an oracle that still answers
    at the global rate. Successes never consume the budget and never reset it
    (a valid low-privilege key must not buy more guesses at the admin key).
    """

    def __init__(self, rate_limit: str = "5/minute") -> None:
        self._limit = parse_rate_limit(_scale(rate_limit, IP_LIMIT_MULTIPLIER))
        self._limiter = MovingWindowRateLimiter(MemoryStorage())

    @staticmethod
    def _key(ip: str, bucket: str) -> str:
        return f"{bucket}@{ip}"

    def retry_after(self, ip: str, bucket: str) -> int:
        """Seconds until a new attempt from ``ip`` is allowed in ``bucket``, or 0 when it is allowed now."""
        key = self._key(ip, bucket)
        if self._limiter.test(self._limit, key):  # type: ignore[arg-type]
            return 0
        stats = self._limiter.get_window_stats(self._limit, key)  # type: ignore[arg-type]
        return max(1, int(stats.reset_time - time.time() + 0.999))

    def record_failure(self, ip: str, bucket: str) -> None:
        """Count one failed authentication from ``ip`` in ``bucket``."""
        self._limiter.hit(self._limit, self._key(ip, bucket))  # type: ignore[arg-type]


def _scale(rate_limit: str, factor: int) -> str:
    amount, _, period = rate_limit.partition("/")
    try:
        return f"{int(amount.strip()) * factor}/{period.strip()}"
    except ValueError:
        return rate_limit
