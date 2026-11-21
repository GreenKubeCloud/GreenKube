# tests/gitops/test_repository_identity.py
"""One repository URL normalizer for identity, persistence and API output (roadmap M6)."""

import pytest

from greenkube.gitops.contracts import (
    canonical_repository_identity,
    public_repository_url,
    repository_identity,
    same_repository,
    sanitize_repository_url,
)


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


_GITOPS_HTTPS = "https://gitlab.example.com/ops/gitops.git"


@pytest.mark.parametrize(
    "spelling",
    [
        "https://gitlab.example.com/ops/gitops",
        "https://gitlab.example.com/ops/gitops/",
        "https://gitlab.example.com/ops/gitops.git/",
        "https://GitLab.Example.com/ops/gitops.git",
        "git@gitlab.example.com:ops/gitops.git",
        "git@gitlab.example.com:ops/gitops",
        "ssh://git@gitlab.example.com/ops/gitops.git",
        "ssh://git@gitlab.example.com:2222/ops/gitops.git",
        "https://user:token@gitlab.example.com/ops/gitops.git",
    ],
)
def test_one_repository_identity_for_every_spelling_of_a_remote(spelling):
    assert same_repository(spelling, _GITOPS_HTTPS)
    assert repository_identity(spelling) == "gitlab.example.com/ops/gitops"


@pytest.mark.parametrize(
    "other",
    [
        "https://gitlab.example.com/ops/gitops-config.git",
        "https://gitlab.example.com/other/gitops.git",
        "https://github.com/ops/gitops.git",
        "registry-1.docker.io/bitnamicharts",
        "unresolved",
        None,
    ],
)
def test_distinct_repositories_keep_distinct_identities(other):
    assert not same_repository(other, _GITOPS_HTTPS)


def test_unresolved_repositories_are_never_the_same_repository():
    assert not same_repository("unresolved", "unresolved")
