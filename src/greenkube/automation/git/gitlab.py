# src/greenkube/automation/git/gitlab.py
"""GitLab REST API adapter (GitLab.com and self-managed)."""

from __future__ import annotations

import logging
from typing import List, Optional
from urllib.parse import quote

from greenkube.automation.git.base import (
    GitFile,
    GitProvider,
    GitProviderError,
    GitRepository,
    PullRequestInfo,
    normalize_pull_request,
    validate_git_ref,
)

logger = logging.getLogger(__name__)


class GitLabProvider(GitProvider):
    """Opens merge requests through the GitLab REST API."""

    name = "gitlab"

    def __init__(self, token: str, *, api_base_url: Optional[str] = None, **kwargs):
        base = api_base_url or "https://gitlab.com"
        if not base.endswith("/api/v4"):
            base = f"{base.rstrip('/')}/api/v4"
        super().__init__(token, api_base_url=base, **kwargs)

    def _headers(self) -> dict:
        return {"Accept": "application/json", "PRIVATE-TOKEN": self.token}

    def _project(self, repo: GitRepository) -> str:
        return quote(repo.full_name, safe="")

    async def get_default_branch(self, repo: GitRepository) -> str:
        response = await self._request("GET", f"/projects/{self._project(repo)}")
        return response.json().get("default_branch") or repo.default_branch

    async def get_file(self, repo: GitRepository, path: str, ref: str) -> Optional[GitFile]:
        encoded_path = quote(path.lstrip("/"), safe="")
        response = await self._request(
            "GET",
            f"/projects/{self._project(repo)}/repository/files/{encoded_path}",
            params={"ref": ref},
        )
        if response.status_code == 404:
            return None
        data = response.json()
        import base64

        try:
            content = base64.b64decode(data.get("content", "")).decode("utf-8")
        except Exception:
            content = data.get("content", "")
        return GitFile(path=path, content=content, sha=data.get("last_commit_id"))

    async def list_files(self, repo: GitRepository, ref: str) -> List[str]:
        files: List[str] = []
        page = 1
        seen_pages = set()
        while True:
            if page in seen_pages or page > 10000:
                raise GitProviderError("GitLab pagination did not progress; refusing partial results.")
            seen_pages.add(page)
            response = await self._request(
                "GET",
                f"/projects/{self._project(repo)}/repository/tree",
                params={"ref": ref, "recursive": "true", "per_page": "100", "page": str(page)},
            )
            if response.status_code == 404:
                return files
            data = response.json()
            files.extend(item["path"] for item in data if item.get("type") == "blob")
            next_page = response.headers.get("x-next-page")
            if not data or not next_page:
                break
            try:
                page = int(next_page)
            except (TypeError, ValueError):
                raise GitProviderError("GitLab returned an invalid pagination cursor; refusing partial results.")
        return files

    async def create_branch(self, repo: GitRepository, branch: str, from_ref: str) -> None:
        branch = validate_git_ref(branch, field="branch name")
        from_ref = validate_git_ref(from_ref, field="base branch")
        response = await self._request(
            "POST",
            f"/projects/{self._project(repo)}/repository/branches",
            params={"branch": branch, "ref": from_ref},
            ok_statuses=(200, 201, 400),
        )
        if response.status_code == 400 and "already exists" in response.text.lower():
            logger.info("Branch %s already exists in %s; reusing it.", branch, repo.full_name)
            return
        if response.status_code not in (200, 201):
            raise GitProviderError(f"Could not create branch '{branch}': {response.text[:200]}")

    async def update_file(
        self,
        repo: GitRepository,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: Optional[str] = None,
    ) -> None:
        encoded_path = quote(path.lstrip("/"), safe="")
        await self._request(
            "PUT",
            f"/projects/{self._project(repo)}/repository/files/{encoded_path}",
            json_body={
                "branch": branch,
                "content": content,
                "commit_message": message,
                "encoding": "text",
                **({"last_commit_id": sha} if sha else {}),
            },
        )

    async def create_pull_request(
        self,
        repo: GitRepository,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequestInfo:
        head = validate_git_ref(head, field="branch name")
        base = validate_git_ref(base, field="base branch")
        response = await self._request(
            "POST",
            f"/projects/{self._project(repo)}/merge_requests",
            json_body={
                "source_branch": head,
                "target_branch": base,
                "title": title,
                "description": body,
            },
        )
        if response.status_code == 404:
            raise GitProviderError(f"Could not open a merge request in {repo.full_name}: project or branch not found.")
        return normalize_pull_request(response.json(), repo=repo)

    async def get_pull_request(self, repo: GitRepository, number: int) -> PullRequestInfo:
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise GitProviderError("Pull-request number must be a positive integer.")
        response = await self._request("GET", f"/projects/{self._project(repo)}/merge_requests/{number}")
        if response.status_code == 404:
            raise GitProviderError(f"Merge request {number} not found in {repo.full_name}.")
        return normalize_pull_request(response.json(), repo=repo)
