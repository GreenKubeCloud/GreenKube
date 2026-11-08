# tests/gitops/test_repository_identity.py
"""One repository URL normalizer for identity, persistence and API output (roadmap M6)."""

import pytest

from greenkube.gitops.contracts import canonical_repository_identity, public_repository_url, sanitize_repository_url


def test_identity_and_persisted_form_are_the_same_function():
    assert canonical_repository_identity is sanitize_repository_url


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://user:token@GitHub.com/acme/app.git?private_token=x", "https://github.com/acme/app.git"),
        ("git@GitHub.com:acme/app.git", "github.com:acme/app.git"),
        ("unresolved", "unresolved"),
        ("not a repository", "redacted"),
        ("https://github.com/acme/app\n.git", "redacted"),
        ("https://github.com/" + "a" * 600, "redacted"),
    ],
)
def test_public_repository_url_never_leaks_credentials(value, expected):
    assert public_repository_url(value) == expected
