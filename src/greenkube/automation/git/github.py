# src/greenkube/automation/git/github.py
"""GitHub REST API adapter (SaaS and Enterprise Server)."""

from __future__ import annotations

import base64
import logging
from collections.abc import Sequence
from typing import List, Optional
from urllib.parse import quote, urlsplit

from greenkube.automation.git.base import (
    COMMENT_PAGE_SIZE,
    CommitStatusState,
    GitCommitRequest,
    GitCommitResult,
    GitConflictError,
    GitFile,
    GitProvider,
    GitProviderCapability,
    GitProviderError,
    GitRepository,
    GitTree,
    GitTreeEntry,
    PullRequestComment,
    PullRequestInfo,
    PullRequestOptions,
    _git_response_object,
    _validate_full_commit_sha,
    _validate_git_path,
    normalize_open_pull_request_branch_match,
    normalize_open_pull_request_match,
    normalize_pull_request,
    normalize_terminal_pull_request_match,
    parse_comment_id,
    parse_comment_list,
    parse_pull_request_list,
    pull_request_next_page,
    quote_git_path,
    validate_git_ref,
)

_TREE_ENTRY_TYPES = frozenset({"blob", "commit"})

logger = logging.getLogger(__name__)


def _author_login(pull_request: PullRequestInfo) -> str | None:
    """The login of the PR's author (GreenKube's identity), when the payload carries it."""
    user = pull_request.raw.get("user") if isinstance(pull_request.raw, dict) else None
    login = user.get("login") if isinstance(user, dict) else None
    return login if isinstance(login, str) and login else None


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
            GitProviderCapability.COMMIT_ANCESTRY,
            GitProviderCapability.COMMIT_STATUS,
            GitProviderCapability.PULL_REQUEST_COMMENTS,
            GitProviderCapability.PULL_REQUEST_METADATA,
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

    async def get_tree(self, repo: GitRepository, ref: str) -> GitTree:
        """Read the whole recursive tree in one call, independent of repository size."""
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/git/trees/{quote(ref, safe='/')}",
            params={"recursive": "1"},
        )
        if response.status_code == 404:
            raise GitProviderError(f"GitHub tree for {repo.full_name}@{ref} was not found.", status_code=404)
        data = _git_response_object(response, provider=self.name, ref=ref)
        if data.get("truncated"):
            raise GitProviderError(f"GitHub tree for {repo.full_name}@{ref} was truncated; refusing partial results.")
        items = data.get("tree")
        tree_sha = data.get("sha")
        if not isinstance(items, list) or not isinstance(tree_sha, str) or not tree_sha:
            raise GitProviderError(f"GitHub returned an invalid tree for {repo.full_name}@{ref}.")
        entries: dict[str, GitTreeEntry] = {}
        for item in items:
            if not isinstance(item, dict):
                raise GitProviderError(f"GitHub returned an invalid tree entry for {repo.full_name}@{ref}.")
            item_type = item.get("type")
            if item_type == "tree":
                continue
            path, sha, mode = item.get("path"), item.get("sha"), item.get("mode")
            if (
                not isinstance(item_type, str)
                or item_type not in _TREE_ENTRY_TYPES
                or not isinstance(path, str)
                or not isinstance(sha, str)
                or not sha
                or not isinstance(mode, str)
                or path in entries
            ):
                raise GitProviderError(f"GitHub returned an invalid tree entry for {repo.full_name}@{ref}.")
            entries[path] = GitTreeEntry(path=path, sha=sha, mode=mode, type=item_type)
        return GitTree(entries=entries, sha=tree_sha)

    async def get_blob_ids(self, repo: GitRepository, ref: str, paths: Sequence[str]) -> dict[str, Optional[str]]:
        """Resolve blob IDs from one (cached) recursive tree instead of per-file reads."""
        for path in paths:
            _validate_git_path(path)
        if not paths:
            return {}
        tree = await self._get_tree_cached(repo, ref)
        blob_ids: dict[str, Optional[str]] = {}
        for path in paths:
            entry = tree.entries.get(path)
            blob_ids[path] = entry.sha if entry is not None and entry.type == "blob" else None
        return blob_ids

    async def is_ancestor(self, repo: GitRepository, ancestor_sha: str, descendant_sha: str) -> Optional[bool]:
        ancestor, descendant = self._validate_ancestry_request(repo, ancestor_sha, descendant_sha)
        if ancestor == descendant:
            return True
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/compare/{ancestor}...{descendant}",
            params={"per_page": "1"},
        )
        if response.status_code == 404:
            return None
        data = _git_response_object(response, provider=self.name, ref=f"{ancestor}...{descendant}")
        status = data.get("status")
        behind_by = data.get("behind_by")
        if status in ("identical", "ahead"):
            return behind_by in (None, 0)
        if status in ("behind", "diverged"):
            return False
        return None

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

        # Create first: an existing ref answers 422, so the common case costs one write.
        response = await self._request(
            "POST",
            f"/repos/{repo.owner}/{repo.name}/git/refs",
            json_body={"ref": f"refs/heads/{branch}", "sha": sha},
            ok_statuses=(200, 201, 422),
        )
        if response.status_code != 422:
            return
        exists = await self._request("GET", f"/repos/{repo.owner}/{repo.name}/git/ref/heads/{quote(branch, safe='/')}")
        if exists.status_code == 200:
            logger.info("Branch %s already exists in %s; reusing it.", branch, repo.full_name)
            return
        raise GitProviderError(f"GitHub could not create branch '{branch}' in {repo.full_name}.", status_code=422)

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

        # One recursive tree read (shared with branch-state checks through the
        # immutable cache) yields the base tree SHA and every precondition, so
        # the commit costs O(1) reads instead of one Contents call per file.
        base_tree = await self._get_tree_cached(repo, base_revision)
        base_tree_sha = base_tree.sha
        if not isinstance(base_tree_sha, str) or not base_tree_sha:
            raise GitProviderError(f"GitHub returned an invalid base tree for {repo.full_name}@{base_revision}.")
        for edit in request.files:
            existing = base_tree.entries.get(edit.path)
            exists = existing is not None and existing.type == "blob"
            if edit.action == "create" and existing is not None:
                raise GitConflictError(f"Cannot create '{edit.path}': the file already exists at the base revision.")
            if edit.action != "create" and not exists:
                raise GitConflictError(f"Cannot {edit.action} '{edit.path}': the file is missing at the base revision.")
        tree_entries = []
        for edit in request.files:
            base_entry = base_tree.entries.get(edit.path)
            mode = base_entry.mode if base_entry is not None and base_entry.mode in ("100644", "100755") else "100644"
            entry: dict[str, str | None] = {"path": edit.path, "mode": mode, "type": "blob"}
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

        commit_body: dict[str, object] = {
            "message": request.message,
            "tree": new_tree_sha,
            "parents": [base_revision],
        }
        if not self.platform_signed_commits:
            author = {"name": request.author_name, "email": request.author_email}
            commit_body["author"] = author
            commit_body["committer"] = author
        # A GitHub App commit without author and committer is created and signed by GitHub
        # as the App's bot, so it shows as Verified (J8).
        commit_response = await self._request(
            "POST",
            f"/repos/{repo.owner}/{repo.name}/git/commits",
            json_body=commit_body,
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
        verification = new_commit.get("verification") if isinstance(new_commit, dict) else None
        verified = verification.get("verified") if isinstance(verification, dict) else None
        reason = verification.get("reason") if isinstance(verification, dict) else None
        return GitCommitResult(
            commit_sha=commit_sha,
            branch=branch,
            files_changed=[edit.path for edit in request.files],
            verified=verified if isinstance(verified, bool) else None,
            verification_reason=reason if isinstance(reason, str) else None,
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
            await self._add_labels(repo, pull_request.number, selected_options.labels)
        await self._request_reviewers(
            repo,
            pull_request.number,
            selected_options.reviewers,
            selected_options.team_reviewers,
            author=_author_login(pull_request),
        )

    async def _add_labels(self, repo: GitRepository, number: int, labels: Sequence[str]) -> None:
        # The issues API creates labels that do not exist yet (J2).
        response = await self._request(
            "POST",
            f"/repos/{repo.owner}/{repo.name}/issues/{number}/labels",
            json_body={"labels": list(labels)},
        )
        if response.status_code == 404:
            raise GitProviderError("GitHub could not apply configured labels to the pull request.")

    async def _request_reviewers(
        self,
        repo: GitRepository,
        number: int,
        requested_users: Sequence[str],
        requested_teams: Sequence[str],
        *,
        author: str | None = None,
    ) -> None:
        if not requested_users and not requested_teams:
            return
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/pulls/{number}/requested_reviewers",
        )
        if response.status_code == 404:
            raise GitProviderError("GitHub could not read requested reviewers for the pull request.")
        data = _git_response_object(
            response,
            provider=self.name,
            ref=f"pull request {number} reviewers",
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

        # GitHub refuses a review request from the PR's own author (GreenKube's identity).
        skipped = {author.casefold()} if author else set()
        reviewers = [name for name in requested_users if name.casefold() not in existing_users | skipped]
        team_reviewers = [name for name in requested_teams if name.casefold() not in existing_teams]
        if not reviewers and not team_reviewers:
            return
        try:
            await self._post_reviewers(repo, number, reviewers, team_reviewers)
            return
        except GitProviderError as error:
            if error.status_code != 422 or len(reviewers) + len(team_reviewers) < 2:
                raise
        # One unknown, departed or non-collaborator login makes GitHub refuse the whole
        # request (422); request each reviewer alone so the valid ones are still asked.
        refused: list[str] = []
        for users, teams in [*(([name], []) for name in reviewers), *(([], [name]) for name in team_reviewers)]:
            try:
                await self._post_reviewers(repo, number, users, teams)
            except GitProviderError as error:
                if error.status_code != 422:
                    raise
                refused.extend(users or [f"team {name}" for name in teams])
        if refused:
            raise GitProviderError(f"GitHub refused review requests for: {', '.join(refused)}.", status_code=422)

    async def _post_reviewers(
        self, repo: GitRepository, number: int, reviewers: Sequence[str], team_reviewers: Sequence[str]
    ) -> None:
        response = await self._request(
            "POST",
            f"/repos/{repo.owner}/{repo.name}/pulls/{number}/requested_reviewers",
            json_body={"reviewers": list(reviewers), "team_reviewers": list(team_reviewers)},
        )
        if response.status_code == 404:
            raise GitProviderError("GitHub could not request configured pull-request reviewers.")

    async def add_pull_request_metadata(
        self,
        repo: GitRepository,
        pull_request: PullRequestInfo,
        *,
        labels: Sequence[str] = (),
        reviewers: Sequence[str] = (),
        team_reviewers: Sequence[str] = (),
    ) -> None:
        number = self._validate_pull_request_number(repo, pull_request.number)
        if labels:
            await self._add_labels(repo, number, labels)
        await self._request_reviewers(repo, number, reviewers, team_reviewers, author=_author_login(pull_request))

    async def set_commit_status(
        self,
        repo: GitRepository,
        sha: str,
        *,
        state: CommitStatusState,
        context: str,
        description: str,
        target_url: Optional[str] = None,
    ) -> None:
        sha = self._validate_commit_status(repo, sha, state, context)
        payload: dict[str, str] = {"state": state, "context": context, "description": description[:140]}
        if target_url:
            payload["target_url"] = target_url
        response = await self._request(
            "POST", f"/repos/{repo.owner}/{repo.name}/statuses/{quote(sha, safe='')}", json_body=payload
        )
        if response.status_code == 404:
            raise GitProviderError(f"GitHub could not set a commit status on {sha} in {repo.full_name}.")

    async def _list_pull_request_comments(
        self, repo: GitRepository, number: int, page: int
    ) -> tuple[list[PullRequestComment], bool]:
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/issues/{number}/comments",
            params={"per_page": str(COMMENT_PAGE_SIZE), "page": str(page)},
        )
        if response.status_code == 404:
            raise GitProviderError(f"GitHub could not list comments of pull request {number}.")
        comments = parse_comment_list(response, provider=self.name)
        has_next = pull_request_next_page(response, repo=repo, current_page=page) is not None or (
            "next" not in response.links and len(comments) >= COMMENT_PAGE_SIZE
        )
        return comments, has_next

    async def _create_pull_request_comment(self, repo: GitRepository, number: int, body: str) -> int:
        response = await self._request(
            "POST",
            f"/repos/{repo.owner}/{repo.name}/issues/{number}/comments",
            json_body={"body": body},
            retryable=False,
        )
        return parse_comment_id(response, provider=self.name)

    async def _update_pull_request_comment(self, repo: GitRepository, number: int, comment_id: int, body: str) -> None:
        response = await self._request(
            "PATCH",
            f"/repos/{repo.owner}/{repo.name}/issues/comments/{comment_id}",
            json_body={"body": body},
        )
        if response.status_code == 404:
            raise GitProviderError(f"GitHub comment {comment_id} disappeared before it could be updated.")

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

    async def find_terminal_pull_request(self, repo: GitRepository, head: str, base: str) -> Optional[PullRequestInfo]:
        """The most recently updated merged or closed request from ``head`` into ``base`` (D10)."""
        if repo.provider != self.name:
            raise GitProviderError(f"Pull-request lookup targets provider '{repo.provider}', not '{self.name}'.")
        head = validate_git_ref(head, field="branch name")
        base = validate_git_ref(base, field="base branch")
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/pulls",
            params={
                "state": "closed",
                "head": f"{repo.owner}:{head}",
                "base": base,
                "sort": "updated",
                "direction": "desc",
                "per_page": "100",
            },
        )
        if response.status_code == 404:
            raise GitProviderError(f"Could not list pull requests in {repo.full_name}: repository not found.")
        for item in parse_pull_request_list(response, repo=repo):
            pull_request_head = item.get("head")
            pull_request_base = item.get("base")
            match = normalize_terminal_pull_request_match(
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
        return None

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
