# src/greenkube/automation/git/github.py
"""GitHub REST API adapter (SaaS and Enterprise Server)."""

from __future__ import annotations

import base64
import logging
from typing import List, Optional
from urllib.parse import quote

from greenkube.automation.git.base import (
    GitFile,
    GitProvider,
    GitProviderError,
    GitRepository,
    quote_git_path,
    validate_git_ref,
)

logger = logging.getLogger(__name__)


class GitHubProvider(GitProvider):
    """Opens pull requests through the GitHub REST API."""

    name = "github"

    def __init__(self, token: str, *, api_base_url: Optional[str] = None, **kwargs):
        super().__init__(token, api_base_url=api_base_url or "https://api.github.com", **kwargs)

    def _headers(self) -> dict:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def get_default_branch(self, repo: GitRepository) -> str:
        response = await self._request("GET", f"/repos/{repo.owner}/{repo.name}")
        return response.json().get("default_branch") or repo.default_branch

    async def get_file(self, repo: GitRepository, path: str, ref: str) -> Optional[GitFile]:
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/contents/{quote_git_path(path)}",
            params={"ref": ref},
        )
        if response.status_code == 404:
            return None
        data = response.json()
        if isinstance(data, list):
            raise GitProviderError(f"Path '{path}' is a directory, not a file.")
        content = data.get("content", "")
        try:
            decoded = base64.b64decode(content).decode("utf-8")
        except Exception:
            decoded = content
        return GitFile(path=path, content=decoded, sha=data.get("sha"))

    async def list_files(self, repo: GitRepository, ref: str) -> List[str]:
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/git/trees/{quote(ref, safe='/')}",
            params={"recursive": "1"},
        )
        if response.status_code == 404:
            return []
        data = response.json()
        tree = data.get("tree", []) or []
        if data.get("truncated"):
            logger.warning(
                "GitHub tree for %s@%s is truncated; manifest discovery may miss files.", repo.full_name, ref
            )
        return [item["path"] for item in tree if item.get("type") == "blob"]

    async def create_branch(self, repo: GitRepository, branch: str, from_ref: str) -> None:
        branch = validate_git_ref(branch, field="branch name")
        from_ref = validate_git_ref(from_ref, field="base branch")
        ref_response = await self._request(
            "GET", f"/repos/{repo.owner}/{repo.name}/git/ref/heads/{quote(from_ref, safe='/')}"
        )
        if ref_response.status_code == 404:
            raise GitProviderError(f"Base branch '{from_ref}' not found in {repo.full_name}.")
        sha = ref_response.json()["object"]["sha"]

        exists = await self._request("GET", f"/repos/{repo.owner}/{repo.name}/git/ref/heads/{quote(branch, safe='/')}")
        if exists.status_code == 200:
            logger.info("Branch %s already exists in %s; reusing it.", branch, repo.full_name)
            return

        await self._request(
            "POST",
            f"/repos/{repo.owner}/{repo.name}/git/refs",
            json_body={"ref": f"refs/heads/{branch}", "sha": sha},
        )

    async def update_file(
        self,
        repo: GitRepository,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: Optional[str] = None,
    ) -> None:
        body = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
            "committer": {"name": self.author_name, "email": self.author_email},
        }
        if sha:
            body["sha"] = sha
        await self._request(
            "PUT",
            f"/repos/{repo.owner}/{repo.name}/contents/{quote_git_path(path)}",
            json_body=body,
        )

    async def create_pull_request(
        self,
        repo: GitRepository,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> dict:
        head = validate_git_ref(head, field="branch name")
        base = validate_git_ref(base, field="base branch")
        response = await self._request(
            "POST",
            f"/repos/{repo.owner}/{repo.name}/pulls",
            json_body={"title": title, "head": head, "base": base, "body": body},
        )
        if response.status_code == 404:
            raise GitProviderError(
                f"Could not open a pull request in {repo.full_name}: repository or branch not found."
            )
        return response.json()

    async def get_pull_request(self, repo: GitRepository, number: int) -> dict:
        response = await self._request("GET", f"/repos/{repo.owner}/{repo.name}/pulls/{number}")
        if response.status_code == 404:
            raise GitProviderError(f"Pull request {number} not found in {repo.full_name}.")
        return response.json()
