"""Allowlist suggestions and provider inference (roadmap F2).

``GET /api/v1/automation/repositories/suggestions`` lists the repositories found
by GitOps discovery with the provider inferred for their host (``github.com``,
``gitlab.com``, or a self-hosted GitLab/Gitea identified by an API probe) and
whether automation may open pull requests against them.

``POST /api/v1/automation/repositories/allowlist`` (``admin`` scope) allows some of
them: the database-backed allowlist is merged into the provider allowlists by the
controller's readiness reconciler within a minute. The response also carries the
equivalent ``automation.repositoryAllowlist`` values snippet for installs managed
through GitOps, where the values file is the source of truth.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from greenkube.automation.readiness import repository_ref
from greenkube.storage.automation_capability_repository import SCOPE_REPOSITORY, AutomationCapabilityRepository

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_REPOSITORIES_PER_REQUEST = 100


class RepositorySuggestion(BaseModel):
    repo_url: str
    provider_type: Optional[str] = None
    api_base_url: Optional[str] = None
    inferred_from: str
    allowlisted: bool


class RepositorySuggestions(BaseModel):
    suggestions: List[RepositorySuggestion] = Field(default_factory=list)


class AllowlistRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_urls: List[str] = Field(min_length=1, max_length=MAX_REPOSITORIES_PER_REQUEST)


class AllowlistResponse(BaseModel):
    added: int
    allowlisted: List[str]
    values_snippet: str


def _repository() -> AutomationCapabilityRepository:
    from greenkube.core.db import get_db_manager

    return AutomationCapabilityRepository(get_db_manager())


async def _suggestions() -> List[RepositorySuggestion]:
    rows = await _repository().list_capabilities(SCOPE_REPOSITORY)
    return [
        RepositorySuggestion(
            repo_url=row.subject,
            provider_type=row.payload.get("provider_type"),
            api_base_url=row.payload.get("api_base_url"),
            inferred_from=str(row.payload.get("inferred_from") or "unknown"),
            allowlisted=bool(row.payload.get("allowlisted")),
        )
        for row in rows
    ]


@router.get("/automation/repositories/suggestions", response_model=RepositorySuggestions)
async def repository_suggestions() -> RepositorySuggestions:
    """Discovered repositories with their inferred provider and allowlist state."""
    try:
        return RepositorySuggestions(suggestions=await _suggestions())
    except Exception as error:
        logger.warning("Repository suggestions are unavailable (%s).", type(error).__name__)
        raise HTTPException(status_code=503, detail="Repository suggestions are not available yet.") from None


@router.post("/automation/repositories/allowlist", response_model=AllowlistResponse)
async def allow_repositories(body: AllowlistRequest, request: Request) -> AllowlistResponse:
    """Allow discovered repositories (``admin``); the controller applies it within a minute."""
    known = {suggestion.repo_url: suggestion for suggestion in await _suggestions()}
    entries = []
    unknown = []
    for url in dict.fromkeys(item.strip() for item in body.repo_urls):
        ref = repository_ref(url)
        if url not in known or ref is None:
            unknown.append(url)
            continue
        entries.append({"repo_url": url, "host": ref.host, "repository": ref.repository})
    if unknown:
        # Fail closed: only repositories discovery found can be allowed here.
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_repository",
                "message": "Only repositories listed by the suggestions can be allowed.",
                "repo_urls": unknown,
            },
        )
    principal = getattr(request.state, "principal", None)
    actor = str(getattr(principal, "actor", None) or getattr(principal, "name", None) or "api")
    added = await _repository().add_allowed_repositories(entries, actor=actor)
    names = sorted({entry["repository"] for entry in entries})
    snippet = "automation:\n  repositoryAllowlist:\n" + "".join(f"    - {name}\n" for name in names)
    return AllowlistResponse(added=added, allowlisted=[entry["repo_url"] for entry in entries], values_snippet=snippet)
