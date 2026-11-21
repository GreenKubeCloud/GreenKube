"""Failed Bearer authentications are throttled per client address (roadmap A.9).

An operator-chosen bootstrap key ``GREENKUBE_API_KEY`` must not be guessable
online at the global ``API_RATE_LIMIT``: failed ``Authorization: Bearer``
attempts (and failed API-key browser cookies) feed a non-escalating per-IP
budget, then get ``429`` with ``Retry-After``. Prometheus scrapes use their own
budget, so unrelated failures never stop a correct scrape.
"""

from __future__ import annotations

import time

from greenkube.auth.metrics import AUTH_FAILURES, FAILURE_REASONS
from greenkube.auth.throttle import IP_LIMIT_MULTIPLIER
from tests.api.auth_support import ORIGIN, auth_harness  # pyrefly: ignore[missing-import]

LEGACY_KEY = "operator-chosen-bootstrap"
SCRAPE_TOKEN = "scrape-token-abcdef0123456789"
PROXY = ("10.0.0.7", 40000)
ATTACKER = "203.0.113.66"
OPERATOR = "198.51.100.7"
# AUTH_LOGIN_RATE_LIMIT=1/minute: the per-IP Bearer budget is IP_LIMIT_MULTIPLIER failures a minute.
BUDGET = IP_LIMIT_MULTIPLIER


def _env(**extra: str) -> dict[str, str]:
    env = {
        "API_AUTH_MODE": "api_key",
        "GREENKUBE_API_KEY": LEGACY_KEY,
        "AUTH_LOGIN_RATE_LIMIT": "1/minute",
        "TRUSTED_PROXY_IPS": "10.0.0.0/8",
    }
    env.update(extra)
    return env


def _get(h, path: str, token: str, ip: str):
    return h.client.get(path, headers={"Authorization": f"Bearer {token}", "X-Forwarded-For": ip})


def _guess(h, ip: str, count: int, path: str = "/api/v1/version") -> list[int]:
    return [_get(h, path, f"wrong-{index}", ip).status_code for index in range(count)]


def test_repeated_wrong_bearer_keys_get_429_with_retry_after(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, **_env()) as h:
        statuses = _guess(h, ATTACKER, BUDGET)
        blocked = _get(h, "/api/v1/version", "wrong-again", ATTACKER)
        # The budget is per address: the right key from the throttled address waits too.
        same_address = _get(h, "/api/v1/version", LEGACY_KEY, ATTACKER)

    assert statuses == [401] * BUDGET
    assert blocked.status_code == 429, blocked.text
    assert blocked.json()["detail"]["code"] == "TOO_MANY_ATTEMPTS"
    assert 1 <= int(blocked.headers["retry-after"]) <= 60
    assert same_address.status_code == 429


def test_a_correct_key_still_works_from_another_address(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, **_env()) as h:
        _guess(h, ATTACKER, BUDGET)
        blocked = _get(h, "/api/v1/version", "wrong-again", ATTACKER)
        operator = _get(h, "/api/v1/version", LEGACY_KEY, OPERATOR)
        # Successes never consume the budget.
        repeated = [_get(h, "/api/v1/version", LEGACY_KEY, OPERATOR).status_code for _ in range(BUDGET * 2)]

    assert blocked.status_code == 429
    assert operator.status_code == 200, operator.text
    assert repeated == [200] * (BUDGET * 2)


def test_prometheus_scraping_with_the_correct_token_keeps_working(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, **_env(GREENKUBE_METRICS_TOKEN=SCRAPE_TOKEN)) as h:
        # Unrelated failures from the scraper's own address, on the API routes ...
        _guess(h, OPERATOR, BUDGET)
        api_blocked = _get(h, "/api/v1/version", "wrong-again", OPERATOR)
        # ... and from another address on the scrape endpoint itself.
        _guess(h, ATTACKER, BUDGET, path="/prometheus/metrics")
        scrape_blocked = _get(h, "/prometheus/metrics", "wrong-again", ATTACKER)
        scrape = _get(h, "/prometheus/metrics", SCRAPE_TOKEN, OPERATOR)

    assert api_blocked.status_code == 429
    assert scrape_blocked.status_code == 429
    assert scrape.status_code == 200, scrape.text


def test_the_per_address_budget_resets_after_the_window(tmp_path, monkeypatch):
    real_time = time.time
    offset = {"seconds": 0.0}
    monkeypatch.setattr(time, "time", lambda: real_time() + offset["seconds"])
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, **_env()) as h:
        _guess(h, ATTACKER, BUDGET)
        blocked = _get(h, "/api/v1/version", "wrong-again", ATTACKER)
        offset["seconds"] = 61.0
        after_window = _get(h, "/api/v1/version", "wrong-after-window", ATTACKER)
        correct = _get(h, "/api/v1/version", LEGACY_KEY, ATTACKER)

    assert blocked.status_code == 429
    # Non-escalating: one window later the address may try again (no exponential lockout).
    assert after_window.status_code == 401
    assert correct.status_code == 200


def test_failed_api_key_cookies_feed_the_same_budget(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, **_env()) as h:
        statuses = []
        for index in range(BUDGET):
            h.client.cookies.set("greenkube_session", f"forged-{index}")
            statuses.append(h.client.get("/api/v1/version", headers={"X-Forwarded-For": ATTACKER}).status_code)
        h.client.cookies.set("greenkube_session", "forged-again")
        blocked = h.client.get("/api/v1/version", headers={"X-Forwarded-For": ATTACKER})
        h.client.cookies.clear()
        operator = _get(h, "/api/v1/version", LEGACY_KEY, OPERATOR)

    assert statuses == [401] * BUDGET
    assert blocked.status_code == 429
    assert "retry-after" in blocked.headers
    assert operator.status_code == 200


def test_an_open_development_instance_is_never_throttled(tmp_path, monkeypatch):
    with auth_harness(
        tmp_path,
        monkeypatch,
        users=[],
        client_address=PROXY,
        API_AUTH_MODE="api_key",
        GREENKUBE_API_KEY="",
        AUTH_LOGIN_RATE_LIMIT="1/minute",
        TRUSTED_PROXY_IPS="10.0.0.0/8",
    ) as h:
        statuses = _guess(h, ATTACKER, BUDGET * 2)

    assert statuses == [200] * (BUDGET * 2)


def _cleared(response) -> bool:
    header = response.headers.get("set-cookie", "")
    return header.startswith("greenkube_session=") and "Max-Age=0" in header


def test_a_stale_cookie_on_an_open_development_instance_is_never_counted_and_is_cleared(tmp_path, monkeypatch):
    """Open access serves the request: a leftover cookie is not a failed authentication."""
    with auth_harness(
        tmp_path,
        monkeypatch,
        users=[],
        client_address=PROXY,
        API_AUTH_MODE="api_key",
        GREENKUBE_API_KEY="",
        AUTH_LOGIN_RATE_LIMIT="1/minute",
        TRUSTED_PROXY_IPS="10.0.0.0/8",
    ) as h:
        responses = []
        for _ in range(BUDGET + 2):
            h.client.cookies.set("greenkube_session", "v1.legacy.123.stale")
            responses.append(h.client.get("/api/v1/version", headers={"X-Forwarded-For": ATTACKER}))

    assert [r.status_code for r in responses] == [200] * (BUDGET + 2)
    assert all(_cleared(r) for r in responses)


def test_a_stale_cookie_on_public_auth_paths_is_ignored_and_cleared(tmp_path, monkeypatch):
    """A browser with a stale cookie must still be able to sign in again with the right key."""
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, **_env()) as h:
        providers = []
        for _ in range(BUDGET + 1):
            h.client.cookies.set("greenkube_session", "stale")
            providers.append(h.client.get("/api/v1/auth/providers", headers={"X-Forwarded-For": OPERATOR}))
        h.client.cookies.set("greenkube_session", "stale")
        login = h.client.post(
            "/api/v1/auth/api-key-login",
            json={"key": LEGACY_KEY},
            headers={"Origin": ORIGIN, "X-Forwarded-For": OPERATOR, "X-Forwarded-Proto": "https"},
        )
        h.client.cookies.clear()
        bearer = _get(h, "/api/v1/version", LEGACY_KEY, OPERATOR)

    assert [r.status_code for r in providers] == [200] * (BUDGET + 1)
    assert all(_cleared(r) for r in providers)
    assert login.status_code == 200, login.text
    # The stale cookies never consumed the address's failed-key budget.
    assert bearer.status_code == 200


def test_the_key_throttle_has_its_own_failure_reason(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, **_env()) as h:
        _guess(h, ATTACKER, BUDGET)
        before = AUTH_FAILURES.labels(reason="key_rate_limited")._value.get()
        blocked = _get(h, "/api/v1/version", "wrong-again", ATTACKER)

    assert blocked.status_code == 429
    assert AUTH_FAILURES.labels(reason="key_rate_limited")._value.get() == before + 1
    assert "key_rate_limited" in FAILURE_REASONS
