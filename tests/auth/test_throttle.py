"""Sign-in brute-force protection (roadmap F14)."""

from greenkube.auth.throttle import IP_LIMIT_MULTIPLIER, MAX_BACKOFF_SECONDS, LoginThrottle


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_lockout_doubles_with_each_exhaustion_and_resets_on_success():
    clock = FakeClock()
    throttle = LoginThrottle("2/minute", clock=clock)
    key = throttle._pair_key("1.1.1.1", "admin")

    assert throttle.retry_after("1.1.1.1", "admin") == 0
    throttle.record_failure("1.1.1.1", "admin")
    assert throttle.retry_after("1.1.1.1", "admin") == 0
    throttle.record_failure("1.1.1.1", "admin")
    first = throttle.retry_after("1.1.1.1", "admin")
    assert 55 <= first <= 60

    # The moving window frees up, but the second exhaustion locks twice as long.
    throttle._limiter.clear(throttle._limit, key)
    clock.now += 61
    throttle.record_failure("1.1.1.1", "admin")
    throttle.record_failure("1.1.1.1", "admin")
    assert throttle.retry_after("1.1.1.1", "admin") >= 119

    throttle.record_success("1.1.1.1", "admin")
    assert throttle._backoff.get(key) is None


def test_usernames_are_throttled_case_insensitively_per_client_address():
    throttle = LoginThrottle("1/minute")
    throttle.record_failure("10.0.0.1", "Admin")

    assert throttle.retry_after("10.0.0.1", "admin") > 0
    assert throttle.retry_after("10.0.0.1", "viewer") == 0


def test_an_attacker_cannot_lock_a_user_out_from_another_address():
    """The lockout is per username and client address: guessing from one address never locks the account."""
    throttle = LoginThrottle("3/minute")
    for _ in range(10):
        throttle.record_failure("203.0.113.66", "admin")

    assert throttle.retry_after("203.0.113.66", "admin") > 0
    assert throttle.retry_after("198.51.100.7", "admin") == 0


def test_the_per_address_budget_bounds_password_spraying_without_escalating():
    clock = FakeClock()
    throttle = LoginThrottle("1/minute", clock=clock)
    for index in range(IP_LIMIT_MULTIPLIER):
        throttle.record_failure("203.0.113.66", f"user{index}")

    # A new username from the same address is refused for the rest of the window ...
    wait = throttle.retry_after("203.0.113.66", "someone-else")
    assert 0 < wait <= 60
    # ... but the address is never locked for longer than one window (no exponential lockout
    # that would let one client behind a shared address lock everyone out for 15 minutes).
    assert throttle._backoff.get("ip:203.0.113.66") is None


def test_backoff_is_capped():
    clock = FakeClock()
    throttle = LoginThrottle("1/minute", clock=clock)
    key = throttle._pair_key("10.0.0.1", "admin")
    for _ in range(30):
        throttle._limiter.clear(throttle._limit, key)
        throttle.record_failure("10.0.0.1", "admin")

    assert throttle._backoff[key].locked_until - clock.now <= MAX_BACKOFF_SECONDS
