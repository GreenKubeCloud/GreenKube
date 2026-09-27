# src/greenkube/automation/service.py
"""Automation service that turns a recommendation into a pull request.

Orchestration only: source discovery (K8s annotations), file lookup, YAML
patching, branch/PR creation and persistence. Git platform details live in the
provider adapters; manifest details live in the patcher.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Optional

from greenkube.automation.git.base import GitProvider, GitProviderError, GitRepository
from greenkube.automation.manifests.patcher import ManifestNotFoundError, RightsizingPatcher
from greenkube.automation.pr_body import recommendation_title, render_pr_body
from greenkube.automation.source_resolver import (
    AnnotationSourceResolver,
    ManifestSource,
    SourceResolutionError,
    SourceResolver,
)
from greenkube.models.metrics import (
    ApplyPrRequest,
    ApplyPrResponse,
    PullRequestRecord,
    PullRequestStatus,
    RecommendationEventType,
    RecommendationRecord,
    RecommendationStatus,
    RecommendationType,
)

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.storage.base_pull_request_repository import PullRequestRepository
    from greenkube.storage.base_repository import RecommendationRepository

logger = logging.getLogger(__name__)

RIGHTSIZING_TYPES = {RecommendationType.RIGHTSIZING_CPU, RecommendationType.RIGHTSIZING_MEMORY}

#: Maximum number of repository files scanned when the path annotation is absent.
MAX_DISCOVERY_FILES = 300


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", (value or "workload").lower()).strip("-")[:40]


class AutomationService:
    """Coordinates the recommendation → pull-request workflow."""

    def __init__(
        self,
        reco_repo: "RecommendationRepository",
        pr_repo: "PullRequestRepository",
        config: Optional["Config"] = None,
        resolver: Optional[SourceResolver] = None,
        provider: Optional[GitProvider] = None,
        patcher: Optional[RightsizingPatcher] = None,
    ):
        from greenkube.core.config import get_config

        self.reco_repo = reco_repo
        self.pr_repo = pr_repo
        self.config = config if config is not None else get_config()
        self.resolver = resolver if resolver is not None else AnnotationSourceResolver()
        self._provider = provider
        self.patcher = patcher if patcher is not None else RightsizingPatcher()

    def _git_provider(self) -> GitProvider:
        if self._provider is not None:
            return self._provider
        from greenkube.automation.git.factory import get_git_provider

        return get_git_provider(self.config)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def apply_recommendation_pr(
        self,
        rec_id: int,
        request: ApplyPrRequest,
        actor: str = "user",
    ) -> ApplyPrResponse:
        """Opens (or previews) a pull request that applies a recommendation."""
        record = await self.reco_repo.get_recommendation_by_id(rec_id)
        if record is None or record.id is None:
            raise ValueError(f"Recommendation {rec_id} not found.")

        if record.type not in RIGHTSIZING_TYPES:
            return ApplyPrResponse(
                status="error",
                message=(
                    f"Pull-request automation only supports CPU/memory rightsizing. "
                    f"Recommendation {rec_id} is {record.type.value}."
                ),
            )

        try:
            source = await self.resolver.resolve(record)
        except SourceResolutionError as exc:
            return ApplyPrResponse(status="error", message=str(exc))

        try:
            provider = self._git_provider()
        except GitProviderError as exc:
            return ApplyPrResponse(status="error", message=str(exc))

        repo = provider.repository(source.repo_url, source.branch)
        base_branch = request.base_branch or source.branch or self.config.GIT_DEFAULT_BRANCH

        try:
            file_path, file_sha, content = await self._read_manifest(provider, repo, source, base_branch, record)
        except (SourceResolutionError, ManifestNotFoundError, GitProviderError) as exc:
            return ApplyPrResponse(status="error", provider=provider.name, repo=repo.full_name, message=str(exc))

        try:
            patch_result = self.patcher.patch_content(record, content, path=file_path)
        except ManifestNotFoundError as exc:
            return ApplyPrResponse(
                status="error", provider=provider.name, repo=repo.full_name, path=file_path, message=str(exc)
            )

        if request.dry_run:
            return ApplyPrResponse(
                status="dry_run",
                provider=provider.name,
                repo=repo.full_name,
                base_branch=base_branch,
                path=file_path,
                patch=record.patch,
                diff=patch_result.diff,
                message="Dry run: no branch or pull request was created.",
            )

        if not patch_result.changed:
            return ApplyPrResponse(
                status="dry_run",
                provider=provider.name,
                repo=repo.full_name,
                base_branch=base_branch,
                path=file_path,
                patch=record.patch,
                diff="",
                message="The manifest already matches the recommendation; no pull request created.",
            )

        return await self._open_pull_request(
            record=record,
            provider=provider,
            repo=repo,
            source=source,
            base_branch=base_branch,
            file_path=file_path,
            file_sha=file_sha,
            patch_result=patch_result,
            actor=actor,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _read_manifest(
        self,
        provider: GitProvider,
        repo: GitRepository,
        source: ManifestSource,
        base_branch: str,
        record: RecommendationRecord,
    ) -> tuple[str, Optional[str], str]:
        """Returns ``(path, sha, content)`` for the target manifest."""
        if source.path:
            git_file = await provider.get_file(repo, source.path, base_branch)
            if git_file is None:
                raise SourceResolutionError(
                    f"Manifest '{source.path}' was not found in {repo.full_name}@{base_branch}."
                )
            return git_file.path, git_file.sha, git_file.content

        files = await provider.list_files(repo, base_branch)
        candidates = [p for p in files if p.endswith((".yaml", ".yml"))][:MAX_DISCOVERY_FILES]
        contents: dict = {}
        for path in candidates:
            git_file = await provider.get_file(repo, path, base_branch)
            if git_file is None:
                continue
            contents[path] = git_file.content
            found = self.patcher.find_path(record, {path: git_file.content})
            if found:
                return path, git_file.sha, git_file.content

        raise ManifestNotFoundError(
            f"Could not find a {record.owner_kind} manifest named '{record.owner_name}' in {repo.full_name}. "
            "Add the 'greenkube.cloud/git-path' annotation to point at the exact file."
        )

    async def _open_pull_request(
        self,
        *,
        record: RecommendationRecord,
        provider: GitProvider,
        repo: GitRepository,
        source: ManifestSource,
        base_branch: str,
        file_path: str,
        file_sha: Optional[str],
        patch_result,
        actor: str,
    ) -> ApplyPrResponse:
        if record.id is None:  # pragma: no cover - guarded by the caller
            raise ValueError("Cannot open a pull request for an unsaved recommendation.")

        head_branch = f"greenkube/reco-{record.id}-{_slug(record.owner_name or record.pod_name or 'workload')}"
        title = recommendation_title(record)
        body = render_pr_body(record, diff=patch_result.diff, source_ref=source.repo_url)

        attempt = PullRequestRecord(
            recommendation_id=record.id,
            provider=provider.name,
            repo=repo.full_name,
            base_branch=base_branch,
            head_branch=head_branch,
            status=PullRequestStatus.PENDING,
        )
        attempt = await self.pr_repo.save_pull_request(attempt)
        attempt_id = attempt.id
        if attempt_id is None:  # pragma: no cover - storage always assigns an ID
            raise RuntimeError("Pull-request attempt was persisted without an ID.")

        try:
            await provider.create_branch(repo, head_branch, base_branch)
            await provider.update_file(
                repo,
                file_path,
                patch_result.patched,
                f"{title}\n\nApplied by GreenKube recommendation #{record.id}.",
                head_branch,
                sha=file_sha,
            )
            pr = await provider.create_pull_request(
                repo,
                head=head_branch,
                base=base_branch,
                title=title,
                body=body,
            )
        except Exception as exc:
            message = str(exc)
            await self.pr_repo.update_pull_request(attempt_id, {"status": PullRequestStatus.ERROR, "error": message})
            logger.warning("Could not open pull request for recommendation %s: %s", record.id, exc)
            return ApplyPrResponse(
                status="error",
                provider=provider.name,
                repo=repo.full_name,
                base_branch=base_branch,
                head_branch=head_branch,
                path=file_path,
                patch=record.patch,
                diff=patch_result.diff,
                message=message,
                pull_request=await self.pr_repo.get_pull_request_by_id(attempt_id),
            )

        number = pr.get("number") or pr.get("iid")
        url = pr.get("html_url") or pr.get("web_url")
        updated_pr = await self.pr_repo.update_pull_request(
            attempt_id,
            {
                "status": PullRequestStatus.OPEN,
                "pr_number": number,
                "pr_url": url,
                "error": None,
            },
        )

        await self._mark_recommendation_pr_open(record, updated_pr, actor)

        return ApplyPrResponse(
            status="pr_open",
            provider=provider.name,
            repo=repo.full_name,
            base_branch=base_branch,
            head_branch=head_branch,
            path=file_path,
            patch=record.patch,
            diff=patch_result.diff,
            pr_url=url,
            message=f"Pull request #{number} opened.",
            pull_request=updated_pr,
        )

    async def _mark_recommendation_pr_open(
        self,
        record: RecommendationRecord,
        pr: PullRequestRecord,
        actor: str,
    ) -> None:
        from greenkube.core.optimization.lifecycle import RecommendationLifecycle

        if record.id is None or record.status in (RecommendationStatus.APPLIED, RecommendationStatus.PR_OPEN):
            return
        try:
            await RecommendationLifecycle(self.reco_repo).transition(
                record.id,
                RecommendationStatus.PR_OPEN,
                event_type=RecommendationEventType.PR_OPENED,
                actor=actor,
                payload={"pr_url": pr.pr_url, "pr_number": pr.pr_number, "provider": pr.provider},
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Could not transition recommendation %s to pr_open: %s", record.id, exc)

    async def get_pull_requests(self, rec_id: int):
        """Returns the pull-request attempts recorded for a recommendation."""
        return await self.pr_repo.get_pull_requests_for_recommendation(rec_id)

    async def automation_status(self) -> dict:
        """Returns whether the PR bot is ready to open pull requests."""
        provider_name = (self.config.GIT_PROVIDER or "github").lower()
        token_configured = bool(self.config.GIT_TOKEN)
        return {
            "enabled": token_configured,
            "provider": provider_name,
            "api_base_url": self.config.GIT_API_BASE_URL,
            "token_configured": token_configured,
            "default_branch": self.config.GIT_DEFAULT_BRANCH,
        }
