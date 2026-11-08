"""Sign-in brute-force protection (roadmap F14)."""

from greenkube.auth.throttle import MAX_BACKOFF_SECONDS, LoginThrottle


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_lockout_doubles_with_each_exhaustion_and_resets_on_success():
    clock = FakeClock()
    throttle = LoginThrottle("2/minute", clock=clock)

    assert throttle.retry_after("1.1.1.1", "admin") == 0
    throttle.record_failure("1.1.1.1", "admin")
    assert throttle.retry_after("1.1.1.1", "admin") == 0
    throttle.record_failure("1.1.1.1", "admin")
    first = throttle.retry_after("1.1.1.1", "admin")
    assert 55 <= first <= 60

    # The moving window frees up, but the second exhaustion locks twice as long.
    throttle._limiter.clear(throttle._limit, "user:admin")
    clock.now += 61
    throttle.record_failure("1.1.1.1", "admin")
    throttle.record_failure("1.1.1.1", "admin")
    assert throttle.retry_after("1.1.1.1", "admin") >= 119

    throttle.record_success("1.1.1.1", "admin")
    assert throttle._backoff.get("user:admin") is None


def test_usernames_are_throttled_case_insensitively_and_separately():
    throttle = LoginThrottle("1/minute")
    throttle.record_failure("10.0.0.1", "Admin")

    assert throttle.retry_after("10.0.0.2", "admin") > 0
    assert throttle.retry_after("10.0.0.2", "viewer") == 0


def test_backoff_is_capped():
    clock = FakeClock()
    throttle = LoginThrottle("1/minute", clock=clock)
    for _ in range(30):
        throttle._limiter.clear(throttle._limit, "user:admin")
        throttle.record_failure("10.0.0.1", "admin")

    assert throttle._backoff["user:admin"].locked_until - clock.now <= MAX_BACKOFF_SECONDS
