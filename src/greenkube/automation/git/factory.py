# src/greenkube/automation/git/factory.py
"""Selects and configures the Git provider adapter from configuration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from greenkube.automation.git.base import GitProvider, GitProviderError
from greenkube.automation.git.gitea import GiteaProvider
from greenkube.automation.git.github import GitHubProvider
from greenkube.automation.git.gitlab import GitLabProvider

if TYPE_CHECKING:
    from greenkube.core.config import Config

logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = ("github", "gitlab", "gitea")


def get_git_provider(config: Optional["Config"] = None) -> GitProvider:
    """Builds the configured Git provider adapter.

    Raises:
        GitProviderError: When the provider is unknown or the token is missing.
    """
    from greenkube.core.config import get_config

    cfg = config if config is not None else get_config()
    provider = (cfg.GIT_PROVIDER or "github").strip().lower()
    token = cfg.GIT_TOKEN

    if provider not in SUPPORTED_PROVIDERS:
        raise GitProviderError(
            f"Unsupported GIT_PROVIDER '{provider}'. Supported providers: {', '.join(SUPPORTED_PROVIDERS)}."
        )
    if not token:
        raise GitProviderError(
            "GIT_TOKEN is not configured. Set secrets.gitToken (Helm) or the GIT_TOKEN environment variable "
            "to enable pull-request automation."
        )

    kwargs = {
        "api_base_url": cfg.GIT_API_BASE_URL or None,
        "author_name": cfg.GIT_COMMIT_AUTHOR_NAME,
        "author_email": cfg.GIT_COMMIT_AUTHOR_EMAIL,
    }
    if provider == "github":
        return GitHubProvider(token, **kwargs)
    if provider == "gitlab":
        return GitLabProvider(token, **kwargs)
    return GiteaProvider(token, **kwargs)
