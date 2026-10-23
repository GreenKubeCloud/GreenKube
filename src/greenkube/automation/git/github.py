# src/greenkube/automation/git/github.py
"""GitHub REST API adapter (SaaS and Enterprise Server)."""

from __future__ import annotations

import base64
import logging
from typing import List, Optional
from urllib.parse import quote, urlsplit

from greenkube.automation.git.base import (
    GitCommitRequest,
    GitCommitResult,
    GitConflictError,
    GitFile,
    GitProvider,
    GitProviderCapability,
    GitProviderError,
    GitRepository,
    PullRequestInfo,
    PullRequestOptions,
    _git_response_object,
    _validate_full_commit_sha,
    normalize_open_pull_request_branch_match,
    normalize_open_pull_request_match,
    normalize_pull_request,
    parse_pull_request_list,
    pull_request_next_page,
    quote_git_path,
    validate_git_ref,
)

logger = logging.getLogger(__name__)


class GitHubProvider(GitProvider):
    """Opens pull requests through the GitHub REST API."""

    name = "github"
    capabilities = frozenset(
        {
            GitProviderCapability.MULTI_FILE_COMMIT,
            GitProviderCapability.FIND_PULL_REQUEST,
            GitProviderCapability.GET_REF_REVISION,
            GitProviderCapability.REPOSITORY_ARCHIVE,
            GitProviderCapability.COMPLETE_FILE_TREE,
            GitProviderCapability.DRAFT_PULL_REQUEST,
            GitProviderCapability.LABELS,
            GitProviderCapability.REVIEWERS,
            GitProviderCapability.TEAM_REVIEWERS,
            GitProviderCapability.FIND_OPEN_PULL_REQUEST_FOR_BRANCH,
            GitProviderCapability.DELETE_PLAN_BRANCH,
        }
    )

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
        return GitFile(path=path, content=decoded, sha=data.get("sha"), blob_sha=data.get("sha"))

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
            raise GitProviderError(f"GitHub tree for {repo.full_name}@{ref} was truncated; refusing partial results.")
        return [item["path"] for item in tree if item.get("type") == "blob"]

    async def get_archive(self, repo: GitRepository, revision: str, *, max_bytes: int) -> bytes:
        revision = self._validate_archive_request(repo, revision, max_bytes)
        parsed_base = urlsplit(self.api_base_url)
        redirect_hosts = (
            frozenset({"codeload.github.com"})
            if (
                parsed_base.scheme == "https"
                and parsed_base.hostname is not None
                and parsed_base.hostname.lower() == "api.github.com"
            )
            else frozenset()
        )
        return await self._request_archive(
            f"/repos/{quote(repo.owner, safe='/')}/{quote(repo.name, safe='')}/tarball/{quote(revision, safe='')}",
            max_bytes=max_bytes,
            redirect_hosts=redirect_hosts,
        )

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

    async def _branch_head_sha(self, repo: GitRepository, branch: str) -> str:
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/git/ref/heads/{quote(branch, safe='/')}",
            conflict_statuses=(404,),
        )
        data = _git_response_object(response, provider=self.name, ref=branch)
        git_object = data.get("object")
        head_sha = git_object.get("sha") if isinstance(git_object, dict) else None
        if not isinstance(head_sha, str) or not head_sha:
            raise GitProviderError(f"GitHub returned an invalid branch revision for {repo.full_name}/{branch}.")
        return head_sha

    async def _resolve_tag_object(self, repo: GitRepository, git_object: object, ref: str, seen: set[str]) -> str:
        if not isinstance(git_object, dict):
            raise GitProviderError(f"GitHub returned an invalid tag response for ref '{ref}'.")
        object_type = git_object.get("type")
        object_sha = git_object.get("sha")
        if object_type == "commit":
            return _validate_full_commit_sha(object_sha, provider=self.name, ref=ref)
        if object_type != "tag":
            raise GitProviderError(f"GitHub returned an invalid tag target for ref '{ref}'.")

        tag_object_sha = _validate_full_commit_sha(object_sha, provider=self.name, ref=ref)
        if tag_object_sha in seen or len(seen) >= 32:
            raise GitProviderError(f"GitHub returned an invalid annotated tag chain for ref '{ref}'.")
        seen.add(tag_object_sha)
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/git/tags/{quote(tag_object_sha, safe='')}",
        )
        if response.status_code == 404:
            raise GitProviderError(f"GitHub returned a missing annotated tag object for ref '{ref}'.")
        tag_data = _git_response_object(response, provider=self.name, ref=ref)
        return await self._resolve_tag_object(repo, tag_data.get("object"), ref, seen)

    async def _tag_head_sha(self, repo: GitRepository, tag: str) -> str:
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/git/ref/tags/{quote(tag, safe='/')}",
            conflict_statuses=(404,),
        )
        data = _git_response_object(response, provider=self.name, ref=tag)
        return await self._resolve_tag_object(repo, data.get("object"), tag, set())

    async def get_ref_revision(self, repo: GitRepository, ref: str) -> str:
        ref = self._validate_ref_request(repo, ref)
        try:
            revision = await self._branch_head_sha(repo, ref)
        except GitConflictError:
            try:
                revision = await self._tag_head_sha(repo, ref)
            except GitConflictError as exc:
                raise GitProviderError(f"GitHub ref '{ref}' not found in {repo.full_name}.") from exc
        return _validate_full_commit_sha(revision, provider=self.name, ref=ref)

    async def create_commit(self, request: GitCommitRequest) -> GitCommitResult:
        self._validate_commit_request(request)
        repo = request.repo
        branch = validate_git_ref(request.branch, field="branch name")
        base_revision = request.base_revision
        branch_ref_path = f"/repos/{repo.owner}/{repo.name}/git/refs/heads/{quote(branch, safe='/')}"

        current_head = await self._branch_head_sha(repo, branch)
        if current_head != base_revision:
            raise GitConflictError(f"Branch '{branch}' moved from base revision {base_revision} to {current_head}.")

        base_commit = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/git/commits/{quote(base_revision, safe='')}",
            conflict_statuses=(404,),
        )
        commit_data = base_commit.json()
        base_tree_sha = commit_data.get("tree", {}).get("sha") if isinstance(commit_data, dict) else None
        if not isinstance(base_tree_sha, str) or not base_tree_sha:
            raise GitProviderError(f"GitHub returned an invalid base tree for {repo.full_name}@{base_revision}.")

        await self._validate_commit_edits(request)
        tree_entries = []
        for edit in request.files:
            entry: dict[str, str | None] = {"path": edit.path, "mode": "100644", "type": "blob"}
            if edit.action == "delete":
                entry["sha"] = None
            else:
                if edit.content is None:
                    raise GitProviderError(f"{edit.action.capitalize()} edits require file content.")
                entry["content"] = edit.content
            tree_entries.append(entry)

        tree_response = await self._request(
            "POST",
            f"/repos/{repo.owner}/{repo.name}/git/trees",
            json_body={"base_tree": base_tree_sha, "tree": tree_entries},
            retryable=False,
        )
        tree_data = tree_response.json()
        new_tree_sha = tree_data.get("sha") if isinstance(tree_data, dict) else None
        if not isinstance(new_tree_sha, str) or not new_tree_sha:
            raise GitProviderError("GitHub returned an invalid tree after creating the atomic commit plan.")

        author = {"name": request.author_name, "email": request.author_email}
        commit_response = await self._request(
            "POST",
            f"/repos/{repo.owner}/{repo.name}/git/commits",
            json_body={
                "message": request.message,
                "tree": new_tree_sha,
                "parents": [base_revision],
                "author": author,
                "committer": author,
            },
            retryable=False,
        )
        new_commit = commit_response.json()
        commit_sha = new_commit.get("sha") if isinstance(new_commit, dict) else None
        if not isinstance(commit_sha, str) or not commit_sha:
            raise GitProviderError("GitHub returned an invalid commit SHA.")

        current_head = await self._branch_head_sha(repo, branch)
        if current_head != base_revision:
            raise GitConflictError(f"Branch '{branch}' moved from base revision {base_revision} to {current_head}.")
        await self._request(
            "PATCH",
            branch_ref_path,
            json_body={"sha": commit_sha, "force": False},
            conflict_statuses=(404, 409, 422),
            retryable=False,
        )
        return GitCommitResult(
            commit_sha=commit_sha,
            branch=branch,
            files_changed=[edit.path for edit in request.files],
        )

    async def create_pull_request(
        self,
        repo: GitRepository,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
        options: PullRequestOptions | None = None,
    ) -> PullRequestInfo:
        selected_options = self.validate_pull_request_options(options)
        if repo.provider != self.name:
            raise GitProviderError(f"Pull-request creation targets provider '{repo.provider}', not '{self.name}'.")
        head = validate_git_ref(head, field="branch name")
        base = validate_git_ref(base, field="base branch")
        response = await self._request(
            "POST",
            f"/repos/{repo.owner}/{repo.name}/pulls",
            json_body={
                "title": title,
                "head": head,
                "base": base,
                "body": body,
                "draft": selected_options.draft,
            },
        )
        if response.status_code == 404:
            raise GitProviderError(
                f"Could not open a pull request in {repo.full_name}: repository or branch not found."
            )
        pull_request = normalize_pull_request(response.json(), repo=repo)
        await self.ensure_pull_request_options(repo, pull_request, selected_options)
        return pull_request

    async def ensure_pull_request_options(
        self,
        repo: GitRepository,
        pull_request: PullRequestInfo,
        options: PullRequestOptions | None = None,
    ) -> None:
        selected_options = self.validate_pull_request_options(options)
        await super().ensure_pull_request_options(repo, pull_request, selected_options)
        if selected_options.draft and pull_request.raw.get("draft") is not True:
            raise GitProviderError(
                "GitHub did not return the requested draft state; refusing to treat this pull request as configured."
            )
        if not selected_options.draft and pull_request.raw.get("draft") is True:
            response = await self._request(
                "POST",
                f"/repos/{repo.owner}/{repo.name}/pulls/{pull_request.number}/ready_for_review",
            )
            if response.status_code == 404:
                raise GitProviderError("GitHub could not mark the existing pull request ready for review.")
        if selected_options.labels:
            response = await self._request(
                "POST",
                f"/repos/{repo.owner}/{repo.name}/issues/{pull_request.number}/labels",
                json_body={"labels": list(selected_options.labels)},
            )
            if response.status_code == 404:
                raise GitProviderError("GitHub could not apply configured labels to the pull request.")
        if not selected_options.reviewers and not selected_options.team_reviewers:
            return

        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/pulls/{pull_request.number}/requested_reviewers",
        )
        if response.status_code == 404:
            raise GitProviderError("GitHub could not read requested reviewers for the pull request.")
        data = _git_response_object(
            response,
            provider=self.name,
            ref=f"pull request {pull_request.number} reviewers",
        )
        users = data.get("users", [])
        teams = data.get("teams", [])
        if not isinstance(users, list) or not isinstance(teams, list):
            raise GitProviderError("GitHub returned an invalid requested-reviewers response.")
        existing_users = {
            item["login"].casefold() for item in users if isinstance(item, dict) and isinstance(item.get("login"), str)
        }
        existing_teams = {
            item["slug"].casefold() for item in teams if isinstance(item, dict) and isinstance(item.get("slug"), str)
        }
        if len(existing_users) != len(users) or len(existing_teams) != len(teams):
            raise GitProviderError("GitHub returned an invalid requested-reviewers response.")

        reviewers = [name for name in selected_options.reviewers if name.casefold() not in existing_users]
        team_reviewers = [name for name in selected_options.team_reviewers if name.casefold() not in existing_teams]
        if reviewers or team_reviewers:
            response = await self._request(
                "POST",
                f"/repos/{repo.owner}/{repo.name}/pulls/{pull_request.number}/requested_reviewers",
                json_body={"reviewers": reviewers, "team_reviewers": team_reviewers},
            )
            if response.status_code == 404:
                raise GitProviderError("GitHub could not request configured pull-request reviewers.")

    async def get_pull_request(self, repo: GitRepository, number: int) -> PullRequestInfo:
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise GitProviderError("Pull-request number must be a positive integer.")
        response = await self._request("GET", f"/repos/{repo.owner}/{repo.name}/pulls/{number}")
        if response.status_code == 404:
            raise GitProviderError(f"Pull request {number} not found in {repo.full_name}.")
        return normalize_pull_request(response.json(), repo=repo)

    async def find_pull_request(self, repo: GitRepository, head: str, base: str) -> Optional[PullRequestInfo]:
        if repo.provider != self.name:
            raise GitProviderError(f"Pull-request lookup targets provider '{repo.provider}', not '{self.name}'.")
        head = validate_git_ref(head, field="branch name")
        base = validate_git_ref(base, field="base branch")
        page_size = 100
        page = 1
        while page <= 10000:
            response = await self._request(
                "GET",
                f"/repos/{repo.owner}/{repo.name}/pulls",
                params={
                    "state": "open",
                    "head": f"{repo.owner}:{head}",
                    "base": base,
                    "per_page": str(page_size),
                    "page": str(page),
                },
            )
            if response.status_code == 404:
                raise GitProviderError(f"Could not list pull requests in {repo.full_name}: repository not found.")
            data = parse_pull_request_list(response, repo=repo)
            for item in data:
                pull_request_head = item.get("head")
                pull_request_base = item.get("base")
                match = normalize_open_pull_request_match(
                    item,
                    repo=repo,
                    head=head,
                    base=base,
                    provider_head=pull_request_head.get("ref") if isinstance(pull_request_head, dict) else None,
                    provider_base=pull_request_base.get("ref") if isinstance(pull_request_base, dict) else None,
                    open_states=("open",),
                )
                if match is not None:
                    return match
            next_page = pull_request_next_page(response, repo=repo, current_page=page)
            if next_page is not None:
                page = next_page
            elif len(data) < page_size:
                return None
            else:
                page += 1
        raise GitProviderError("GitHub pull-request pagination exceeded the safety limit.")

    async def find_open_pull_request_for_branch(
        self,
        repo: GitRepository,
        branch: str,
    ) -> Optional[PullRequestInfo]:
        branch = self._validate_plan_branch_request(repo, branch)
        page_size = 100
        for branch_filter, value in (("head", f"{repo.owner}:{branch}"), ("base", branch)):
            page = 1
            while page <= 10000:
                response = await self._request(
                    "GET",
                    f"/repos/{repo.owner}/{repo.name}/pulls",
                    params={
                        "state": "open",
                        branch_filter: value,
                        "per_page": str(page_size),
                        "page": str(page),
                    },
                )
                if response.status_code == 404:
                    raise GitProviderError(f"Could not list pull requests in {repo.full_name}: repository not found.")
                data = parse_pull_request_list(response, repo=repo)
                for item in data:
                    pull_request_head = item.get("head")
                    pull_request_base = item.get("base")
                    match = normalize_open_pull_request_branch_match(
                        item,
                        repo=repo,
                        branch=branch,
                        provider_head=(pull_request_head.get("ref") if isinstance(pull_request_head, dict) else None),
                        provider_base=(pull_request_base.get("ref") if isinstance(pull_request_base, dict) else None),
                        open_states=("open",),
                    )
                    if match is not None:
                        return match
                next_page = pull_request_next_page(response, repo=repo, current_page=page)
                if next_page is not None:
                    page = next_page
                elif len(data) < page_size:
                    break
                else:
                    page += 1
            else:
                raise GitProviderError("GitHub pull-request pagination exceeded the safety limit.")
        return None

    async def delete_plan_branch(self, repo: GitRepository, branch: str) -> None:
        branch = self._validate_plan_branch_request(repo, branch)
        if branch == repo.default_branch:
            raise GitProviderError("Refusing to delete the plan base branch.")
        default_branch = validate_git_ref(await self.get_default_branch(repo), field="provider default branch")
        if branch == default_branch:
            raise GitProviderError("Refusing to delete the repository default branch.")
        if await self.find_open_pull_request_for_branch(repo, branch) is not None:
            raise GitProviderError("Refusing to delete a branch with an open pull request.")
        response = await self._request(
            "DELETE",
            f"/repos/{repo.owner}/{repo.name}/git/refs/heads/{quote(branch, safe='/')}",
        )
        if response.status_code == 404:
            return
