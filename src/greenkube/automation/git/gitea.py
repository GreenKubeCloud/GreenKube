# src/greenkube/automation/git/gitea.py
"""Gitea/Forgejo REST API adapter (self-hosted, GitHub-inspired)."""

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


class GiteaProvider(GitProvider):
    """Opens pull requests through the Gitea REST API."""

    name = "gitea"

    def __init__(self, token: str, *, api_base_url: Optional[str] = None, **kwargs):
        base = api_base_url or "http://localhost:3000"
        if not base.endswith("/api/v1"):
            base = f"{base.rstrip('/')}/api/v1"
        super().__init__(token, api_base_url=base, **kwargs)

    def _headers(self) -> dict:
        return {"Accept": "application/json", "Authorization": f"token {self.token}"}

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
        try:
            content = base64.b64decode(data.get("content", "")).decode("utf-8")
        except Exception:
            content = data.get("content", "")
        return GitFile(path=path, content=content, sha=data.get("sha"))

    async def list_files(self, repo: GitRepository, ref: str) -> List[str]:
        files: List[str] = []
        page = 1
        first = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/git/trees/{quote(ref, safe='/')}",
            params={"recursive": "true", "per_page": "1000", "page": str(page)},
        )
        if first.status_code == 200:
            while True:
                data = first.json()
                tree = data.get("tree", []) or []
                files.extend(item["path"] for item in tree if item.get("type") == "blob")
                if not data.get("truncated"):
                    return files
                page += 1
                first = await self._request(
                    "GET",
                    f"/repos/{repo.owner}/{repo.name}/git/trees/{quote(ref, safe='/')}",
                    params={"recursive": "true", "per_page": "1000", "page": str(page)},
                )
                if first.status_code != 200:
                    return files

        # Older Gitea versions lack the trees API: walk the contents API instead.
        return await self._list_files_via_contents(repo, ref, "", depth=0)

    async def _list_files_via_contents(self, repo: GitRepository, ref: str, path: str, depth: int) -> List[str]:
        if depth > 4:
            return []
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/contents/{quote_git_path(path)}"
            if path
            else f"/repos/{repo.owner}/{repo.name}/contents",
            params={"ref": ref},
        )
        if response.status_code != 200:
            return []
        data = response.json()
        if not isinstance(data, list):
            return [path] if path else []
        files: List[str] = []
        for item in data:
            item_path = item.get("path") or ""
            if item.get("type") == "file":
                if item_path.endswith((".yaml", ".yml")):
                    files.append(item_path)
            elif item.get("type") == "dir":
                files.extend(await self._list_files_via_contents(repo, ref, item_path, depth + 1))
        return files

    async def create_branch(self, repo: GitRepository, branch: str, from_ref: str) -> None:
        branch = validate_git_ref(branch, field="branch name")
        from_ref = validate_git_ref(from_ref, field="base branch")
        exists = await self._request("GET", f"/repos/{repo.owner}/{repo.name}/branches/{quote(branch, safe='/')}")
        if exists.status_code == 200:
            logger.info("Branch %s already exists in %s; reusing it.", branch, repo.full_name)
            return

        await self._request(
            "POST",
            f"/repos/{repo.owner}/{repo.name}/branches",
            json_body={"new_branch_name": branch, "old_branch_name": from_ref},
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
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "message": message,
            "branch": branch,
            "author": {"name": self.author_name, "email": self.author_email},
        }
        endpoint = f"/repos/{repo.owner}/{repo.name}/contents/{quote_git_path(path)}"
        if sha:
            body["sha"] = sha
            await self._request("PUT", endpoint, json_body=body)
        else:
            await self._request("POST", endpoint, json_body=body)

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
        data = response.json()
        return {
            "number": data.get("number"),
            "html_url": data.get("html_url"),
            "state": data.get("state"),
        }

    async def get_pull_request(self, repo: GitRepository, number: int) -> dict:
        response = await self._request("GET", f"/repos/{repo.owner}/{repo.name}/pulls/{number}")
        if response.status_code == 404:
            raise GitProviderError(f"Pull request {number} not found in {repo.full_name}.")
        data = response.json()
        return {
            "number": data.get("number"),
            "html_url": data.get("html_url"),
            "state": data.get("state"),
            "merged": bool(data.get("merged") or data.get("merged_at")),
        }
