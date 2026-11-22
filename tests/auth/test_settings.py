"""Authentication settings (roadmap F14)."""

from datetime import timedelta

import pytest

from greenkube.auth.settings import AuthSettings
from greenkube.core.config import Config


def test_sessions_are_the_default_mode():
    assert Config.model_fields["API_AUTH_MODE"].default == "session"
    settings = AuthSettings.from_config(Config(_env_file=None))
    assert settings.sessions_enabled
    assert settings.cookie_secure == "auto"
    assert settings.idle_timeout == timedelta(hours=12)
    assert settings.absolute_lifetime == timedelta(days=7)
    assert settings.remember_lifetime == timedelta(days=30)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("auto", "auto"), ("Always", "always"), ("never", "never"), ("true", "always"), ("false", "never")],
)
def test_secure_cookie_values(value, expected):
    assert Config(_env_file=None, SESSION_COOKIE_SECURE=value).SESSION_COOKIE_SECURE == expected


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("SESSION_COOKIE_SECURE", "sometimes"),
        ("AUTH_SESSION_IDLE_TIMEOUT", "12"),
        ("AUTH_SESSION_IDLE_TIMEOUT", "0h"),
        ("AUTH_LOGIN_RATE_LIMIT", "five per minute"),
        ("AUTH_LOGIN_RATE_LIMIT", "5/fortnight"),
        ("AUTH_LOGIN_RATE_LIMIT", "0/minute"),
        ("AUTH_LOGIN_RATE_LIMIT", "5/minute;100/day"),
        ("AUTH_LOGIN_RATE_LIMIT", ""),
    ],
)
def test_invalid_values_fail_fast(field, value):
    with pytest.raises(ValueError):
        Config(_env_file=None, **{field: value})


@pytest.mark.parametrize("value", ["5/minute", "10/hour", " 3/second "])
def test_valid_login_rate_limits(value):
    assert Config(_env_file=None, AUTH_LOGIN_RATE_LIMIT=value).AUTH_LOGIN_RATE_LIMIT == value.strip()
